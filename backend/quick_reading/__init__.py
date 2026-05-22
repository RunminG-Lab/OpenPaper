"""Quick-reading public facade.

The package keeps the historical ``backend.quick_reading`` import path while
the implementation is split into focused modules.
"""

from .pipeline import generate_speedread, test_speedread_config
from .storage import (
    get_speedread_cache,
    list_speedread_cache,
    write_speedread_cache,
    rebuild_speedread_index,
)

__all__ = [
    "generate_speedread",
    "test_speedread_config",
    "get_speedread_cache",
    "list_speedread_cache",
    "write_speedread_cache",
    "rebuild_speedread_index",
]