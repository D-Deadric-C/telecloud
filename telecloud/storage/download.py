"""The download side of the chunking engine — range-aware (SPEC.md §6.9, §7.2).

``open_download`` reassembles a committed file by streaming its chunks in order
straight from Telegram (no disk buffering, SPEC §1). It supports an optional HTTP
``Range`` so downloads are resumable (SPEC §7.2):

* **No range** → stream chunks ``0..n-1`` whole; a ``200`` with full
  ``Content-Length``.
* **With range** ``bytes=start-end`` → use the shared chunk-math
  (:func:`~telecloud.shared.locate_byte`) to find the starting chunk + intra-chunk
  offset, skip ``offset`` bytes, and keep streaming across chunk boundaries until
  ``end`` (inclusive) — a ``206`` with ``Content-Range``.

This module yields **bytes + metadata** only. It does not build a FastAPI response,
set status codes on the wire, check quota, or do auth (SPEC §6.9). It hands
``files/`` a :class:`DownloadResponse` carrying everything needed to frame the
response (``Content-Length`` / ``Content-Range`` / ``Accept-Ranges`` and the 200-vs-
206 choice); ``files/`` owns the actual ``Response`` and ``Content-Disposition``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol
from uuid import UUID

from telecloud import telegram
from telecloud.config import CHUNK_SIZE
from telecloud.database import Database, chunks_repo, files_repo
from telecloud.shared import (
    ChunkMeta,
    ErrorCode,
    FileStatus,
    TeleCloudError,
    locate_byte,
)


class _Transport(Protocol):
    """The slice of ``telegram/`` (SPEC §6.8) the download path uses."""

    def get_file_stream(
        self, channel_id: int, file_id: str, *, bot_id: str | None = None
    ) -> AsyncIterator[bytes]: ...


@dataclass(frozen=True)
class ByteRange:
    """A validated, inclusive byte range ``[start, end]`` within a file.

    ``end`` is inclusive (HTTP semantics), so a 1-byte range has
    ``start == end``. Construct validated ranges via :func:`parse_range` (from a
    ``Range`` header) or :func:`open_download` itself; the raw dataclass performs
    no bounds checking.
    """

    start: int
    end: int

    @property
    def length(self) -> int:
        """Number of bytes the range covers (``end - start + 1``)."""
        return self.end - self.start + 1


@dataclass(frozen=True)
class DownloadResponse:
    """Bytes + the metadata ``files/`` needs to frame an HTTP response (SPEC §7.2).

    Pure data — no FastAPI types. ``files/`` reads :attr:`status_code` /
    :attr:`headers` (or the individual fields) to build the actual response and to
    add ``Content-Disposition``, which is its concern, not storage's (SPEC §6.9).
    """

    stream: AsyncIterator[bytes]
    size_bytes: int
    content_length: int
    is_partial: bool
    content_range: str | None
    mime_type: str

    @property
    def status_code(self) -> int:
        """``206`` for a partial (range) response, else ``200`` (SPEC §7.2)."""
        return 206 if self.is_partial else 200

    @property
    def headers(self) -> dict[str, str]:
        """The response headers SPEC §7.2 prescribes (a convenience for ``files/``).

        Always advertises ``Accept-Ranges: bytes`` and the correct
        ``Content-Length``; adds ``Content-Range`` for a partial response.
        """
        headers = {
            "Content-Type": self.mime_type,
            "Content-Length": str(self.content_length),
            "Accept-Ranges": "bytes",
        }
        if self.content_range is not None:
            headers["Content-Range"] = self.content_range
        return headers


def _unsatisfiable(total_size: int) -> TeleCloudError:
    # SPEC §5.1 has no dedicated "range not satisfiable" code; we reuse
    # validation_error but with HTTP 416 (the correct status). Flagged in
    # storage/README.md in case a `range_not_satisfiable` code is wanted later.
    return TeleCloudError(
        ErrorCode.VALIDATION_ERROR,
        f"Requested range is not satisfiable for a {total_size}-byte file.",
        416,
    )


def _clamp_and_validate(start: int, end: int, total_size: int) -> ByteRange:
    """Clamp ``end`` to the file's last byte and reject an unsatisfiable range."""
    if end >= total_size:
        end = total_size - 1
    if start < 0 or start > end or start >= total_size:
        raise _unsatisfiable(total_size)
    return ByteRange(start, end)


def parse_range(value: str, total_size: int) -> ByteRange:
    """Parse a single HTTP ``Range`` header value into a validated :class:`ByteRange`.

    Accepts the forms ``bytes=start-end``, ``bytes=start-`` (to end of file), and
    ``bytes=-suffix`` (final ``suffix`` bytes), per SPEC §7.2. ``end`` is clamped
    to the last byte. Multiple comma-separated ranges are not supported.

    Raises :class:`~telecloud.shared.TeleCloudError` with ``validation_error`` —
    HTTP ``422`` for a malformed header, HTTP ``416`` for a well-formed but
    unsatisfiable range.
    """
    spec = value.strip()
    prefix = "bytes="
    if not spec.startswith(prefix):
        raise TeleCloudError(
            ErrorCode.VALIDATION_ERROR, "Malformed Range header.", 422
        )
    spec = spec[len(prefix):].strip()
    if "," in spec or "-" not in spec:
        raise TeleCloudError(
            ErrorCode.VALIDATION_ERROR,
            "Only a single byte range is supported.",
            422,
        )

    start_text, _, end_text = spec.partition("-")
    start_text, end_text = start_text.strip(), end_text.strip()
    try:
        if start_text == "":
            # Suffix range: bytes=-N → the last N bytes.
            if end_text == "":
                raise ValueError
            suffix = int(end_text)
            if suffix <= 0:
                raise _unsatisfiable(total_size)
            start = max(0, total_size - suffix)
            end = total_size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text != "" else total_size - 1
    except ValueError:
        raise TeleCloudError(
            ErrorCode.VALIDATION_ERROR, "Malformed Range header.", 422
        ) from None

    return _clamp_and_validate(start, end, total_size)


def _resolve_range(range_: "ByteRange | str", total_size: int) -> ByteRange:
    """Normalize the caller's ``range`` argument to a validated :class:`ByteRange`."""
    if isinstance(range_, str):
        return parse_range(range_, total_size)
    if isinstance(range_, ByteRange):
        return _clamp_and_validate(range_.start, range_.end, total_size)
    raise TeleCloudError(
        ErrorCode.VALIDATION_ERROR,
        "range must be a 'bytes=...' string or a ByteRange.",
        422,
    )


async def _stream_all(
    chunks: list[ChunkMeta], transport: _Transport
) -> AsyncIterator[bytes]:
    """Stream every chunk's bytes in order — a full (``200``) download."""
    for chunk in chunks:
        async for piece in transport.get_file_stream(
            chunk.channel_id, chunk.telegram_file_id, bot_id=chunk.bot_id
        ):
            yield piece


async def _stream_range(
    chunks_by_index: dict[int, ChunkMeta],
    byte_range: ByteRange,
    transport: _Transport,
    chunk_size: int,
) -> AsyncIterator[bytes]:
    """Stream exactly ``byte_range`` across one or more chunks (SPEC §7.2).

    Locates the starting chunk + intra-chunk offset via the shared chunk-math,
    skips ``offset`` bytes (which may span several streamed pieces), and keeps
    emitting across chunk boundaries until ``length`` bytes have been yielded —
    trimming the final piece so the response is byte-exact.
    """
    start_index, offset = locate_byte(byte_range.start, chunk_size)
    end_index, _ = locate_byte(byte_range.end, chunk_size)
    remaining = byte_range.length
    skip = offset

    for index in range(start_index, end_index + 1):
        chunk = chunks_by_index[index]
        async for piece in transport.get_file_stream(
            chunk.channel_id, chunk.telegram_file_id, bot_id=chunk.bot_id
        ):
            if skip:
                if len(piece) <= skip:
                    skip -= len(piece)
                    continue
                piece = piece[skip:]
                skip = 0
            if len(piece) >= remaining:
                yield piece[:remaining]
                return
            yield piece
            remaining -= len(piece)


async def open_download(
    db: Database,
    file_id: UUID,
    range_: "ByteRange | str | None" = None,
    *,
    transport: _Transport = telegram,
    chunk_size: int = CHUNK_SIZE,
) -> DownloadResponse:
    """Open a streaming download for a committed file, optionally ranged (SPEC §7.2).

    ``db`` is the caller's request-scoped :class:`~telecloud.database.Database`;
    ``file_id`` identifies the file. ``range_`` (SPEC's ``range``) is an optional
    ``bytes=...`` header string or a :class:`ByteRange`; ``None`` streams the whole
    file. ``transport`` and ``chunk_size`` are injectable for tests.

    Returns a :class:`DownloadResponse` (an async byte iterator plus framing
    metadata). Raises ``not_found`` if the file is missing or soft-deleted,
    ``upload_incomplete`` if it is still ``pending`` (SPEC §7.1), and
    ``validation_error`` (HTTP 416/422) for a bad range.
    """
    file = await files_repo.get(db, file_id)
    if file is None or file.deleted_at is not None:
        raise TeleCloudError.from_code(ErrorCode.NOT_FOUND, "File not found.")
    if file.status == FileStatus.PENDING:
        raise TeleCloudError.from_code(
            ErrorCode.UPLOAD_INCOMPLETE, "Upload is not yet complete."
        )
    if file.status != FileStatus.COMMITTED:
        raise TeleCloudError.from_code(ErrorCode.NOT_FOUND, "File not found.")

    chunks = await chunks_repo.list_for_file(db, file.id)
    total_size = file.size_bytes

    if range_ is None:
        return DownloadResponse(
            stream=_stream_all(chunks, transport),
            size_bytes=total_size,
            content_length=total_size,
            is_partial=False,
            content_range=None,
            mime_type=file.mime_type,
        )

    byte_range = _resolve_range(range_, total_size)
    chunks_by_index = {chunk.chunk_index: chunk for chunk in chunks}
    return DownloadResponse(
        stream=_stream_range(chunks_by_index, byte_range, transport, chunk_size),
        size_bytes=total_size,
        content_length=byte_range.length,
        is_partial=True,
        content_range=f"bytes {byte_range.start}-{byte_range.end}/{total_size}",
        mime_type=file.mime_type,
    )
