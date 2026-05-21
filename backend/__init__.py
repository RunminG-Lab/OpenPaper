"""OpenPaper — local paper management and AI speed-read tool."""

__version__ = "1.0.0"

from backend.utils import configure_stdio, log, resolve_workspace_root, safe_rel
from backend.metadata import (
    load_metadata, atomic_write_metadata,
    delete_paper, delete_speedread, list_recycle_bin, restore_paper, restore_recycle_item, purge_paper, purge_all_papers,
    save_metadata, update_paper,
)
from backend.quick_reading import generate_speedread, test_speedread_config
from backend.watcher import PDFHandler

__all__ = [
    "__version__",
    "configure_stdio", "log", "resolve_workspace_root", "safe_rel",
    "load_metadata", "atomic_write_metadata",
    "delete_paper", "delete_speedread", "list_recycle_bin", "restore_paper", "restore_recycle_item", "purge_paper", "purge_all_papers",
    "save_metadata", "update_paper",
    "generate_speedread", "test_speedread_config",
    "PDFHandler",
]
