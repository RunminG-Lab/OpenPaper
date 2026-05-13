"""File system watcher for OpenPaper — monitors papers/ for PDF changes."""

import os
import threading
from typing import Callable

from watchdog.events import FileSystemEventHandler

from backend.utils import log


class PDFHandler(FileSystemEventHandler):
    """Watch PDF changes under papers/ and debounce rebuilds."""

    DEBOUNCE_SECONDS = 1.0

    def __init__(self, build_callback: Callable[[], None], workspace_root: str):
        super().__init__()
        self._build_callback = build_callback
        self._workspace_root = workspace_root
        self._lock = threading.Lock()
        self._timer = None  # type: ignore[assignment]

    def _is_pdf(self, path: str) -> bool:
        return bool(path) and path.lower().endswith(".pdf")

    def _schedule(self, reason: str, path: str):
        try:
            short = os.path.relpath(path, self._workspace_root)
        except ValueError:
            short = path
        log(f"PDF {reason}: {short}")
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.DEBOUNCE_SECONDS, self._build_callback)
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
