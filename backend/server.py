import time
import subprocess
import sys
import os
import re
import shutil
import threading
import json
import hashlib
import base64
import tempfile
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote
from urllib import request, error
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Keep WORKSPACE_ROOT pinned to the repository root.
# When this file lives under backend/, one extra dirname() is required.
_script_dir = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_script_dir) == "backend":
    WORKSPACE_ROOT = os.path.dirname(_script_dir)
else:
    WORKSPACE_ROOT = _script_dir

# Paths resolved from the workspace root.
PDF_DIR = os.path.join(WORKSPACE_ROOT, "papers")
BUILD_SCRIPT = os.path.join(WORKSPACE_ROOT, "build.py")
METADATA_FILE = os.path.join(WORKSPACE_ROOT, "metadata.json")
METADATA_DEMO_FILE = os.path.join(WORKSPACE_ROOT, "metadata.demo.json")

# Bootstrap metadata.json from the tracked demo file on first run.
if not os.path.exists(METADATA_FILE) and os.path.exists(METADATA_DEMO_FILE):
    shutil.copy2(METADATA_DEMO_FILE, METADATA_FILE)

RECYCLE_DIR = os.path.join(WORKSPACE_ROOT, ".recycle_bin")
SPEEDREAD_CACHE_DIR = os.path.join(WORKSPACE_ROOT, ".speedread_cache")
LOG_FILE = os.path.join(WORKSPACE_ROOT, "waatchdog.log")

HTTP_PORT = 8000
SPEEDREAD_MAX_IMAGE_PAGES = 4
SPEEDREAD_IMAGE_WIDTH = 1400
SPEEDREAD_MAX_SOURCE_CHARS = 24000


def _configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if not stream:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _log(msg: str) -> None:
    """Write a timestamped log line."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _bring_explorer_to_front(target_path: str) -> None:
    """Best-effort attempt to focus the Explorer window for target_path."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return

    user32 = ctypes.windll.user32
    target_dir = os.path.basename(os.path.dirname(target_path)) or os.path.basename(target_path)
    if not target_dir:
        return

    EnumWindows = user32.EnumWindows
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    GetWindowText = user32.GetWindowTextW
    GetWindowTextLength = user32.GetWindowTextLengthW
    IsWindowVisible = user32.IsWindowVisible
    GetClassName = user32.GetClassNameW

    found = []

    def callback(hwnd, _lparam):
        if not IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        GetClassName(hwnd, cls, 256)
        if cls.value not in ("CabinetWClass", "ExploreWClass"):
            return True
        length = GetWindowTextLength(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        GetWindowText(hwnd, buf, length + 1)
        if target_dir.lower() in buf.value.lower():
            found.append(hwnd)
            return False
        return True

    # Give Explorer a brief moment to create the target window.
    for _ in range(20):
        time.sleep(0.1)
        found.clear()
        EnumWindows(EnumWindowsProc(callback), 0)
        if found:
            hwnd = found[0]
            try:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            except Exception:
                pass
            return


class PaperRequestHandler(SimpleHTTPRequestHandler):
    """Static file handler with extra local management APIs."""

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/open-folder":
            self.handle_open_folder(parse_qs(parsed.query))
            return
        if parsed.path == "/api/log":
            self.handle_get_log(parse_qs(parsed.query))
            return
        if parsed.path == "/api/recycle-list":
            self.handle_recycle_list()
            return
        if parsed.path == "/api/ping":
            self._send_json(200, {"ok": True, "service": "waatchdog"})
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            self._send_json(400, {"ok": False, "error": "JSON 解析失败"})
            return

        if parsed.path == "/api/delete-paper":
            self.handle_delete_paper(payload)
            return
        if parsed.path == "/api/recycle-restore":
            self.handle_recycle_restore(payload)
            return
        if parsed.path == "/api/recycle-purge":
            self.handle_recycle_purge(payload)
            return
        if parsed.path == "/api/recycle-purge-all":
            self.handle_recycle_purge_all()
            return
        if parsed.path == "/api/save-metadata":
            self.handle_save_metadata(payload)
            return
        if parsed.path == "/api/update-paper":
            self.handle_update_paper(payload)
            return
        if parsed.path == "/api/generate-speedread":
            self.handle_generate_speedread(payload)
            return
        if parsed.path == "/api/test-speedread-config":
            self.handle_test_speedread_config(payload)
            return
        self._send_json(404, {"ok": False, "error": "未知接口"})

    # ------- recycle bin -------
    def _safe_rel(self, rel: str):
        """Normalize a relative path and keep it inside the workspace."""
        rel = (rel or "").replace("\\", "/").lstrip("/")
        if not rel:
            return None
        abs_path = os.path.abspath(os.path.join(WORKSPACE_ROOT, rel))
        try:
            common = os.path.commonpath([WORKSPACE_ROOT, abs_path])
        except ValueError:
            return None
        if common != WORKSPACE_ROOT:
            return None
        return abs_path

    def _slugify(self, s: str) -> str:
        s = re.sub(r"[^\w\-.]+", "_", s, flags=re.UNICODE)
        return s[:80] or "item"

    def handle_delete_paper(self, payload):
        file_key = (payload.get("file_key") or "").strip()
        if not file_key:
            self._send_json(400, {"ok": False, "error": "缺少 file_key"})
            return

        # Resolve the original entry from metadata.json.
        meta_abs = os.path.join(WORKSPACE_ROOT, METADATA_FILE)
        try:
            with open(meta_abs, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"读取 metadata.json 失败: {exc}"})
            return

        entry = meta.get(file_key)
        if not entry:
            self._send_json(404, {"ok": False, "error": f"metadata 中找不到 {file_key}"})
            return

        # Recover the real relative disk path from the encoded pdf path.
        pdf_local = entry.get("pdf_local") or entry.get("pdf") or ""
        rel_disk = unquote(pdf_local)  # e.g. "papers/Embodied AI/foo.pdf"
        abs_pdf = self._safe_rel(rel_disk)
        if not abs_pdf or not os.path.isfile(abs_pdf):
            self._send_json(404, {"ok": False, "error": f"文件不存在: {rel_disk}"})
            return

        # Move the file into .recycle_bin/<id>/.
        recycle_root = os.path.join(WORKSPACE_ROOT, RECYCLE_DIR)
        os.makedirs(recycle_root, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        rid = f"{ts}_{self._slugify(os.path.splitext(os.path.basename(abs_pdf))[0])}"
        item_dir = os.path.join(recycle_root, rid)
        os.makedirs(item_dir, exist_ok=True)

        try:
            dest_pdf = os.path.join(item_dir, os.path.basename(abs_pdf))
            shutil.move(abs_pdf, dest_pdf)
        except Exception as exc:
            try: os.rmdir(item_dir)
            except Exception: pass
            self._send_json(500, {"ok": False, "error": f"移动文件失败: {exc}"})
            return

        info = {
            "id": rid,
            "file_key": file_key,
            "original_rel": rel_disk.replace("\\", "/"),
            "deleted_at": datetime.now().isoformat(timespec="seconds"),
            "title": entry.get("title", ""),
            "metadata": entry,
        }
        try:
            with open(os.path.join(item_dir, "info.json"), "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            _log(f"写入回收站 info.json 失败: {exc}")

        _log(f"已移入回收站: {rel_disk} -> {RECYCLE_DIR}/{rid}/")
        self._send_json(200, {"ok": True, "id": rid, "title": info["title"]})

    def handle_recycle_list(self):
        recycle_root = os.path.join(WORKSPACE_ROOT, RECYCLE_DIR)
        items = []
        if os.path.isdir(recycle_root):
            for name in sorted(os.listdir(recycle_root), reverse=True):
                d = os.path.join(recycle_root, name)
                if not os.path.isdir(d):
                    continue
                info_path = os.path.join(d, "info.json")
                if not os.path.isfile(info_path):
                    continue
                try:
                    with open(info_path, "r", encoding="utf-8") as f:
                        info = json.load(f)
                    items.append({
                        "id": info.get("id", name),
                        "title": info.get("title", ""),
                        "original_rel": info.get("original_rel", ""),
                        "deleted_at": info.get("deleted_at", ""),
                        "file_key": info.get("file_key", ""),
                    })
                except Exception:
                    continue
        self._send_json(200, {"ok": True, "items": items})

    def _read_recycle_info(self, rid: str):
        rid = (rid or "").strip()
        if not rid or "/" in rid or "\\" in rid or rid in (".", ".."):
            return None, None
        item_dir = os.path.join(WORKSPACE_ROOT, RECYCLE_DIR, rid)
        if not os.path.isdir(item_dir):
            return None, None
        info_path = os.path.join(item_dir, "info.json")
        if not os.path.isfile(info_path):
            return item_dir, None
        try:
            with open(info_path, "r", encoding="utf-8") as f:
                return item_dir, json.load(f)
        except Exception:
            return item_dir, None

    def handle_recycle_restore(self, payload):
        rid = payload.get("id")
        item_dir, info = self._read_recycle_info(rid)
        if not item_dir:
            self._send_json(404, {"ok": False, "error": "回收站项目不存在"})
            return
        if not info:
            self._send_json(500, {"ok": False, "error": "info.json 缺失或损坏"})
            return

        rel_disk = info.get("original_rel") or ""
        target_abs = self._safe_rel(rel_disk)
        if not target_abs:
            self._send_json(400, {"ok": False, "error": "原始路径非法"})
            return
        if os.path.exists(target_abs):
            self._send_json(409, {"ok": False, "error": "目标位置已存在同名文件"})
            return

        # Find the first PDF inside the recycle item directory.
        src_pdf = None
        for name in os.listdir(item_dir):
            if name.lower().endswith(".pdf"):
                src_pdf = os.path.join(item_dir, name)
                break
        if not src_pdf:
            self._send_json(500, {"ok": False, "error": "回收站中找不到 PDF 文件"})
            return

        os.makedirs(os.path.dirname(target_abs), exist_ok=True)
        try:
            shutil.move(src_pdf, target_abs)
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"恢复文件失败: {exc}"})
            return

        # Rebuild immediately so metadata includes the restored file.
        try:
            subprocess.run([sys.executable, BUILD_SCRIPT], check=False, cwd=WORKSPACE_ROOT)
        except Exception as exc:
            _log(f"恢复后执行 build 失败: {exc}")

        # Merge saved metadata back in so notes/read/tags survive restore.
        saved_meta = info.get("metadata") or {}
        file_key = info.get("file_key") or saved_meta.get("file_key")
        if file_key and saved_meta:
            meta_abs = os.path.join(WORKSPACE_ROOT, METADATA_FILE)
            try:
                with open(meta_abs, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                # Restore user fields, but keep build-generated path fields.
                current = meta.get(file_key) or {}
                merged = {**current, **saved_meta}
                # Trust build output for path casing and encoding.
                if current.get("pdf"): merged["pdf"] = current["pdf"]
                if current.get("pdf_local"): merged["pdf_local"] = current["pdf_local"]
                meta[file_key] = merged
                with open(meta_abs, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
            except Exception as exc:
                _log(f"合并恢复 metadata 失败: {exc}")

        # Remove the recycle item directory after restore.
        try:
            shutil.rmtree(item_dir)
        except Exception as exc:
            _log(f"清理回收站目录失败: {exc}")

        _log(f"已恢复: {rel_disk}")
        self._send_json(200, {"ok": True, "file_key": file_key})

    def handle_recycle_purge(self, payload):
        rid = payload.get("id")
        item_dir, _ = self._read_recycle_info(rid)
        if not item_dir:
            self._send_json(404, {"ok": False, "error": "回收站项目不存在"})
            return
        try:
            shutil.rmtree(item_dir)
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"删除失败: {exc}"})
            return
        _log(f"已永久删除回收站项目: {rid}")
        self._send_json(200, {"ok": True})

    def handle_recycle_purge_all(self):
        recycle_root = os.path.join(WORKSPACE_ROOT, RECYCLE_DIR)
        count = 0
        if os.path.isdir(recycle_root):
            for name in os.listdir(recycle_root):
                d = os.path.join(recycle_root, name)
                if os.path.isdir(d):
                    try:
                        shutil.rmtree(d)
                        count += 1
                    except Exception as exc:
                        _log(f"清空回收站失败 {name}: {exc}")
        _log(f"已清空回收站，共 {count} 项")
        self._send_json(200, {"ok": True, "count": count})

    # ------- metadata persistence -------
    def _atomic_write_metadata(self, data):
        meta_abs = os.path.join(WORKSPACE_ROOT, METADATA_FILE)
        tmp_abs = meta_abs + ".tmp"
        with open(tmp_abs, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_abs, meta_abs)

    def handle_save_metadata(self, payload):
        data = payload.get("data")
        if not isinstance(data, dict):
            self._send_json(400, {"ok": False, "error": "缺少 data 字段或类型不正确"})
            return
        try:
            self._atomic_write_metadata(data)
        except Exception as exc:
            _log(f"保存 metadata 失败: {exc}")
            self._send_json(500, {"ok": False, "error": f"写入失败: {exc}"})
            return
        self._send_json(200, {"ok": True, "count": len(data)})

    def handle_update_paper(self, payload):
        file_key = (payload.get("file_key") or "").strip()
        fields = payload.get("fields")
        if not file_key or not isinstance(fields, dict):
            self._send_json(400, {"ok": False, "error": "缺少 file_key 或 fields"})
            return
        meta_abs = os.path.join(WORKSPACE_ROOT, METADATA_FILE)
        try:
            if os.path.exists(meta_abs):
                with open(meta_abs, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            else:
                meta = {}
            current = meta.get(file_key) or {}
            merged = {**current, **fields}
            merged["file_key"] = file_key
            meta[file_key] = merged
            self._atomic_write_metadata(meta)
        except Exception as exc:
            _log(f"update-paper 失败: {exc}")
            self._send_json(500, {"ok": False, "error": f"更新失败: {exc}"})
            return
        self._send_json(200, {"ok": True, "file_key": file_key})

    # ------- paper speed-read -------
    def _load_metadata_dict(self):
        meta_abs = os.path.join(WORKSPACE_ROOT, METADATA_FILE)
        if not os.path.exists(meta_abs):
            return {}
        with open(meta_abs, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}

    def _clean_page_text(self, text, limit=None):
        cleaned = (text or "").replace("\x00", "")
        cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = cleaned.strip()
        if limit and len(cleaned) > limit:
            cleaned = cleaned[:limit].rstrip() + " ..."
        return cleaned

    def _speedread_text_value(self, value, fallback="论文中未明确说明"):
        if not isinstance(value, str):
            return fallback
        cleaned = self._clean_page_text(value)
        return cleaned or fallback

    def _speedread_list_value(self, value, fallback_text="论文中未明确说明", max_items=6):
        if not isinstance(value, list):
            return [fallback_text]
        items = []
        for item in value:
            text = self._speedread_text_value(item, "")
            if text:
                items.append(text)
            if len(items) >= max_items:
                break
        return items or [fallback_text]

    def _resolve_pdf_entry(self, file_key, metadata):
        entry = metadata.get(file_key)
        if not isinstance(entry, dict):
            return None, None, None
        pdf_local = entry.get("pdf_local") or entry.get("pdf") or ""
        rel_disk = unquote(pdf_local)
        abs_pdf = self._safe_rel(rel_disk)
        if not abs_pdf or not os.path.isfile(abs_pdf):
            return entry, rel_disk, None
        return entry, rel_disk.replace("\\", "/"), abs_pdf

    def _run_capture(self, cmd):
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def _read_pdf_page_count(self, abs_pdf):
        result = self._run_capture(["pdfinfo", abs_pdf])
        if result.returncode != 0:
            return 0
        match = re.search(r"^Pages:\s+(\d+)", result.stdout, re.IGNORECASE | re.MULTILINE)
        return int(match.group(1)) if match else 0

    def _extract_pdf_pages_text(self, abs_pdf, page_count):
        result = self._run_capture(["pdftotext", "-layout", "-enc", "UTF-8", abs_pdf, "-"])
        if result.returncode != 0:
            detail = self._clean_page_text(result.stderr or result.stdout, 500)
            raise RuntimeError(f"pdftotext 失败: {detail or '未知错误'}")
        raw_pages = result.stdout.split("\f")
        pages = [self._clean_page_text(page) for page in raw_pages]
        while pages and not pages[-1]:
            pages.pop()
        if page_count and len(pages) < page_count:
            pages.extend([""] * (page_count - len(pages)))
        return pages

    def _score_page_for_keywords(self, page_no, page_count, text, keywords):
        lower = (text or "").lower()
        score = 0
        for keyword in keywords:
            if keyword in lower:
                score += 2
        if "figure" in lower or "fig." in lower or "fig " in lower:
            score += 2
        if "table" in lower:
            score += 2
        if page_no <= 2:
            score += 1
        if page_count and page_no >= max(page_count - 1, 1) and ("conclusion" in lower or "discussion" in lower):
            score += 1
        return score

    def _select_speedread_pages(self, page_texts, page_count):
        if not page_count:
            page_count = len(page_texts)
        if page_count <= 0:
            return []

        role_rules = [
            ("方法总览图", ["figure", "fig.", "fig ", "framework", "pipeline", "architecture", "overview", "method"]),
            ("核心方法页", ["method", "approach", "module", "algorithm", "framework", "pipeline"]),
            ("实验结果页", ["table", "result", "results", "experiment", "evaluation", "benchmark", "comparison"]),
            ("分析或消融页", ["ablation", "analysis", "case study", "limitation", "discussion"]),
        ]
        page_payloads = []
        for idx in range(page_count):
            text = page_texts[idx] if idx < len(page_texts) else ""
            page_payloads.append((idx + 1, text, (text or "").lower()))

        used = set()
        picked = []
        for label, keywords in role_rules:
            best = None
            for page_no, text, lower in page_payloads:
                if page_no in used or not lower.strip():
                    continue
                current_score = self._score_page_for_keywords(page_no, page_count, text, keywords)
                if current_score <= 0:
                    continue
                if best is None or current_score > best[0]:
                    best = (current_score, page_no, text)
            if best is not None:
                used.add(best[1])
                picked.append({
                    "page": best[1],
                    "reason": label,
                    "excerpt": self._clean_page_text(best[2], 900),
                })

        if len(picked) < SPEEDREAD_MAX_IMAGE_PAGES:
            ranked = []
            fallback_keywords = [
                "figure", "fig.", "fig ", "table", "method", "approach", "experiment",
                "evaluation", "benchmark", "ablation", "analysis", "results",
            ]
            for page_no, text, lower in page_payloads:
                if page_no in used:
                    continue
                current_score = self._score_page_for_keywords(page_no, page_count, text, fallback_keywords)
                if page_no == 1:
                    current_score += 2
                ranked.append((current_score, page_no, text))
            ranked.sort(key=lambda item: (-item[0], item[1]))
            for score, page_no, text in ranked:
                if page_no in used:
                    continue
                used.add(page_no)
                picked.append({
                    "page": page_no,
                    "reason": "关键页面" if score > 0 else "补充页面",
                    "excerpt": self._clean_page_text(text, 900),
                })
                if len(picked) >= SPEEDREAD_MAX_IMAGE_PAGES:
                    break

        if not picked:
            for page_no in sorted({1, min(2, page_count), min(4, page_count)}):
                if not page_no:
                    continue
                text = page_texts[page_no - 1] if page_no - 1 < len(page_texts) else ""
                picked.append({
                    "page": page_no,
                    "reason": "默认候选页",
                    "excerpt": self._clean_page_text(text, 900),
                })

        picked.sort(key=lambda item: item["page"])
        return picked[:SPEEDREAD_MAX_IMAGE_PAGES]

    def _render_speedread_page_image(self, abs_pdf, file_key, page_no):
        paper_hash = hashlib.sha1(file_key.encode("utf-8")).hexdigest()[:16]
        cache_dir = os.path.join(WORKSPACE_ROOT, SPEEDREAD_CACHE_DIR, paper_hash)
        os.makedirs(cache_dir, exist_ok=True)

        base_name = f"page_{page_no:03d}"
        target_jpg = os.path.join(cache_dir, base_name + ".jpg")
        target_png = os.path.join(cache_dir, base_name + ".png")
        prefix = os.path.join(cache_dir, base_name)

        cmd = [
            "pdftoppm",
            "-f", str(page_no),
            "-l", str(page_no),
            "-singlefile",
            "-scale-to", str(SPEEDREAD_IMAGE_WIDTH),
            "-jpeg",
            abs_pdf,
            prefix,
        ]
        result = self._run_capture(cmd)
        if result.returncode != 0 or not os.path.exists(target_jpg):
            fallback = [
                "pdftoppm",
                "-f", str(page_no),
                "-l", str(page_no),
                "-singlefile",
                "-scale-to", str(SPEEDREAD_IMAGE_WIDTH),
                "-png",
                abs_pdf,
                prefix,
            ]
            result = self._run_capture(fallback)
            if result.returncode != 0 or not os.path.exists(target_png):
                detail = self._clean_page_text(result.stderr or result.stdout, 300)
                raise RuntimeError(f"渲染第 {page_no} 页失败: {detail or '未知错误'}")
            return os.path.relpath(target_png, WORKSPACE_ROOT).replace(os.sep, "/")
        return os.path.relpath(target_jpg, WORKSPACE_ROOT).replace(os.sep, "/")

    def _build_speedread_grounding_text(self, page_texts, candidate_pages):
        page_count = len(page_texts)
        chosen = set()
        for page_no in [1, 2, 3, page_count - 1, page_count]:
            if 1 <= page_no <= page_count:
                chosen.add(page_no)
        for item in candidate_pages:
            page_no = int(item.get("page") or 0)
            if 1 <= page_no <= page_count:
                chosen.add(page_no)

        key_terms = ["abstract", "introduction", "method", "approach", "experiment", "evaluation", "conclusion", "limitation"]
        for term in key_terms:
            for idx, text in enumerate(page_texts, start=1):
                if term in (text or "").lower():
                    chosen.add(idx)
                    break

        blocks = []
        total_chars = 0
        for page_no in sorted(chosen):
            text = page_texts[page_no - 1] if page_no - 1 < len(page_texts) else ""
            cleaned = self._clean_page_text(text, 3600)
            if not cleaned:
                continue
            block = f"=== Page {page_no} ===\n{cleaned}\n"
            if total_chars + len(block) > SPEEDREAD_MAX_SOURCE_CHARS:
                break
            blocks.append(block)
            total_chars += len(block)
        return "\n".join(blocks)

    def _normalize_chat_endpoint(self, base_url):
        url = (base_url or "").strip()
        if not url:
            raise ValueError("未配置 API 地址")
        url = url.rstrip("/")
        if url.endswith("/chat/completions"):
            return url
        if url.endswith("/v1"):
            return url + "/chat/completions"
        return url + "/v1/chat/completions"

    def _image_to_data_url(self, rel_path):
        abs_path = self._safe_rel(rel_path)
        if not abs_path or not os.path.isfile(abs_path):
            return None
        with open(abs_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        mime = "image/jpeg" if rel_path.lower().endswith(".jpg") else "image/png"
        return f"data:{mime};base64,{encoded}"

    def _extract_completion_text(self, payload):
        if not isinstance(payload, dict):
            return ""
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        parts.append(item.get("text"))
                return "\n".join(parts)
        output = payload.get("output")
        if isinstance(output, list):
            parts = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                for content in item.get("content") or []:
                    if isinstance(content, dict) and isinstance(content.get("text"), str):
                        parts.append(content.get("text"))
            if parts:
                return "\n".join(parts)
        if isinstance(payload.get("content"), str):
            return payload.get("content")
        return ""

    def _extract_json_block(self, text):
        raw = (text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        try:
            return json.loads(raw)
        except Exception:
            pass

        start = raw.find("{")
        while start != -1:
            depth = 0
            in_string = False
            escape = False
            for idx in range(start, len(raw)):
                ch = raw[idx]
                if in_string:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = raw[start:idx + 1]
                        try:
                            return json.loads(candidate)
                        except Exception:
                            break
            start = raw.find("{", start + 1)
        raise ValueError("模型返回中未找到合法 JSON")

    def _normalize_speedread_payload(self, parsed, candidate_pages):
        candidate_by_page = {int(item.get("page") or 0): item for item in candidate_pages}
        raw_figures = parsed.get("core_figures") if isinstance(parsed, dict) else None
        figures = []
        if isinstance(raw_figures, list):
            for item in raw_figures:
                if not isinstance(item, dict):
                    continue
                try:
                    page_no = int(item.get("page") or 0)
                except (TypeError, ValueError):
                    continue
                if page_no not in candidate_by_page:
                    continue
                asset = candidate_by_page[page_no]
                figures.append({
                    "page": page_no,
                    "title": self._speedread_text_value(item.get("title")),
                    "what_to_look": self._speedread_text_value(item.get("what_to_look")),
                    "what_it_proves": self._speedread_text_value(item.get("what_it_proves")),
                    "why_important": self._speedread_text_value(item.get("why_important")),
                    "image_path": asset.get("image_path", ""),
                    "reason": asset.get("reason", ""),
                })

        if not figures:
            for asset in candidate_pages[:3]:
                figures.append({
                    "page": asset.get("page"),
                    "title": asset.get("reason") or "关键页面",
                    "what_to_look": "优先查看主图、表格标题和图注，再回到正文对应段落交叉验证。",
                    "what_it_proves": "如果自动输出缺少更细的证据，请以图注和正文原文为准。",
                    "why_important": "该页在自动筛选中最接近方法或实验的核心证据。",
                    "image_path": asset.get("image_path", ""),
                    "reason": asset.get("reason", ""),
                })

        return {
            "one_sentence": self._speedread_text_value(parsed.get("one_sentence") if isinstance(parsed, dict) else None),
            "quick_takeaways": self._speedread_list_value(parsed.get("quick_takeaways") if isinstance(parsed, dict) else None, max_items=5),
            "problem_and_motivation": self._speedread_text_value(parsed.get("problem_and_motivation") if isinstance(parsed, dict) else None),
            "method_overview": self._speedread_text_value(parsed.get("method_overview") if isinstance(parsed, dict) else None),
            "method_steps": self._speedread_list_value(parsed.get("method_steps") if isinstance(parsed, dict) else None, max_items=6),
            "core_figures": figures,
            "experiment_read": self._speedread_text_value(parsed.get("experiment_read") if isinstance(parsed, dict) else None),
            "contributions": self._speedread_list_value(parsed.get("contributions") if isinstance(parsed, dict) else None, max_items=5),
            "limitations": self._speedread_list_value(parsed.get("limitations") if isinstance(parsed, dict) else None, max_items=5),
            "deep_read_suggestions": self._speedread_list_value(parsed.get("deep_read_suggestions") if isinstance(parsed, dict) else None, max_items=6),
        }

    def _request_speedread_from_model(self, api_config, paper_meta, grounding_text, candidate_pages):
        api_key = (api_config.get("apiKey") or "").strip()
        model = (api_config.get("model") or "").strip()
        if not api_key:
            raise ValueError("未配置 API Key")
        if not model:
            raise ValueError("未配置模型名称")

        endpoint = self._normalize_chat_endpoint(api_config.get("apiBaseUrl"))
        try:
            timeout_sec = int(api_config.get("timeoutSec") or 180)
        except (TypeError, ValueError):
            timeout_sec = 180
        timeout_sec = max(30, min(timeout_sec, 600))

        prompt = (
            "你是一个只依据论文原文证据生成内容的论文速读助手。\n"
            "任务目标：帮助用户快速理解论文，不要生成泛泛摘要。\n"
            "写作要求：\n"
            "1. 全部使用中文，信息密度高，但要可读。\n"
            "2. 只依据提供的论文文本片段与候选页图像，不得补充外部知识。\n"
            "3. 如果论文没有明确说明某点，必须写“论文中未明确说明”。\n"
            "4. 不要夸奖论文，不要写营销式语言，不要把摘要直译成中文。\n"
            "5. 重点讲清研究问题、动机、方法如何工作、实验结果说明了什么。\n"
            "6. 图表解读要说明该看哪一页、看什么、它支撑了作者什么论点。\n"
            "7. 仅输出一个合法 JSON 对象，不要输出 Markdown 代码块。\n\n"
            "JSON schema:\n"
            "{\n"
            "  \"one_sentence\": \"一句话总结\",\n"
            "  \"quick_takeaways\": [\"省流点\", \"省流点\"],\n"
            "  \"problem_and_motivation\": \"问题与动机\",\n"
            "  \"method_overview\": \"方法速读\",\n"
            "  \"method_steps\": [\"步骤 1\", \"步骤 2\"],\n"
            "  \"core_figures\": [\n"
            "    {\n"
            "      \"page\": 3,\n"
            "      \"title\": \"图/表标题的中文概括\",\n"
            "      \"what_to_look\": \"读图时要先看什么\",\n"
            "      \"what_it_proves\": \"它支撑了什么结论\",\n"
            "      \"why_important\": \"为什么这张图/表值得优先看\"\n"
            "    }\n"
            "  ],\n"
            "  \"experiment_read\": \"实验速读\",\n"
            "  \"contributions\": [\"贡献 1\", \"贡献 2\"],\n"
            "  \"limitations\": [\"局限 1\", \"局限 2\"],\n"
            "  \"deep_read_suggestions\": [\"建议先精读哪一节、哪几页及原因\"]\n"
            "}\n\n"
            f"论文基础信息：\n标题：{paper_meta.get('title', '')}\n"
            f"作者：{paper_meta.get('authors', '')}\n"
            f"年份：{paper_meta.get('year', '')}\n"
            f"会议/期刊：{paper_meta.get('venue', '')}\n\n"
            "候选关键页（如果在 core_figures 中引用 page，必须从这些候选页里选择）：\n"
            + "\n".join(
                f"- page {item.get('page')}: {item.get('reason')}\n  摘录: {self._clean_page_text(item.get('excerpt'), 500)}"
                for item in candidate_pages
            )
            + "\n\n论文文本片段：\n"
            + grounding_text
        )

        content_items = [{"type": "text", "text": prompt}]
        has_image_inputs = False
        for item in candidate_pages:
            image_path = item.get("image_path")
            if not image_path:
                continue
            data_url = self._image_to_data_url(image_path)
            if not data_url:
                continue
            has_image_inputs = True
            content_items.append({
                "type": "text",
                "text": f"候选页 page {item.get('page')}，自动标记：{item.get('reason')}。请结合该页图表与前述文本片段交叉解读。",
            })
            content_items.append({
                "type": "image_url",
                "image_url": {"url": data_url},
            })

        # Bypass system proxies to avoid local proxy tools interfering.
        _no_proxy_opener = request.build_opener(request.ProxyHandler({}))

        def send_chat_request(user_content, request_mode):
            payload = {
                "model": model,
                "temperature": 0.2,
                "max_tokens": 2200,
                "messages": [
                    {
                        "role": "system",
                        "content": "你是严格依据论文证据输出 JSON 的论文速读助手。",
                    },
                    {
                        "role": "user",
                        "content": user_content,
                    },
                ],
            }

            req = request.Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                method="POST",
            )

            _log(f"send speed-read request: model={model}, mode={request_mode}, endpoint={endpoint}, timeout={timeout_sec}s")
            try:
                with _no_proxy_opener.open(req, timeout=timeout_sec) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                    _log(f"收到模型回复，长度 {len(body)}")
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
                _log(f"HTTP 错误 {exc.code}: {detail[:200]}")
                raise RuntimeError(f"模型接口返回 HTTP {exc.code}: {self._clean_page_text(detail, 600)}")
            except error.URLError as exc:
                _log(f"连接失败: {exc.reason}")
                raise RuntimeError(f"无法连接模型接口: {exc.reason}")
            except Exception as exc:
                _log(f"请求异常: {exc}")
                raise RuntimeError(f"请求模型接口异常: {exc}")

            try:
                data = json.loads(body)
                _log("JSON 解析成功")
            except json.JSONDecodeError as exc:
                _log(f"JSON 解析失败: {exc}, 响应长度 {len(body)} 字符")
                _log(f"   响应片段: {body[:500]}")
                raise RuntimeError(f"模型接口返回了无效 JSON，可能是错误或网关问题。详情: {self._clean_page_text(str(exc), 300)}")

            content = self._extract_completion_text(data)
            if not content:
                _log(f"无法从响应中提取文本。响应结构: {json.dumps(data, ensure_ascii=False)[:500]}")
                raise RuntimeError("模型接口返回成功，但没有生成文本内容")

            _log(f"提取生成文本: {len(content)} 字符")
            return self._extract_json_block(content), request_mode

        def should_fallback_to_text_only(message):
            lowered = (message or "").lower()
            if not has_image_inputs:
                return False
            if any(code in lowered for code in ("http 401", "http 403", "http 404", "http 429")):
                return False
            return any(token in lowered for token in (
                "http 400",
                "http 415",
                "http 422",
                "image",
                "image_url",
                "vision",
                "multimodal",
                "unsupported",
                "content",
                "messages",
                "没有生成文本内容",
            ))

        if has_image_inputs:
            try:
                return send_chat_request(content_items, "multimodal")
            except RuntimeError as exc:
                if not should_fallback_to_text_only(str(exc)):
                    raise
                _log("当前接口可能不支持图像输入或多模态消息，已自动降级为纯文本速读重试")

        return send_chat_request(prompt, "text-only")

    def handle_generate_speedread(self, payload):
        file_key = (payload.get("file_key") or "").strip()
        api_config = payload.get("apiConfig") or {}
        force = bool(payload.get("force"))
        if not file_key:
            self._send_json(400, {"ok": False, "error": "缺少 file_key"})
            return
        if not isinstance(api_config, dict):
            self._send_json(400, {"ok": False, "error": "apiConfig 格式不正确"})
            return

        try:
            metadata = self._load_metadata_dict()
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"读取 metadata 失败: {exc}"})
            return

        entry, rel_disk, abs_pdf = self._resolve_pdf_entry(file_key, metadata)
        if not entry:
            self._send_json(404, {"ok": False, "error": f"找不到论文: {file_key}"})
            return
        if not abs_pdf:
            self._send_json(404, {"ok": False, "error": f"PDF 不存在: {rel_disk}"})
            return

        existing = entry.get("speed_read") if isinstance(entry.get("speed_read"), dict) else {}
        if existing.get("status") == "success" and not force:
            self._send_json(200, {"ok": True, "cached": True, "speed_read": existing})
            return

        now_iso = datetime.now().isoformat(timespec="seconds")
        generating_state = {
            "status": "running",
            "generated_at": existing.get("generated_at") or now_iso,
            "updated_at": now_iso,
            "model": (api_config.get("model") or "").strip(),
            "error": "",
        }
        try:
            current = metadata.get(file_key) or {}
            current["speed_read"] = generating_state
            metadata[file_key] = current
            self._atomic_write_metadata(metadata)
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"写入生成状态失败: {exc}"})
            return

        try:
            page_count = self._read_pdf_page_count(abs_pdf)
            page_texts = self._extract_pdf_pages_text(abs_pdf, page_count)
            if not page_count:
                page_count = len(page_texts)

            candidate_pages = self._select_speedread_pages(page_texts, page_count)
            enriched_candidates = []
            for item in candidate_pages:
                enriched = dict(item)
                try:
                    enriched["image_path"] = self._render_speedread_page_image(abs_pdf, file_key, int(item.get("page") or 0))
                except Exception as img_exc:
                    _log(f"速读渲染第 {item.get('page')} 页失败: {img_exc}")
                    enriched["image_path"] = ""
                enriched_candidates.append(enriched)

            grounding_text = self._build_speedread_grounding_text(page_texts, enriched_candidates)
            if not grounding_text and not any(item.get("image_path") for item in enriched_candidates):
                raise RuntimeError("无法从 PDF 中提取可用文本或页面图像")

            parsed, request_mode = self._request_speedread_from_model(api_config, entry, grounding_text, enriched_candidates)
            normalized = self._normalize_speedread_payload(parsed, enriched_candidates)
            success_state = {
                "status": "success",
                "generated_at": existing.get("generated_at") or now_iso,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "model": (api_config.get("model") or "").strip(),
                "api_base_url": self._normalize_chat_endpoint(api_config.get("apiBaseUrl")),
                "request_mode": request_mode,
                "page_count": page_count,
                "candidate_pages": [
                    {
                        "page": item.get("page"),
                        "reason": item.get("reason", ""),
                        "image_path": item.get("image_path", ""),
                    }
                    for item in enriched_candidates
                ],
                "content": normalized,
            }

            metadata = self._load_metadata_dict()
            current = metadata.get(file_key) or {}
            current["speed_read"] = success_state
            metadata[file_key] = current
            self._atomic_write_metadata(metadata)
            _log(f"速读生成完成: {file_key}")
            self._send_json(200, {"ok": True, "speed_read": success_state})
        except Exception as exc:
            error_state = {
                "status": "error",
                "generated_at": existing.get("generated_at") or now_iso,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "model": (api_config.get("model") or "").strip(),
                "error": self._clean_page_text(str(exc), 600),
            }
            try:
                metadata = self._load_metadata_dict()
                current = metadata.get(file_key) or {}
                current["speed_read"] = error_state
                metadata[file_key] = current
                self._atomic_write_metadata(metadata)
            except Exception as write_exc:
                _log(f"写入速读错误状态失败: {write_exc}")
            _log(f"速读生成失败: {file_key} - {exc}")
            self._send_json(500, {"ok": False, "error": self._clean_page_text(str(exc), 600), "speed_read": error_state})

    def handle_test_speedread_config(self, payload):
        api_config = payload.get("apiConfig") or {}
        if not isinstance(api_config, dict):
            self._send_json(400, {"ok": False, "error": "apiConfig 格式不正确"})
            return

        test_paper_meta = {
            "title": "接口测试论文",
            "authors": "OpenPaper",
            "year": "2026",
            "venue": "Config Test",
        }
        test_grounding_text = (
            "This is a short connectivity test prompt for API and model availability. "
            "Return valid JSON only, and do not use external knowledge. "
            "Assume the paper proposes a method and reports effective experiment results."
        )
        try:
            parsed, request_mode = self._request_speedread_from_model(api_config, test_paper_meta, test_grounding_text, [])
            normalized = self._normalize_speedread_payload(parsed, [])
            self._send_json(200, {
                "ok": True,
                "request_mode": request_mode,
                "preview": normalized.get("one_sentence") or "测试成功",
            })
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": self._clean_page_text(str(exc), 600)})

    def handle_get_log(self, query):
        try:
            tail = int((query.get("tail") or ["500"])[0])
        except (TypeError, ValueError):
            tail = 500
        tail = max(1, min(tail, 5000))

        if not os.path.exists(LOG_FILE):
            self._send_json(200, {"ok": True, "path": LOG_FILE, "lines": [], "truncated": False})
            return
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"读取日志失败: {exc}"})
            return
        truncated = len(all_lines) > tail
        lines = all_lines[-tail:]
        self._send_json(200, {
            "ok": True,
            "path": LOG_FILE,
            "total": len(all_lines),
            "returned": len(lines),
            "truncated": truncated,
            "lines": [ln.rstrip("\n") for ln in lines],
        })

    def handle_open_folder(self, query):
        raw = (query.get("path") or [""])[0]
        rel_path = unquote(raw).replace("/", os.sep).replace("\\", os.sep)
        if not rel_path:
            self._send_json(400, {"ok": False, "error": "缺少 path 参数"})
            return

        # Prevent path traversal outside the workspace.
        abs_path = os.path.abspath(os.path.join(WORKSPACE_ROOT, rel_path))
        try:
            common = os.path.commonpath([WORKSPACE_ROOT, abs_path])
        except ValueError:
            common = ""
        if common != WORKSPACE_ROOT:
            self._send_json(403, {"ok": False, "error": "禁止访问工作区外的路径"})
            return

        if not os.path.exists(abs_path):
            self._send_json(404, {"ok": False, "error": f"路径不存在: {abs_path}"})
            return

        try:
            if sys.platform.startswith("win"):
                # Let the new process request foreground window focus.
                try:
                    import ctypes
                    ASFW_ANY = -1
                    ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)
                except Exception:
                    pass

                if os.path.isdir(abs_path):
                    os.startfile(abs_path)  # type: ignore[attr-defined]
                else:
                    # Open the parent folder and select the target file.
                    subprocess.Popen(
                        ["explorer", "/select,", abs_path],
                        close_fds=True,
                    )

                # Second pass: bring the matching Explorer window forward.
                threading.Thread(
                    target=_bring_explorer_to_front,
                    args=(abs_path,),
                    daemon=True,
                ).start()
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", abs_path] if os.path.isfile(abs_path) else ["open", abs_path])
            else:
                target = abs_path if os.path.isdir(abs_path) else os.path.dirname(abs_path)
                subprocess.Popen(["xdg-open", target])
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"打开失败: {exc}"})
            return

        self._send_json(200, {"ok": True, "path": abs_path})

    def log_message(self, format, *args):  # 静默模式
        return

class PDFHandler(FileSystemEventHandler):
    """Watch PDF changes under papers/ and debounce build.py runs."""

    DEBOUNCE_SECONDS = 1.0

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._timer = None  # type: ignore[assignment]

    def _is_pdf(self, path: str) -> bool:
        return bool(path) and path.lower().endswith(".pdf")

    def _schedule(self, reason: str, path: str):
        # Log relative paths only to keep lines readable.
        try:
            short = os.path.relpath(path, WORKSPACE_ROOT)
        except ValueError:
            short = path
        _log(f"PDF {reason}: {short}")
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.DEBOUNCE_SECONDS, self.run_build)
            self._timer.daemon = True
            self._timer.start()

    def on_created(self, event):
        if not event.is_directory and self._is_pdf(event.src_path):
            self._schedule("新增", event.src_path)

    def on_modified(self, event):
        if not event.is_directory and self._is_pdf(event.src_path):
            self._schedule("修改", event.src_path)

    def on_deleted(self, event):
        if not event.is_directory and self._is_pdf(event.src_path):
            self._schedule("删除", event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        if self._is_pdf(getattr(event, "dest_path", "")):
            self._schedule("移动(新位置)", event.dest_path)
        elif self._is_pdf(event.src_path):
            self._schedule("移动(旧位置)", event.src_path)

    def run_build(self):
        _log("执行 build.py")
        try:
            subprocess.run([sys.executable, BUILD_SCRIPT], check=False)
        except Exception as exc:
            _log(f"build 失败: {exc}")

if __name__ == "__main__":
    _configure_stdio()
    # Serve files from the repository root regardless of launch location.
    os.chdir(WORKSPACE_ROOT)
    
    # Parse an optional port override: python backend/server.py --port 8001
    port = HTTP_PORT
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg in ("--port", "-p") and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                print(f"无效端口: {argv[i + 1]}，使用默认 {HTTP_PORT}")
                port = HTTP_PORT
            break

    # Start the HTTP server for static files and local APIs.
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), PaperRequestHandler)
    except OSError as exc:
        print(f"无法绑定 127.0.0.1:{port}: {exc}")
        if getattr(exc, "winerror", None) == 10048 or "Address already in use" in str(exc):
            print("   端口已被占用，可能是之前的 backend/server.py 仍在运行。")
            print("   解决办法:")
            print("     1) 关闭旧服务，或在 PowerShell 执行:")
            print(f"        Get-NetTCPConnection -LocalPort {port} | Select OwningProcess")
            print("        Stop-Process -Id <PID> -Force")
            print(f"     2) 或换端口启动: python backend/server.py --port {port + 1}")
        sys.exit(1)

    http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    http_thread.start()
    _log(f"HTTP 服务已启动: http://127.0.0.1:{port}")

    os.makedirs(PDF_DIR, exist_ok=True)

    event_handler = PDFHandler()
    observer = Observer()
    observer.schedule(event_handler, PDF_DIR, recursive=True)
    observer.start()
    _log(f"监控已启动: {PDF_DIR}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        observer.join()
        httpd.shutdown()
        httpd.server_close()
        _log("监控和 HTTP 服务已停止")



