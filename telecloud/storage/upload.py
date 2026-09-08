"""The upload side of the chunking engine — two-phase commit (SPEC.md §6.9, §7.1).

``store_upload`` takes an already-created ``pending`` file row (``files/`` opens it
after ``quota.check_can_upload``, SPEC §7.1 step 3) and an inbound byte stream. It:

1. re-chunks the stream into fixed **18 MiB** pieces (last may be smaller, SPEC §1),
2. for each piece, sends the bytes to Telegram and records a ``pending`` ``chunks``
   row with the identifiers the transport reports back (SPEC §7.1 step 4),
3. once **all** chunks land, performs the commit: flip every chunk to ``committed``
   then flip the file to ``committed`` (SPEC §7.1 step 5).

No disk buffering — at most two 18 MiB pieces are held in memory while re-chunking
(SPEC §1). It never checks quota, does auth, or builds HTTP responses (SPEC §6.9):
the quota increment that SPEC §7.1 step 5 lists alongside the status flips is left
to ``files/`` (see ``storage/README.md`` "Flagged contract notes").

On a mid-stream failure (a Telegram send or a DB insert raising) the exception
propagates and the file stays ``pending``; the ``jobs/`` orphan sweeper reclaims the
abandoned Telegram messages + rows later (SPEC §7.1 step 6, §7.4). ``store_upload``
deliberately does **not** roll back here.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol

from telecloud import telegram
from telecloud.config import CHUNK_SIZE
from telecloud.database import Database, chunks_repo, files_repo
from telecloud.shared import ErrorCode, FileMeta, TeleCloudError


class _Transport(Protocol):
    """The slice of ``telegram/`` (SPEC §6.8) the upload path uses."""

    async def send_document(self, channel_id: int | None, data: bytes) -> Any: ...


async def _iter_fixed_chunks(
    stream: AsyncIterator[bytes], chunk_size: int
) -> AsyncIterator[bytes]:
    """Re-chunk an arbitrary byte ``stream`` into fixed ``chunk_size`` pieces.

    The inbound stream (e.g. FastAPI's request body iterator) yields
    arbitrarily-sized pieces; chunk boundaries must be exact so the Range math on
    download (SPEC §7.2) lines up. We buffer only up to one full chunk plus the
    current inbound piece — never the whole file — to honor "no disk buffering"
    and bounded memory (SPEC §1). The final piece may be smaller than
    ``chunk_size``; a zero-byte stream yields nothing.
    """
    buffer = bytearray()
    async for data in stream:
        if not data:
            continue
        buffer.extend(data)
        while len(buffer) >= chunk_size:
            yield bytes(buffer[:chunk_size])
            del buffer[:chunk_size]
    if buffer:
        yield bytes(buffer)


async def store_upload(
    db: Database,
    file_meta: FileMeta,
    stream: AsyncIterator[bytes],
    *,
    transport: _Transport = telegram,
    chunk_size: int = CHUNK_SIZE,
) -> FileMeta:
    """Chunk ``stream`` to Telegram and two-phase-commit the ``pending`` file.

    ``file_meta`` is the ``pending`` ``files`` row created by ``files/`` (SPEC §7.1
    step 3); ``stream`` is an async iterator of bytes. Returns the now-``committed``
    :class:`~telecloud.shared.FileMeta`.

    ``db`` is the caller's request-scoped :class:`~telecloud.database.Database`
    (storage cannot mint one — that needs the user's JWT, an ``auth``/``files``
    concern; see ``storage/README.md``). ``transport`` defaults to the ``telegram``
    module and ``chunk_size`` to :data:`~telecloud.config.CHUNK_SIZE`; both are
    injectable for tests.

    Raises whatever ``telegram`` or the repositories raise on failure, leaving the
    file ``pending`` for the ``jobs/`` sweeper (SPEC §7.1 step 6).
    """
    chunk_index = 0
    async for piece in _iter_fixed_chunks(stream, chunk_size):
        # Channel is None → the bot pool picks one and reports it back, since
        # chunks are channel-aware (SPEC §1, §4.4, §6.8).
        result = await transport.send_document(None, piece)
        await chunks_repo.insert_pending(
            db,
            file_id=file_meta.id,
            chunk_index=chunk_index,
            size_bytes=len(piece),
            channel_id=result.channel_id,
            message_id=result.message_id,
            telegram_file_id=result.telegram_file_id,
            bot_id=result.bot_id,
        )
        chunk_index += 1

    # Commit phase (SPEC §7.1 step 5). Chunks are flipped first and the file row
    # last, so the file — the commit marker — only becomes visible once its chunks
    # are committed. See storage/README.md for why this is sequential, not a single
    # DB transaction, and why quota.add_usage is left to files/.
    await chunks_repo.mark_all_committed(db, file_meta.id)
    committed = await files_repo.mark_committed(db, file_meta.id)
    if committed is None:
        raise TeleCloudError(
            ErrorCode.INTERNAL_ERROR,
            "File row disappeared during commit.",
            500,
        )
    return committed
