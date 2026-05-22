from __future__ import annotations

import hashlib
import json
import os

from backend.utils import log

from .models import _Result, now_iso


def paper_hash(file_key: str) -> str:
    return hashlib.sha1(file_key.encode("utf-8")).hexdigest()[:16]


def cache_root(workspace_root: str, quick_reading_cache_dir: str) -> str:
    if os.path.isabs(quick_reading_cache_dir):
        return quick_reading_cache_dir
    return os.path.join(workspace_root, quick_reading_cache_dir)


def paper_cache_dir(file_key: str, workspace_root: str, quick_reading_cache_dir: str) -> str:
    return os.path.join(cache_root(workspace_root, quick_reading_cache_dir), paper_hash(file_key))


def speedread_cache_file(file_key: str, workspace_root: str, quick_reading_cache_dir: str) -> str:
    return os.path.join(paper_cache_dir(file_key, workspace_root, quick_reading_cache_dir), "speed_read.json")


def speedread_cache_relpath(file_key: str, workspace_root: str, quick_reading_cache_dir: str) -> str:
    return os.path.relpath(speedread_cache_file(file_key, workspace_root, quick_reading_cache_dir), workspace_root).replace(os.sep, "/")


def speedread_index_file(workspace_root: str, quick_reading_cache_dir: str) -> str:
    return os.path.join(cache_root(workspace_root, quick_reading_cache_dir), "index.json")


def _speedread_index_item(file_key: str, speed_read: dict, workspace_root: str, quick_reading_cache_dir: str) -> dict:
    return {
        "path": speedread_cache_relpath(file_key, workspace_root, quick_reading_cache_dir),
        "status": speed_read.get("status", "success"),
        "generated_at": speed_read.get("generated_at", ""),
        "updated_at": speed_read.get("updated_at", ""),
        "model": speed_read.get("model", ""),
        "request_mode": speed_read.get("request_mode", ""),
        "version": speed_read.get("version", 1),
    }


def _write_speedread_index(items: dict, workspace_root: str, quick_reading_cache_dir: str) -> None:
    index_file = speedread_index_file(workspace_root, quick_reading_cache_dir)
    os.makedirs(os.path.dirname(index_file), exist_ok=True)
    payload = {
        "version": 1,
        "generated_at": now_iso(),
        "items": items,
    }
    tmp_file = index_file + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as file_obj:
        json.dump(payload, file_obj, ensure_ascii=False, indent=2)
    os.replace(tmp_file, index_file)


def _update_speedread_index(file_key: str, speed_read: dict, workspace_root: str, quick_reading_cache_dir: str) -> None:
    index_file = speedread_index_file(workspace_root, quick_reading_cache_dir)
    try:
        with open(index_file, "r", encoding="utf-8") as file_obj:
            index = json.load(file_obj)
        items = index.get("items") if isinstance(index, dict) else {}
        if not isinstance(items, dict):
            items = {}
    except Exception:
        items = {}
    items[file_key] = _speedread_index_item(file_key, speed_read, workspace_root, quick_reading_cache_dir)
    _write_speedread_index(items, workspace_root, quick_reading_cache_dir)


def rebuild_speedread_index(file_keys: list, workspace_root: str, quick_reading_cache_dir: str) -> None:
    items = {}
    for file_key in file_keys:
        if not isinstance(file_key, str) or not file_key:
            continue
        cache_file = speedread_cache_file(file_key, workspace_root, quick_reading_cache_dir)
        if not os.path.isfile(cache_file):
            continue
        try:
            with open(cache_file, "r", encoding="utf-8") as file_obj:
                speed_read = json.load(file_obj)
        except Exception:
            speed_read = {}
        if not isinstance(speed_read, dict):
            speed_read = {}
        items[file_key] = _speedread_index_item(file_key, speed_read, workspace_root, quick_reading_cache_dir)
    _write_speedread_index(items, workspace_root, quick_reading_cache_dir)


def load_speedread_cache(file_key: str, workspace_root: str, quick_reading_cache_dir: str) -> dict:
    cache_file = speedread_cache_file(file_key, workspace_root, quick_reading_cache_dir)
    if not os.path.isfile(cache_file):
        return {}
    with open(cache_file, "r", encoding="utf-8") as file_obj:
        data = json.load(file_obj)
    return data if isinstance(data, dict) else {}


def write_speedread_cache(file_key: str, speed_read: dict, workspace_root: str, quick_reading_cache_dir: str) -> str:
    cache_file = speedread_cache_file(file_key, workspace_root, quick_reading_cache_dir)
    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
    tmp_file = cache_file + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as file_obj:
        json.dump(speed_read, file_obj, ensure_ascii=False, indent=2)
    os.replace(tmp_file, cache_file)
    _update_speedread_index(file_key, speed_read, workspace_root, quick_reading_cache_dir)
    return os.path.relpath(cache_file, workspace_root).replace(os.sep, "/")


def get_speedread_cache(file_key: str, workspace_root: str, quick_reading_cache_dir: str) -> _Result:
    if not file_key:
        return _Result(400, {"ok": False, "error": "缺少 file_key"})
    try:
        speed_read = load_speedread_cache(file_key, workspace_root, quick_reading_cache_dir)
    except Exception as exc:
        return _Result(500, {"ok": False, "error": f"读取速读缓存失败: {exc}"})
    if not speed_read:
        return _Result(404, {"ok": False, "error": "速读缓存不存在"})
    return _Result(200, {"ok": True, "speed_read": speed_read})


def list_speedread_cache(file_keys: list, workspace_root: str, quick_reading_cache_dir: str) -> _Result:
    if not isinstance(file_keys, list):
        return _Result(400, {"ok": False, "error": "file_keys 格式不正确"})
    items = {}
    for file_key in file_keys:
        if not isinstance(file_key, str) or not file_key:
            continue
        try:
            speed_read = load_speedread_cache(file_key, workspace_root, quick_reading_cache_dir)
        except Exception as exc:
            log(f"读取速读缓存失败 {file_key}: {exc}")
            continue
        if speed_read:
            items[file_key] = speed_read
    return _Result(200, {"ok": True, "speed_reads": items})