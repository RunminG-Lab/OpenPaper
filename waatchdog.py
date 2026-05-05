import time
import subprocess
import sys
import os
import re
import shutil
import threading
import json
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

PDF_DIR = "papers"  # 监控目录
BUILD_SCRIPT = "build.py"  # 构建脚本
METADATA_FILE = "metadata.json"
RECYCLE_DIR = ".recycle_bin"
HTTP_PORT = 8000
# 确保 WORKSPACE_ROOT 始终指向脚本所在的仓库根目录
WORKSPACE_ROOT = os.path.abspath(os.path.dirname(__file__))
LOG_FILE = os.path.join(WORKSPACE_ROOT, "waatchdog.log")


def _log(msg: str) -> None:
    """统一日志输出：前缀时间戳，简洁风格。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _bring_explorer_to_front(target_path: str) -> None:
    """在 Windows 上尝试把刚刚打开的资源管理器窗口置顶。"""
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

    # 给 explorer 一点时间真正打开窗口
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
    """在静态文件服务基础上，增加 /api/open-folder 接口用于调用资源管理器。"""

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
        self._send_json(404, {"ok": False, "error": "未知接口"})

    # ------- 回收站相关 -------
    def _safe_rel(self, rel: str):
        """把传入的相对路径规范化，并确保在工作区内。"""
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

        # 读 metadata.json 找到原始 entry（保留大小写的 pdf_local）
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

        # 还原真实磁盘相对路径（pdf_local 是 URL-encoded）
        pdf_local = entry.get("pdf_local") or entry.get("pdf") or ""
        rel_disk = unquote(pdf_local)  # e.g. "papers/Embodied AI/foo.pdf"
        abs_pdf = self._safe_rel(rel_disk)
        if not abs_pdf or not os.path.isfile(abs_pdf):
            self._send_json(404, {"ok": False, "error": f"文件不存在: {rel_disk}"})
            return

        # 移动到 .recycle_bin/<id>/
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
            _log(f"⚠️ 写入回收站 info.json 失败: {exc}")

        _log(f"🗑️ 删除到回收站: {rel_disk} → {RECYCLE_DIR}/{rid}/")
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

        # 找到回收站里的 PDF（除 info.json 外的第一个 .pdf）
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

        # 同步运行 build.py 让 metadata 立即包含新文件
        try:
            subprocess.run([sys.executable, BUILD_SCRIPT], check=False, cwd=WORKSPACE_ROOT)
        except Exception as exc:
            _log(f"⚠️ 恢复后 build 失败: {exc}")

        # 把保存的 metadata 合并回去（保留笔记/已读/标签等）
        saved_meta = info.get("metadata") or {}
        file_key = info.get("file_key") or saved_meta.get("file_key")
        if file_key and saved_meta:
            meta_abs = os.path.join(WORKSPACE_ROOT, METADATA_FILE)
            try:
                with open(meta_abs, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                # 用保存版本覆盖（pdf 路径可能小写化，沿用 build 生成的更安全）
                current = meta.get(file_key) or {}
                merged = {**current, **saved_meta}
                # pdf 路径以 build 生成的为准（避免大小写漂移）
                if current.get("pdf"): merged["pdf"] = current["pdf"]
                if current.get("pdf_local"): merged["pdf_local"] = current["pdf_local"]
                meta[file_key] = merged
                with open(meta_abs, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
            except Exception as exc:
                _log(f"⚠️ 合并恢复 metadata 失败: {exc}")

        # 清理回收站项目目录
        try:
            shutil.rmtree(item_dir)
        except Exception as exc:
            _log(f"⚠️ 清理回收站目录失败: {exc}")

        _log(f"♻️ 恢复: {rel_disk}")
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
        _log(f"🔥 永久删除回收站项: {rid}")
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
                        _log(f"⚠️ 清空回收站失败 {name}: {exc}")
        _log(f"🔥 清空回收站，共 {count} 项")
        self._send_json(200, {"ok": True, "count": count})

    # ------- metadata 即时保存 -------
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
            _log(f"⚠️ 保存 metadata 失败: {exc}")
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
            _log(f"⚠️ update-paper 失败: {exc}")
            self._send_json(500, {"ok": False, "error": f"更新失败: {exc}"})
            return
        self._send_json(200, {"ok": True, "file_key": file_key})

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

        # 防止路径穿越：必须落在工作区目录内
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
                # 允许新进程把窗口提到前台（绕过 Windows 的前台锁机制）
                try:
                    import ctypes
                    ASFW_ANY = -1
                    ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)
                except Exception:
                    pass

                if os.path.isdir(abs_path):
                    os.startfile(abs_path)  # type: ignore[attr-defined]
                else:
                    # 打开文件所在文件夹并选中该文件
                    subprocess.Popen(
                        ["explorer", "/select,", abs_path],
                        close_fds=True,
                    )

                # 二次保险：稍等片刻把目标窗口提前
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

    def log_message(self, format, *args):  # 安静模式
        return

class PDFHandler(FileSystemEventHandler):
    """监听 papers/ 下任意 PDF 的新增 / 修改 / 删除 / 移动，触发 build。"""

    DEBOUNCE_SECONDS = 1.0

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._timer = None  # type: ignore[assignment]

    def _is_pdf(self, path: str) -> bool:
        return bool(path) and path.lower().endswith(".pdf")

    def _schedule(self, reason: str, path: str):
        # 仅打印相对路径，避免每行都过长
        try:
            short = os.path.relpath(path, WORKSPACE_ROOT)
        except ValueError:
            short = path
        _log(f"📄 PDF {reason}: {short}")
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
        _log("⚡ 执行 build.py")
        try:
            subprocess.run([sys.executable, BUILD_SCRIPT], check=False)
        except Exception as exc:
            _log(f"❌ build 失败: {exc}")

if __name__ == "__main__":
    # 确保工作目录始终是仓库根目录，这样 HTTP 服务器才能正确提供文件
    os.chdir(WORKSPACE_ROOT)
    
    # 解析端口参数：python waatchdog.py [--port 8001]
    port = HTTP_PORT
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg in ("--port", "-p") and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                print(f"⚠️ 无效端口: {argv[i + 1]}，使用默认 {HTTP_PORT}")
                port = HTTP_PORT
            break

    # 启动自定义 HTTP 服务器（提供静态文件 + /api/open-folder 接口）
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), PaperRequestHandler)
    except OSError as exc:
        print(f"❌ 无法绑定 127.0.0.1:{port} —— {exc}")
        if getattr(exc, "winerror", None) == 10048 or "Address already in use" in str(exc):
            print("   端口已被占用。常见原因：之前的 waatchdog.py 还在运行。")
            print("   解决办法：")
            print("     1) 关掉旧的 waatchdog 终端，或在 PowerShell 执行：")
            print(f"        Get-NetTCPConnection -LocalPort {port} | Select OwningProcess")
            print("        Stop-Process -Id <PID> -Force")
            print(f"     2) 或换个端口启动：python waatchdog.py --port {port + 1}")
        sys.exit(1)

    http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    http_thread.start()
    _log(f"🌐 HTTP server started on http://127.0.0.1:{port}")

    event_handler = PDFHandler()
    observer = Observer()
    observer.schedule(event_handler, PDF_DIR, recursive=True)
    observer.start()
    _log(f"🔔 监控启动: {PDF_DIR}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        observer.join()
        httpd.shutdown()
        httpd.server_close()
        _log("🛑 监控和 HTTP 服务器已停止")
