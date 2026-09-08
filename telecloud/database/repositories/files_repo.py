"""Data access for ``public.files`` (SPEC §4.3) — file metadata + lifecycle.

Covers the operations the upload two-phase commit (SPEC §7.1), the file routes
(SPEC §6.12), and the cleanup jobs (SPEC §7.4) imply. This repo only moves the
``status`` / ``deleted_at`` columns between states; it does NOT compute quota,
talk to Telegram, or decide *when* to transition — those live in ``files/``,
``quota/``, ``storage/``, and ``jobs/``. Rows are returned as :class:`FileMeta`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from telecloud.shared import FileMeta, FileStatus

from telecloud.database._encoding import to_jsonable
from telecloud.database.client import Database
from telecloud.database.repositories._common import first, rows

_TABLE = "files"
_COLUMNS = (
    "id, owner_id, folder_id, name, size_bytes, mime_type, "
    "chunk_count, status, created_at, deleted_at"
)


async def insert_pending(
    db: Database,
    *,
    owner_id: UUID,
    name: str,
    size_bytes: int,
    chunk_count: int,
    folder_id: UUID | None = None,
    mime_type: str = "application/octet-stream",
) -> FileMeta:
    """Create the ``pending`` files row that opens a two-phase upload (SPEC §7.1).

    Status defaults to ``pending`` in the schema; the file becomes usable only
    once :func:`mark_committed` runs after all chunks are confirmed.
    """
    payload = to_jsonable(
        {
            "owner_id": owner_id,
            "folder_id": folder_id,
            "name": name,
            "size_bytes": size_bytes,
            "mime_type": mime_type,
            "chunk_count": chunk_count,
            "status": FileStatus.PENDING,
        }
    )
    row = first(await db.table(_TABLE).insert(payload).execute())
    assert row is not None
    return FileMeta.model_validate(row)


async def get(db: Database, file_id: UUID) -> FileMeta | None:
    """Return a file by id (any status, including soft-deleted), or ``None``."""
    row = first(
        await db.table(_TABLE)
        .select(_COLUMNS)
        .eq("id", str(file_id))
        .limit(1)
        .execute()
    )
    return FileMeta.model_validate(row) if row else None


async def list_in_folder(
    db: Database, *, owner_id: UUID, folder_id: UUID | None
) -> list[FileMeta]:
    """List the owner's committed, non-deleted files in a folder (root if ``None``).

    Only ``committed`` files are user-visible (SPEC §7.1); pending and deleting
    rows are excluded.
    """
    query = (
        db.table(_TABLE)
        .select(_COLUMNS)
        .eq("owner_id", str(owner_id))
        .eq("status", FileStatus.COMMITTED.value)
        .is_("deleted_at", "null")
    )
    if folder_id is None:
        query = query.is_("folder_id", "null")
    else:
        query = query.eq("folder_id", str(folder_id))
    result = await query.order("name").execute()
    return [FileMeta.model_validate(row) for row in rows(result)]


async def mark_committed(db: Database, file_id: UUID) -> FileMeta | None:
    """Flip a file to ``committed`` — the commit step of the upload (SPEC §7.1).

    Chunks are committed separately via ``chunks_repo.mark_all_committed``; the
    two-phase orchestration that calls both belongs to ``storage/``/``files/``.
    """
    row = first(
        await db.table(_TABLE)
        .update({"status": FileStatus.COMMITTED.value})
        .eq("id", str(file_id))
        .execute()
    )
    return FileMeta.model_validate(row) if row else None


async def mark_deleting(db: Database, file_id: UUID) -> FileMeta | None:
    """Soft-delete: set status ``deleting`` and stamp ``deleted_at`` (SPEC §6.12).

    The actual Telegram-message removal and row deletion are deferred to a
    ``jobs/`` deletion job (SPEC §7.4); quota is decremented by ``files/`` at this
    point, not here.
    """
    payload = to_jsonable(
        {
            "status": FileStatus.DELETING,
            "deleted_at": datetime.now(timezone.utc),
        }
    )
    row = first(
        await db.table(_TABLE).update(payload).eq("id", str(file_id)).execute()
    )
    return FileMeta.model_validate(row) if row else None


async def rename(db: Database, file_id: UUID, name: str) -> FileMeta | None:
    """Rename a file; return the updated row (or ``None`` if not found)."""
    row = first(
        await db.table(_TABLE)
        .update({"name": name})
        .eq("id", str(file_id))
        .execute()
    )
    return FileMeta.model_validate(row) if row else None


async def move(
    db: Database, file_id: UUID, *, new_folder_id: UUID | None
) -> FileMeta | None:
    """Move a file to another folder (``None`` = root); return the updated row."""
    payload = to_jsonable({"folder_id": new_folder_id})
    row = first(
        await db.table(_TABLE).update(payload).eq("id", str(file_id)).execute()
    )
    return FileMeta.model_validate(row) if row else None


async def find_pending_older_than(
    db: Database, cutoff: datetime
) -> list[FileMeta]:
    """Find ``pending`` files created before ``cutoff`` — the orphan sweep input.

    Used by the ``jobs/`` orphan sweeper (SPEC §7.4) to reclaim abandoned uploads.
    Typically run with the service-role client from a QStash job.
    """
    result = await (
        db.table(_TABLE)
        .select(_COLUMNS)
        .eq("status", FileStatus.PENDING.value)
        .lt("created_at", cutoff.isoformat())
        .order("created_at")
        .execute()
    )
    return [FileMeta.model_validate(row) for row in rows(result)]


async def find_deleting(db: Database) -> list[FileMeta]:
    """Find files in ``deleting`` awaiting deferred hard-delete (SPEC §7.4)."""
    result = await (
        db.table(_TABLE)
        .select(_COLUMNS)
        .eq("status", FileStatus.DELETING.value)
        .order("deleted_at")
        .execute()
    )
    return [FileMeta.model_validate(row) for row in rows(result)]


async def delete_row(db: Database, file_id: UUID) -> None:
    """Hard-delete a file row after its Telegram messages are gone (SPEC §7.4).

    The ``chunks`` rows cascade via the FK ``on delete cascade``.
    """
    await db.table(_TABLE).delete().eq("id", str(file_id)).execute()
