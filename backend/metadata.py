"""Metadata I/O and recycle bin operations for OpenPaper."""

from __future__ import annotations

import hashlib
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
STATS_SOURCE_FILENAME = "stats.data.json"


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


def _atomic_write_json(data: dict, target_file: str) -> None:
    target_abs = os.path.join(target_file)
    tmp_abs = target_abs + ".tmp"
    with open(tmp_abs, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_abs, target_abs)


def _build_stats_source_payload(metadata: dict) -> dict:
    papers = []
    for file_key in sorted(metadata):
        paper = metadata.get(file_key)
        if not isinstance(paper, dict):
            continue
        tags = paper.get("tags")
        if not isinstance(tags, list):
            tags = []
        papers.append({
            "file_key": paper.get("file_key") or file_key,
            "title": paper.get("title", ""),
            "authors": paper.get("authors", ""),
            "year": paper.get("year", ""),
            "venue": paper.get("venue", ""),
            "tags": tags,
            "pdf": paper.get("pdf", ""),
            "pdf_local": paper.get("pdf_local", ""),
            "read": bool(paper.get("read", False)),
            "added_at": paper.get("added_at", ""),
        })
    return {
        "version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "paper_count": len(papers),
        "papers": papers,
    }


def write_stats_source(metadata: dict, metadata_file: str) -> str:
    """Write stats.data.json next to metadata.json for dashboard sync."""
    meta_abs = os.path.abspath(os.path.join(metadata_file))
    stats_file = os.path.join(os.path.dirname(meta_abs), STATS_SOURCE_FILENAME)
    _atomic_write_json(_build_stats_source_payload(metadata), stats_file)
    return stats_file


def atomic_write_metadata(data: dict, metadata_file: str) -> None:
    """Atomically write metadata.json via tmp + os.replace."""
    meta_abs = os.path.join(metadata_file)
    _atomic_write_json(data, meta_abs)
    write_stats_source(data, meta_abs)


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


def _recycle_item_type(info: dict | None) -> str:
    item_type = (info or {}).get("item_type")
    if item_type in ("paper", "speedread"):
        return item_type
    return "paper"


def _speedread_cache_root(workspace_root: str, quick_reading_cache_dir: str) -> str:
    if os.path.isabs(quick_reading_cache_dir):
        return quick_reading_cache_dir
    return os.path.join(workspace_root, quick_reading_cache_dir)


def _speedread_cache_hash(file_key: str) -> str:
    return hashlib.sha1(file_key.encode("utf-8")).hexdigest()[:16]


def _speedread_cache_dir(file_key: str, workspace_root: str, quick_reading_cache_dir: str) -> str:
    return os.path.join(_speedread_cache_root(workspace_root, quick_reading_cache_dir), _speedread_cache_hash(file_key))


def _remove_speedread_index_entry(file_key: str, workspace_root: str, quick_reading_cache_dir: str) -> None:
    index_file = os.path.join(_speedread_cache_root(workspace_root, quick_reading_cache_dir), "index.json")
    try:
        with open(index_file, "r", encoding="utf-8") as f:
            index = json.load(f)
    except Exception:
        return

    items = index.get("items") if isinstance(index, dict) else {}
    if not isinstance(items, dict) or file_key not in items:
        return

    items.pop(file_key, None)
    payload = {
        "version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "items": items,
    }
    _atomic_write_json(payload, index_file)


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
    meta_abs = os.path.join(workspace_root, metadata_file)
    try:
        with open(meta_abs, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception as exc:
        return _Result(500, {"ok": False, "error": f"读取 metadata.json 失败: {exc}"})

    entry = meta.get(file_key)
    if not entry:
        return _Result(404, {"ok": False, "error": f"metadata 中找不到 {file_key}"})

    pdf_local = entry.get("pdf_local") or entry.get("pdf") or ""
    rel_disk = unquote(pdf_local)
    abs_pdf = safe_rel(rel_disk, workspace_root)
    if not abs_pdf or not os.path.isfile(abs_pdf):
        return _Result(404, {"ok": False, "error": f"文件不存在: {rel_disk}"})

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
        "item_type": "paper",
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
    return _Result(200, {"ok": True, "id": rid, "title": info["title"], "item_type": "paper"})


def delete_speedread(
    file_key: str,
    workspace_root: str,
    metadata_file: str,
    quick_reading_cache_dir: str,
    recycle_dir: str,
) -> _Result:
    """Move a paper speed-read cache directory into .recycle_bin/."""
    file_key = (file_key or "").strip()
    if not file_key:
        return _Result(400, {"ok": False, "error": "缺少 file_key"})

    cache_dir = _speedread_cache_dir(file_key, workspace_root, quick_reading_cache_dir)
    cache_file = os.path.join(cache_dir, "speed_read.json")
    if not os.path.isdir(cache_dir) or not os.path.isfile(cache_file):
        return _Result(404, {"ok": False, "error": f"速读缓存不存在: {file_key}"})

    metadata = load_metadata(metadata_file)
    entry = metadata.get(file_key) if isinstance(metadata, dict) else {}
    title = ""
    if isinstance(entry, dict):
        title = (entry.get("title") or "").strip()
    if not title:
        title = file_key

    recycle_root = os.path.join(workspace_root, recycle_dir)
    os.makedirs(recycle_root, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    rid = f"{ts}_{_slugify(title)}_speedread"
    item_dir = os.path.join(recycle_root, rid)
    os.makedirs(item_dir, exist_ok=True)

    target_dir = os.path.join(item_dir, os.path.basename(cache_dir))
    try:
        shutil.move(cache_dir, target_dir)
        _remove_speedread_index_entry(file_key, workspace_root, quick_reading_cache_dir)
    except Exception as exc:
        try:
            if os.path.isdir(target_dir):
                shutil.move(target_dir, cache_dir)
            os.rmdir(item_dir)
        except Exception:
            pass
        return _Result(500, {"ok": False, "error": f"移动速读缓存失败: {exc}"})

    original_rel = os.path.relpath(cache_dir, workspace_root).replace(os.sep, "/")
    info = {
        "id": rid,
        "item_type": "speedread",
        "file_key": file_key,
        "original_rel": original_rel,
        "deleted_at": datetime.now().isoformat(timespec="seconds"),
        "title": title,
    }
    try:
        with open(os.path.join(item_dir, "info.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        log(f"写入速读回收站 info.json 失败: {exc}")

    log(f"已移入回收站: {original_rel} -> {recycle_dir}/{rid}/")
    return _Result(200, {"ok": True, "id": rid, "title": title, "item_type": "speedread"})


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
                item_type = _recycle_item_type(info)
                items.append({
                    "id": info.get("id", name),
                    "title": info.get("title", ""),
                    "original_rel": info.get("original_rel", ""),
                    "deleted_at": info.get("deleted_at", ""),
                    "file_key": info.get("file_key", ""),
                    "item_type": item_type,
                })
            except Exception:
                continue
    return _Result(200, {"ok": True, "items": items})


def _restore_paper_item(
    item_dir: str,
    info: dict,
    workspace_root: str,
    build_script: str,
    metadata_file: str,
) -> _Result:
    rel_disk = info.get("original_rel") or ""
    target_abs = safe_rel(rel_disk, workspace_root)
    if not target_abs:
        return _Result(400, {"ok": False, "error": "原始路径非法"})
    if os.path.exists(target_abs):
        return _Result(409, {"ok": False, "error": "目标位置已存在同名文件"})

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

    try:
        subprocess.run([sys.executable, build_script], check=False, cwd=workspace_root)
    except Exception as exc:
        log(f"恢复后执行 build 失败: {exc}")

    saved_meta = info.get("metadata") or {}
    file_key = info.get("file_key") or saved_meta.get("file_key")
    if file_key and saved_meta:
        meta_abs = os.path.join(workspace_root, metadata_file)
        try:
            with open(meta_abs, "r", encoding="utf-8") as f:
                meta = json.load(f)
            current = meta.get(file_key) or {}
            merged = {**current, **saved_meta}
            if current.get("pdf"):
                merged["pdf"] = current["pdf"]
            if current.get("pdf_local"):
                merged["pdf_local"] = current["pdf_local"]
            meta[file_key] = merged
            atomic_write_metadata(meta, meta_abs)
        except Exception as exc:
            log(f"合并恢复 metadata 失败: {exc}")

    try:
        shutil.rmtree(item_dir)
    except Exception as exc:
        log(f"清理回收站目录失败: {exc}")

    log(f"已恢复: {rel_disk}")
    return _Result(200, {"ok": True, "file_key": file_key, "item_type": "paper"})


def _restore_speedread_item(
    item_dir: str,
    info: dict,
    workspace_root: str,
    metadata_file: str,
    quick_reading_cache_dir: str,
) -> _Result:
    rel_cache_dir = info.get("original_rel") or ""
    target_abs = safe_rel(rel_cache_dir, workspace_root)
    if not target_abs:
        return _Result(400, {"ok": False, "error": "原始速读路径非法"})
    if os.path.exists(target_abs):
        return _Result(409, {"ok": False, "error": "目标位置已存在同名缓存"})

    src_dir = None
    for name in os.listdir(item_dir):
        if name == "info.json":
            continue
        candidate = os.path.join(item_dir, name)
        if os.path.isdir(candidate):
            src_dir = candidate
            break
    if not src_dir:
        return _Result(500, {"ok": False, "error": "回收站中找不到速读缓存目录"})

    os.makedirs(os.path.dirname(target_abs), exist_ok=True)
    try:
        shutil.move(src_dir, target_abs)
    except Exception as exc:
        return _Result(500, {"ok": False, "error": f"恢复速读缓存失败: {exc}"})

    try:
        from backend.quick_reading import rebuild_speedread_index

        file_keys = list(load_metadata(metadata_file).keys())
        file_key = (info.get("file_key") or "").strip()
        if file_key and file_key not in file_keys:
            file_keys.append(file_key)
        rebuild_speedread_index(file_keys, workspace_root, quick_reading_cache_dir)
    except Exception as exc:
        log(f"恢复速读后重建索引失败: {exc}")

    try:
        shutil.rmtree(item_dir)
    except Exception as exc:
        log(f"清理速读回收站目录失败: {exc}")

    log(f"已恢复速读: {rel_cache_dir}")
    return _Result(200, {"ok": True, "file_key": info.get("file_key") or "", "item_type": "speedread"})


def restore_recycle_item(
    rid: str,
    workspace_root: str,
    build_script: str,
    metadata_file: str,
    recycle_dir: str,
    quick_reading_cache_dir: str,
) -> _Result:
    """Restore a paper or speed-read item from the recycle bin."""
    item_dir, info = _read_recycle_info(rid, recycle_dir)
    if not item_dir:
        return _Result(404, {"ok": False, "error": "回收站项目不存在"})
    if not info:
        return _Result(500, {"ok": False, "error": "info.json 缺失或损坏"})

    if _recycle_item_type(info) == "speedread":
        return _restore_speedread_item(item_dir, info, workspace_root, metadata_file, quick_reading_cache_dir)
    return _restore_paper_item(item_dir, info, workspace_root, build_script, metadata_file)


def restore_paper(
    rid: str,
    workspace_root: str,
    build_script: str,
    metadata_file: str,
    recycle_dir: str,
    quick_reading_cache_dir: str = "",
) -> _Result:
    """Backward-compatible wrapper for recycle restore."""
    if quick_reading_cache_dir:
        return restore_recycle_item(rid, workspace_root, build_script, metadata_file, recycle_dir, quick_reading_cache_dir)

    item_dir, info = _read_recycle_info(rid, recycle_dir)
    if not item_dir:
        return _Result(404, {"ok": False, "error": "回收站项目不存在"})
    if not info:
        return _Result(500, {"ok": False, "error": "info.json 缺失或损坏"})
    if _recycle_item_type(info) != "paper":
        return _Result(400, {"ok": False, "error": "该回收站项目不是论文"})
    return _restore_paper_item(item_dir, info, workspace_root, build_script, metadata_file)


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
    for entry in data.values():
        if isinstance(entry, dict):
            entry.pop("speed_read", None)
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
        merged.pop("speed_read", None)
        merged["file_key"] = file_key
        meta[file_key] = merged
        atomic_write_metadata(meta, metadata_file)
    except Exception as exc:
        log(f"update-paper 失败: {exc}")
        return _Result(500, {"ok": False, "error": f"更新失败: {exc}"})
    return _Result(200, {"ok": True, "file_key": file_key})
