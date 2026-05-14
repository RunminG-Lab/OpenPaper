"""Metadata I/O and recycle bin operations for OpenPaper."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections import namedtuple
from datetime import datetime
from urllib.parse import unquote

from backend.utils import log, safe_rel

_Result = namedtuple("_Result", ["status", "payload"])


# ---------------------------------------------------------------------------
# metadata file helpers
# ---------------------------------------------------------------------------

def load_metadata(metadata_file: str) -> dict:
    """Load metadata.json as a dict. Returns {} if missing or corrupt."""
    meta_abs = os.path.join(metadata_file)  # allow absolute or relative
    if not os.path.exists(meta_abs):
        return {}
    with open(meta_abs, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def atomic_write_metadata(data: dict, metadata_file: str) -> None:
    """Atomically write metadata.json via tmp + os.replace."""
    meta_abs = os.path.join(metadata_file)
    tmp_abs = meta_abs + ".tmp"
    with open(tmp_abs, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_abs, meta_abs)


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _slugify(s: str) -> str:
    s = re.sub(r"[^\w\-.]+", "_", s, flags=re.UNICODE)
    return s[:80] or "item"


def _read_recycle_info(rid: str, recycle_dir: str) -> tuple[str | None, dict | None]:
    """Return (item_dir, info_dict) or (None, None) on failure."""
    rid = (rid or "").strip()
    if not rid or "/" in rid or "\\" in rid or rid in (".", ".."):
        return None, None
    item_dir = os.path.join(recycle_dir, rid)
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


# ---------------------------------------------------------------------------
# recycle bin operations
# ---------------------------------------------------------------------------

def delete_paper(
    file_key: str,
    workspace_root: str,
    metadata_file: str,
    recycle_dir: str,
) -> _Result:
    """Move a paper PDF into .recycle_bin/ and store metadata snapshot."""
    # Resolve the original entry from metadata.json.
    meta_abs = os.path.join(workspace_root, metadata_file)
    try:
        with open(meta_abs, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception as exc:
        return _Result(500, {"ok": False, "error": f"读取 metadata.json 失败: {exc}"})

    entry = meta.get(file_key)
    if not entry:
        return _Result(404, {"ok": False, "error": f"metadata 中找不到 {file_key}"})

    # Recover the real relative disk path from the encoded pdf path.
    pdf_local = entry.get("pdf_local") or entry.get("pdf") or ""
    rel_disk = unquote(pdf_local)  # e.g. "papers/Embodied AI/foo.pdf"
    abs_pdf = safe_rel(rel_disk, workspace_root)
    if not abs_pdf or not os.path.isfile(abs_pdf):
        return _Result(404, {"ok": False, "error": f"文件不存在: {rel_disk}"})

    # Move the file into .recycle_bin/<id>/.
    recycle_root = os.path.join(workspace_root, recycle_dir)
    os.makedirs(recycle_root, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    rid = f"{ts}_{_slugify(os.path.splitext(os.path.basename(abs_pdf))[0])}"
    item_dir = os.path.join(recycle_root, rid)
    os.makedirs(item_dir, exist_ok=True)

    try:
        dest_pdf = os.path.join(item_dir, os.path.basename(abs_pdf))
        shutil.move(abs_pdf, dest_pdf)
    except Exception as exc:
        try:
            os.rmdir(item_dir)
        except Exception:
            pass
        return _Result(500, {"ok": False, "error": f"移动文件失败: {exc}"})

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
        log(f"写入回收站 info.json 失败: {exc}")

    log(f"已移入回收站: {rel_disk} -> {recycle_dir}/{rid}/")
    return _Result(200, {"ok": True, "id": rid, "title": info["title"]})


def list_recycle_bin(recycle_dir: str) -> _Result:
    """Return items in the recycle bin."""
    recycle_root = os.path.join(recycle_dir)
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
    return _Result(200, {"ok": True, "items": items})


def restore_paper(
    rid: str,
    workspace_root: str,
    build_script: str,
    metadata_file: str,
    recycle_dir: str,
) -> _Result:
    """Restore a paper from the recycle bin to its original location."""
    item_dir, info = _read_recycle_info(rid, recycle_dir)
    if not item_dir:
        return _Result(404, {"ok": False, "error": "回收站项目不存在"})
    if not info:
        return _Result(500, {"ok": False, "error": "info.json 缺失或损坏"})

    rel_disk = info.get("original_rel") or ""
    target_abs = safe_rel(rel_disk, workspace_root)
    if not target_abs:
        return _Result(400, {"ok": False, "error": "原始路径非法"})
    if os.path.exists(target_abs):
        return _Result(409, {"ok": False, "error": "目标位置已存在同名文件"})

    # Find the first PDF inside the recycle item directory.
    src_pdf = None
    for name in os.listdir(item_dir):
        if name.lower().endswith(".pdf"):
            src_pdf = os.path.join(item_dir, name)
            break
    if not src_pdf:
        return _Result(500, {"ok": False, "error": "回收站中找不到 PDF 文件"})

    os.makedirs(os.path.dirname(target_abs), exist_ok=True)
    try:
        shutil.move(src_pdf, target_abs)
    except Exception as exc:
        return _Result(500, {"ok": False, "error": f"恢复文件失败: {exc}"})

    # Rebuild immediately so metadata includes the restored file.
    try:
        subprocess.run([sys.executable, build_script], check=False, cwd=workspace_root)
    except Exception as exc:
        log(f"恢复后执行 build 失败: {exc}")

    # Merge saved metadata back in so notes/read/tags survive restore.
    saved_meta = info.get("metadata") or {}
    file_key = info.get("file_key") or saved_meta.get("file_key")
    if file_key and saved_meta:
        meta_abs = os.path.join(workspace_root, metadata_file)
        try:
            with open(meta_abs, "r", encoding="utf-8") as f:
                meta = json.load(f)
            # Restore user fields, but keep build-generated path fields.
            current = meta.get(file_key) or {}
            merged = {**current, **saved_meta}
            # Trust build output for path casing and encoding.
            if current.get("pdf"):
                merged["pdf"] = current["pdf"]
            if current.get("pdf_local"):
                merged["pdf_local"] = current["pdf_local"]
            meta[file_key] = merged
            with open(meta_abs, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            log(f"合并恢复 metadata 失败: {exc}")

    # Remove the recycle item directory after restore.
    try:
        shutil.rmtree(item_dir)
    except Exception as exc:
        log(f"清理回收站目录失败: {exc}")

    log(f"已恢复: {rel_disk}")
    return _Result(200, {"ok": True, "file_key": file_key})


def purge_paper(rid: str, recycle_dir: str) -> _Result:
    """Permanently delete a single recycle bin item."""
    item_dir, _ = _read_recycle_info(rid, recycle_dir)
    if not item_dir:
        return _Result(404, {"ok": False, "error": "回收站项目不存在"})
    try:
        shutil.rmtree(item_dir)
    except Exception as exc:
        return _Result(500, {"ok": False, "error": f"删除失败: {exc}"})
    log(f"已永久删除回收站项目: {rid}")
    return _Result(200, {"ok": True})


def purge_all_papers(recycle_dir: str) -> _Result:
    """Permanently delete all recycle bin items."""
    recycle_root = os.path.join(recycle_dir)
    count = 0
    if os.path.isdir(recycle_root):
        for name in os.listdir(recycle_root):
            d = os.path.join(recycle_root, name)
            if os.path.isdir(d):
                try:
                    shutil.rmtree(d)
                    count += 1
                except Exception as exc:
                    log(f"清空回收站失败 {name}: {exc}")
    log(f"已清空回收站，共 {count} 项")
    return _Result(200, {"ok": True, "count": count})


# ---------------------------------------------------------------------------
# metadata persistence operations
# ---------------------------------------------------------------------------

def save_metadata(data: dict, metadata_file: str) -> _Result:
    """Validate and atomically save full metadata.json."""
    if not isinstance(data, dict):
        return _Result(400, {"ok": False, "error": "缺少 data 字段或类型不正确"})
    try:
        atomic_write_metadata(data, metadata_file)
    except Exception as exc:
        log(f"保存 metadata 失败: {exc}")
        return _Result(500, {"ok": False, "error": f"写入失败: {exc}"})
    return _Result(200, {"ok": True, "count": len(data)})


def update_paper(
    file_key: str,
    fields: dict,
    metadata_file: str,
) -> _Result:
    """Patch specific fields on a paper entry in metadata.json."""
    if not file_key or not isinstance(fields, dict):
        return _Result(400, {"ok": False, "error": "缺少 file_key 或 fields"})
    meta_abs = os.path.join(metadata_file)
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
        atomic_write_metadata(meta, metadata_file)
    except Exception as exc:
        log(f"update-paper 失败: {exc}")
        return _Result(500, {"ok": False, "error": f"更新失败: {exc}"})
    return _Result(200, {"ok": True, "file_key": file_key})
