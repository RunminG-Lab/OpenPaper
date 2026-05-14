"""Shared utilities for the OpenPaper backend."""

from __future__ import annotations

import os
import sys
from datetime import datetime


def configure_stdio() -> None:
    """Configure stdout/stderr to use UTF-8 encoding."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if not stream:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def log(msg: str) -> None:
    """Write a timestamped log line to stdout."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def resolve_workspace_root() -> str:
    """Detect the repository root from the location of this file.

    When running from backend/utils.py (inside backend/), the workspace
    root is one directory up. When running from a relocated backend
    directory, the check on the basename still works.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(script_dir) == "backend":
        return os.path.dirname(script_dir)
    return script_dir


def safe_rel(rel: str, workspace_root: str) -> str | None:
    """Normalize a relative path and ensure it stays inside the workspace.

    Returns the absolute path if valid, or None if the path escapes
    the workspace root.
    """
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel:
        return None
    abs_path = os.path.abspath(os.path.join(workspace_root, rel))
    try:
        common = os.path.commonpath([workspace_root, abs_path])
    except ValueError:
        return None
    if common != workspace_root:
        return None
    return abs_path
