"""Data access for ``public.chunks`` (SPEC §4.4) — channel-aware chunk rows.

One row per 18 MiB piece, carrying the Telegram coordinates needed to fetch or
delete the bytes (``channel_id``, ``message_id``, ``telegram_file_id``,
``bot_id``). Supports the two-phase upload (insert pending, then commit all;
SPEC §7.1), ordered streaming for downloads (SPEC §7.2), and cleanup (SPEC §7.4).
Rows are returned as :class:`ChunkMeta`. This repo records identifiers only — it
never talks to Telegram (that's ``telegram/``).
"""

from __future__ import annotations

from uuid import UUID

from telecloud.shared import ChunkMeta, ChunkStatus

from telecloud.database._encoding import to_jsonable
from telecloud.database.client import Database
from telecloud.database.repositories._common import first, rows

_TABLE = "chunks"
_COLUMNS = (
    "id, file_id, chunk_index, size_bytes, channel_id, message_id, "
    "telegram_file_id, bot_id, status, created_at"
)


async def insert_pending(
    db: Database,
    *,
    file_id: UUID,
    chunk_index: int,
    size_bytes: int,
    channel_id: int,
    message_id: int,
    telegram_file_id: str,
    bot_id: str,
) -> ChunkMeta:
    """Insert a ``pending`` chunk row after its bytes are sent to Telegram.

    Called once per 18 MiB piece during the upload's first phase (SPEC §7.1) with
    the identifiers ``telegram.send_document`` returned. ``chunk_index`` is
    0-based; ``(file_id, chunk_index)`` is unique.
    """
    payload = to_jsonable(
        {
            "file_id": file_id,
            "chunk_index": chunk_index,
            "size_bytes": size_bytes,
            "channel_id": channel_id,
            "message_id": message_id,
            "telegram_file_id": telegram_file_id,
            "bot_id": bot_id,
            "status": ChunkStatus.PENDING,
        }
    )
    row = first(await db.table(_TABLE).insert(payload).execute())
    assert row is not None
    return ChunkMeta.model_validate(row)


async def list_for_file(db: Database, file_id: UUID) -> list[ChunkMeta]:
    """Return all chunks of a file ordered by ``chunk_index`` (0..n-1).

    Ordered streaming for downloads (SPEC §7.2) and cleanup of a file's Telegram
    messages (SPEC §7.4) both rely on this order.
    """
    result = await (
        db.table(_TABLE)
        .select(_COLUMNS)
        .eq("file_id", str(file_id))
        .order("chunk_index")
        .execute()
    )
    return [ChunkMeta.model_validate(row) for row in rows(result)]


async def get_by_index(
    db: Database, *, file_id: UUID, chunk_index: int
) -> ChunkMeta | None:
    """Return a single chunk by ``(file_id, chunk_index)``, or ``None``.

    Range downloads map a start byte to a ``chunk_index`` (SPEC §7.2) and fetch
    just that chunk to begin streaming.
    """
    row = first(
        await db.table(_TABLE)
        .select(_COLUMNS)
        .eq("file_id", str(file_id))
        .eq("chunk_index", chunk_index)
        .limit(1)
        .execute()
    )
    return ChunkMeta.model_validate(row) if row else None


async def mark_all_committed(db: Database, file_id: UUID) -> list[ChunkMeta]:
    """Flip every chunk of a file to ``committed`` — the commit step (SPEC §7.1).

    Paired with ``files_repo.mark_committed`` by the two-phase orchestration in
    ``storage/``/``files/``. Returns the updated chunk rows.
    """
    result = await (
        db.table(_TABLE)
        .update({"status": ChunkStatus.COMMITTED.value})
        .eq("file_id", str(file_id))
        .execute()
    )
    return [ChunkMeta.model_validate(row) for row in rows(result)]


async def delete_for_file(db: Database, file_id: UUID) -> None:
    """Delete all chunk rows of a file (cleanup, SPEC §7.4).

    Deleting the parent ``files`` row cascades to chunks automatically; this is
    for sweeping an abandoned ``pending`` file's chunks directly.
    """
    await db.table(_TABLE).delete().eq("file_id", str(file_id)).execute()
