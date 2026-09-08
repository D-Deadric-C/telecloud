"""``telecloud.shared`` — common errors, models, and pure helpers (SPEC.md §6.2).

The shared vocabulary every other module builds on. Three groups:

* **Error model** (SPEC §5.1): :class:`TeleCloudError`, the reserved
  :class:`ErrorCode` set, and :data:`DEFAULT_STATUS`.
* **Read models** (SPEC §4, §5.3): :class:`UserContext`, :class:`FileMeta`,
  :class:`ChunkMeta`, :class:`FolderMeta`, :class:`ShareMeta`, plus the
  :class:`FileStatus` / :class:`ChunkStatus` enums they use.
* **Pure helpers** (SPEC §6.2): :func:`generate_token`, :func:`format_size`,
  :func:`compute_chunk_count`, :func:`locate_byte`.

This package is **pure** — no DB, Telegram, Redis, HTTP, or filesystem access —
and depends only on ``telecloud.config`` (SPEC §6.2).
"""

from telecloud.shared.errors import DEFAULT_STATUS, ErrorCode, TeleCloudError
from telecloud.shared.helpers import (
    compute_chunk_count,
    format_size,
    generate_token,
    locate_byte,
)
from telecloud.shared.models import (
    ChunkMeta,
    ChunkStatus,
    FileMeta,
    FileStatus,
    FolderMeta,
    ShareMeta,
    UserContext,
)

__all__ = [
    # errors
    "TeleCloudError",
    "ErrorCode",
    "DEFAULT_STATUS",
    # enums
    "FileStatus",
    "ChunkStatus",
    # models
    "UserContext",
    "FileMeta",
    "ChunkMeta",
    "FolderMeta",
    "ShareMeta",
    # helpers
    "generate_token",
    "format_size",
    "compute_chunk_count",
    "locate_byte",
]
