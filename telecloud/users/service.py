"""Profile reads (SPEC.md §3, §6.5).

The remit of ``users/`` is the profile: read it and expose verification status for
other modules to read off the row. Email **verification is Supabase-managed** now
— Supabase sends the confirmation email and owns the link, and a database trigger
mirrors ``auth.users.email_confirmed_at`` onto ``profiles.email_verified`` (see
migration ``0005``). So there is no custom token flow here anymore; this module
neither issues nor redeems verification tokens, and never sends mail.

It does **not** compute or store quota math (that's ``quota/``). Dependencies are
``config``, ``shared``, ``database``.
"""

from __future__ import annotations

import logging

from telecloud.database import get_db, profiles_repo
from telecloud.shared import ErrorCode, TeleCloudError, UserContext

logger = logging.getLogger("telecloud.users")


async def get_profile(user: UserContext, *, access_token: str) -> UserContext:
    """Return the caller's profile, read fresh through the repo (SPEC §6.5).

    Reads via the RLS-scoped client, so a user only ever sees their own row. The
    user's JWT is required to scope RLS — ``get_db`` takes the access token, not
    the ``UserContext`` (see database/README.md "Contract notes" on ``get_db``).
    Raises ``not_found`` if the profile row is absent.
    """
    db = await get_db(access_token)
    try:
        profile = await profiles_repo.get(db, user.id)
    finally:
        await db.aclose()
    if profile is None:
        raise TeleCloudError.from_code(ErrorCode.NOT_FOUND, "Profile not found.")
    return profile
