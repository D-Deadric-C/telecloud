"""Data access for ``public.profiles`` (SPEC §4.1).

One row per auth user, tracking verification state and storage usage. This repo
only reads and writes those columns — it enforces no quota rules (that's
``quota/``) and runs no verification flow (that's ``users/``). The identity
fields are returned as the shared :class:`UserContext` model; storage usage is a
plain ``int`` (a scalar, not a raw row), and the atomic increment used on
commit/delete is exposed as :func:`adjust_storage_used`.

See database/README.md "Contract notes" on why there is no ``ProfileMeta`` shared
model and how the storage column is surfaced.
"""

from __future__ import annotations

from uuid import UUID

from telecloud.shared import UserContext

from telecloud.database._encoding import to_jsonable
from telecloud.database.client import Database
from telecloud.database.repositories._common import first

_TABLE = "profiles"


async def insert(
    db: Database,
    *,
    user_id: UUID,
    email: str,
    email_verified: bool = False,
) -> UserContext:
    """Insert the profile row for a newly signed-up user and return it.

    Called by ``users/`` on signup (SPEC §6.5). ``id`` must equal the auth user's
    id; under the user-scoped client the RLS insert policy enforces
    ``id = auth.uid()``.
    """
    payload = to_jsonable(
        {"id": user_id, "email": email, "email_verified": email_verified}
    )
    row = first(await db.table(_TABLE).insert(payload).execute())
    assert row is not None  # insert returns the created representation
    return UserContext.model_validate(row)


async def get(db: Database, user_id: UUID) -> UserContext | None:
    """Return the user's identity (id/email/verified), or ``None`` if absent."""
    row = first(
        await db.table(_TABLE)
        .select("id, email, email_verified")
        .eq("id", str(user_id))
        .limit(1)
        .execute()
    )
    return UserContext.model_validate(row) if row else None


async def get_storage_used(db: Database, user_id: UUID) -> int | None:
    """Return the user's current ``storage_used_bytes``, or ``None`` if absent."""
    row = first(
        await db.table(_TABLE)
        .select("storage_used_bytes")
        .eq("id", str(user_id))
        .limit(1)
        .execute()
    )
    return int(row["storage_used_bytes"]) if row else None


async def adjust_storage_used(db: Database, user_id: UUID, delta: int) -> int | None:
    """Atomically add ``delta`` (may be negative) to usage; return the new value.

    Backed by the ``adjust_storage_used`` SQL function so the read-modify-write is
    a single atomic UPDATE (SPEC §3, §7.1). ``quota/`` owns the decision of *what*
    delta to apply; this repo only performs the mutation. Returns ``None`` if no
    such profile row exists.
    """
    response = await db.rpc(
        "adjust_storage_used",
        {"p_owner": str(user_id), "p_delta": delta},
    ).execute()
    value = getattr(response, "data", None)
    return int(value) if value is not None else None
