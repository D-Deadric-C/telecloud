"""``telecloud.storage`` — the chunking engine (SPEC.md §6.9).

Splits an upload stream into fixed **18 MiB** chunks driving the two-phase commit
(SPEC §7.1), and streams downloads back in order with HTTP ``Range`` support
(SPEC §7.2). It yields **bytes + metadata** straight from/to Telegram with no disk
buffering (SPEC §1); it does **not** check quota, do auth, or build HTTP responses
— ``files/`` wraps these and owns those concerns (SPEC §6.9).

Public surface:

* :func:`store_upload` — chunk a stream to Telegram and commit the pending file.
* :func:`open_download` — open a (optionally ranged) streaming download.
* :class:`DownloadResponse` — the byte iterator + framing metadata ``files/`` uses.
* :class:`ByteRange` / :func:`parse_range` — the validated range value object and
  a ``Range``-header parser ``files/`` may reuse.

Depends only on ``config``, ``shared``, ``database``, and ``telegram`` (SPEC §6.9).
See ``storage/README.md`` for the flagged contract notes (threaded ``db``, the
non-atomic commit, and the missing ``range_not_satisfiable`` error code).
"""

from telecloud.storage.download import (
    ByteRange,
    DownloadResponse,
    open_download,
    parse_range,
)
from telecloud.storage.upload import store_upload

__all__ = [
    "store_upload",
    "open_download",
    "DownloadResponse",
    "ByteRange",
    "parse_range",
]
