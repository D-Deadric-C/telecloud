"""Data access for ``public.shares`` (SPEC §4.5) — public share links.

Create/revoke links, resolve a token to a share, and atomically bump the
download counter (SPEC §6.13, §7.3). Rows are returned as the internal
:class:`ShareMeta` (which includes ``owner_id``); the public download route is
responsible for not leaking owner identity (SPEC §6.13). The expiry / limit /
revoked *checks* are enforced by ``sharing/`` — this repo just stores and reads.

NOTE: :func:`resolve_by_token` is the share-download read path and is intended to
run with the **service-role** client (``get_service_db``), the only sanctioned
RLS bypass (SPEC §4, §7.3).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from telecloud.shared import ShareMeta

from telecloud.database._encoding import to_jsonable
from telecloud.database.client import Database
from telecloud.database.repositories._common import first, rows

_TABLE = "shares"
_COLUMNS = (
    "id, file_id, owner_id, token, expires_at, download_limit, "
    "download_count, revoked, created_at"
)


async def insert(
    db: Database,
    *,
    file_id: UUID,
    owner_id: UUID,
    token: str,
    expires_at: datetime | None = None,
    download_limit: int | None = None,
) -> ShareMeta:
    """Create a share link for a file and return it (SPEC §6.13).

    ``token`` is an unguessable URL-safe string (generated via
    ``shared.generate_token``). ``expires_at`` / ``download_limit`` of ``None``
    mean never-expire / unlimited.
    """
    payload = to_jsonable(
        {
            "file_id": file_id,
            "owner_id": owner_id,
            "token": token,
            "expires_at": expires_at,
            "download_limit": download_limit,
        }
    )
    row = first(await db.table(_TABLE).insert(payload).execute())
    assert row is not None
    return ShareMeta.model_validate(row)


async def get(db: Database, share_id: UUID) -> ShareMeta | None:
    """Return a share by id, or ``None``."""
    row = first(
        await db.table(_TABLE)
        .select(_COLUMNS)
        .eq("id", str(share_id))
        .limit(1)
        .execute()
    )
    return ShareMeta.model_validate(row) if row else None


async def list_for_file(db: Database, file_id: UUID) -> list[ShareMeta]:
    """List the share links created for a file (owner-scoped management view)."""
    result = await (
        db.table(_TABLE)
        .select(_COLUMNS)
        .eq("file_id", str(file_id))
        .order("created_at")
        .execute()
    )
    return [ShareMeta.model_validate(row) for row in rows(result)]


async def resolve_by_token(db: Database, token: str) -> ShareMeta | None:
    """Resolve a share by its public ``token``, or ``None`` (SPEC §7.3).

    Intended for the public, unauthenticated download route, run with the
    service-role client (the sanctioned RLS bypass). The returned model still
    carries ``owner_id``; the route must not expose it. Expiry/limit/revoked are
    checked by ``sharing/`` after this resolves the row.
    """
    row = first(
        await db.table(_TABLE)
        .select(_COLUMNS)
        .eq("token", token)
        .limit(1)
        .execute()
    )
    return ShareMeta.model_validate(row) if row else None


async def increment_download_count(db: Database, share_id: UUID) -> int | None:
    """Atomically increment a share's ``download_count``; return the new count.

    Backed by the ``increment_share_download`` SQL function so the bump is a
    single atomic UPDATE (SPEC §7.3). Returns ``None`` if the share is gone.
    """
    response = await db.rpc(
        "increment_share_download", {"p_share_id": str(share_id)}
    ).execute()
    value = getattr(response, "data", None)
    return int(value) if value is not None else None


async def revoke(db: Database, share_id: UUID) -> ShareMeta | None:
    """Mark a share revoked (soft) and return it (SPEC §6.13).

    Revocation is a soft flag here: the row is kept (so download attempts can be
    rejected with ``share_revoked``) and ``sharing/`` enforces the ``revoked``
    check on the download path.
    """
    row = first(
        await db.table(_TABLE)
        .update({"revoked": True})
        .eq("id", str(share_id))
        .execute()
    )
    return ShareMeta.model_validate(row) if row else None
