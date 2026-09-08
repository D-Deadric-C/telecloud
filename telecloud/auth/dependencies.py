"""FastAPI auth dependencies: ``current_user`` and ``require_verified`` (SPEC §6.4).

These are the public surface other modules import to protect routes:

* :func:`current_user` resolves the request's bearer token to a
  :class:`UserContext`, or raises ``TeleCloudError("unauthorized", 401)``.
* :func:`require_verified` builds on it and raises
  ``TeleCloudError("forbidden", 403)`` when the user's email is not verified.

The verification *flag* is read fresh from the ``profiles`` row through the
``database/`` user-scoped client (RLS-honoring), not trusted from the JWT — the
tier gate can change after a token is issued, and the profile is authoritative
(SPEC §3). This module enforces *whether* a user is verified; it never enforces
quota numbers (that's ``quota/``) and never decides verification (that's
``users/``).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from telecloud.database import get_db, profiles_repo
from telecloud.shared import ErrorCode, TeleCloudError, UserContext

from telecloud.auth.tokens import user_id_from_claims, verify_supabase_token

#: Pulls ``Authorization: Bearer <token>`` off the request. ``auto_error=False``
#: so a missing/blank header yields ``None`` and we raise the canonical
#: ``TeleCloudError`` ourselves (rather than FastAPI's default 403 envelope).
bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized(message: str = "Authentication required.") -> TeleCloudError:
    return TeleCloudError.from_code(ErrorCode.UNAUTHORIZED, message)


async def access_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    """Return the raw bearer token, or raise ``unauthorized`` if absent.

    Exposed as its own dependency so routes that need the token string itself
    (e.g. logout, which revokes it) can require it without re-resolving a profile.
    """
    if credentials is None or not credentials.credentials:
        raise _unauthorized()
    return credentials.credentials


async def _load_profile(user_id: UUID, token: str) -> UserContext | None:
    """Load the user's profile via the RLS-scoped client (SPEC §4, §6.3).

    Isolated so tests can substitute it without a live Supabase project; it is the
    only I/O ``current_user`` performs.
    """
    db = await get_db(token)
    try:
        return await profiles_repo.get(db, user_id)
    finally:
        await db.aclose()


async def current_user(token: str = Depends(access_token)) -> UserContext:
    """Resolve the request's bearer token to a :class:`UserContext` (SPEC §6.4).

    Verifies the JWT against Supabase's signing key (JWKS for ES256/RS256, or the
    shared secret for HS256), then loads the matching ``profiles`` row
    (authoritative for ``email_verified``). Raises
    ``TeleCloudError("unauthorized", 401)`` if the token is missing/invalid/expired
    or no profile exists for the token's subject.
    """
    claims = await verify_supabase_token(token)
    user_id = user_id_from_claims(claims)
    user = await _load_profile(user_id, token)
    if user is None:
        raise _unauthorized("No profile found for this account.")
    return user


async def require_verified(
    user: UserContext = Depends(current_user),
) -> UserContext:
    """Like :func:`current_user`, but require a verified email (SPEC §3, §6.4).

    Raises ``TeleCloudError("forbidden", 403)`` when ``email_verified`` is false.
    Used to gate the quota-relaxed routes; the quota *numbers* themselves are
    enforced by ``quota/``, not here.
    """
    if not user.email_verified:
        raise TeleCloudError.from_code(
            ErrorCode.FORBIDDEN, "Email verification is required for this action."
        )
    return user
