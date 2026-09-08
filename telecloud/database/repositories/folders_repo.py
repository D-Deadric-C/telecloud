"""Data access for ``public.folders`` (SPEC §4.2) — adjacency-list tree.

CRUD over folder rows returning :class:`FolderMeta`. Tree *traversal* and cascade
semantics (recursing into descendants on delete) belong to ``folders/`` (SPEC
§6.11); this repo offers the single-level primitives it builds on. Soft delete
sets ``deleted_at``; list reads exclude soft-deleted rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from telecloud.shared import FolderMeta

from telecloud.database._encoding import to_jsonable
from telecloud.database.client import Database
from telecloud.database.repositories._common import first, rows

_TABLE = "folders"
_COLUMNS = "id, owner_id, parent_id, name, created_at, deleted_at"


async def insert(
    db: Database,
    *,
    owner_id: UUID,
    name: str,
    parent_id: UUID | None = None,
) -> FolderMeta:
    """Create a folder (root when ``parent_id`` is ``None``) and return it."""
    payload = to_jsonable(
        {"owner_id": owner_id, "name": name, "parent_id": parent_id}
    )
    row = first(await db.table(_TABLE).insert(payload).execute())
    assert row is not None
    return FolderMeta.model_validate(row)


async def get(db: Database, folder_id: UUID) -> FolderMeta | None:
    """Return a folder by id (including soft-deleted), or ``None``."""
    row = first(
        await db.table(_TABLE)
        .select(_COLUMNS)
        .eq("id", str(folder_id))
        .limit(1)
        .execute()
    )
    return FolderMeta.model_validate(row) if row else None


async def list_children(
    db: Database, *, owner_id: UUID, parent_id: UUID | None
) -> list[FolderMeta]:
    """List a folder's direct, non-deleted child folders.

    ``parent_id is None`` lists the owner's root-level folders. Ordered by name
    for a stable listing.
    """
    query = (
        db.table(_TABLE)
        .select(_COLUMNS)
        .eq("owner_id", str(owner_id))
        .is_("deleted_at", "null")
    )
    if parent_id is None:
        query = query.is_("parent_id", "null")
    else:
        query = query.eq("parent_id", str(parent_id))
    result = await query.order("name").execute()
    return [FolderMeta.model_validate(row) for row in rows(result)]


async def rename(db: Database, folder_id: UUID, name: str) -> FolderMeta | None:
    """Rename a folder; return the updated row (or ``None`` if not found)."""
    row = first(
        await db.table(_TABLE)
        .update({"name": name})
        .eq("id", str(folder_id))
        .execute()
    )
    return FolderMeta.model_validate(row) if row else None


async def move(
    db: Database, folder_id: UUID, *, new_parent_id: UUID | None
) -> FolderMeta | None:
    """Re-parent a folder (``None`` moves it to the root); return updated row."""
    payload = to_jsonable({"parent_id": new_parent_id})
    row = first(
        await db.table(_TABLE).update(payload).eq("id", str(folder_id)).execute()
    )
    return FolderMeta.model_validate(row) if row else None


async def soft_delete(db: Database, folder_id: UUID) -> FolderMeta | None:
    """Mark a folder deleted by stamping ``deleted_at`` (SPEC §4 soft delete).

    Cascading the delete to descendant folders and their files is orchestrated by
    ``folders/``; this sets the timestamp on the one row.
    """
    payload = to_jsonable({"deleted_at": datetime.now(timezone.utc)})
    row = first(
        await db.table(_TABLE).update(payload).eq("id", str(folder_id)).execute()
    )
    return FolderMeta.model_validate(row) if row else None
