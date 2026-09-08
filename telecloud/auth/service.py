"""Signup/login/logout orchestration (SPEC.md §6.4).

Ties the Supabase auth adapter (:mod:`telecloud.auth.supabase_auth`) to the
``database/`` profile repo. Verification is **Supabase-managed**: signup triggers
Supabase's built-in confirmation email and returns no session, so the
``profiles`` shell is created by a database trigger on ``auth.users`` insert (see
migration ``0005``), not here. Login ensures the shell as a safety net and always
returns the authoritative ``email_verified`` flag (kept in sync by the same
migration's trigger).

A single :class:`SupabaseAuth` adapter is built lazily from settings and reused.
Tests swap it via :func:`set_auth` (or by overriding ``_get_auth``).
"""

from __future__ import annotations

from telecloud.database import get_db, profiles_repo
from telecloud.shared import UserContext

from telecloud.auth.schemas import PublicUser, SessionResponse, SignupResponse
from telecloud.auth.supabase_auth import AuthSession, SupabaseAuth

#: Process-wide reused adapter, built from settings on first use (SPEC §5.2).
_auth: SupabaseAuth | None = None


async def _get_auth() -> SupabaseAuth:
    """Return the lazily-built shared :class:`SupabaseAuth` adapter."""
    global _auth
    if _auth is None:
        _auth = await SupabaseAuth.from_settings()
    return _auth


def set_auth(adapter: SupabaseAuth | None) -> None:
    """Inject (or clear) the shared auth adapter — for wiring/tests."""
    global _auth
    _auth = adapter


async def close() -> None:
    """Release the shared adapter's HTTP resources (call at app shutdown)."""
    global _auth
    if _auth is not None:
        await _auth.aclose()
        _auth = None


async def _ensure_profile(session: AuthSession) -> UserContext:
    """Ensure a ``profiles`` shell exists for the user; return its identity.

    Idempotent: returns the existing profile if present, else inserts one with
    ``email_verified=false`` (SPEC §6.4). Runs under the user-scoped client so the
    RLS insert policy (``id = auth.uid()``) is satisfied by the just-issued token.
    """
    db = await get_db(session.access_token)
    try:
        existing = await profiles_repo.get(db, session.user_id)
        if existing is not None:
            return existing
        return await profiles_repo.insert(
            db,
            user_id=session.user_id,
            email=session.email,
            email_verified=False,
        )
    finally:
        await db.aclose()


def _to_response(session: AuthSession, user: UserContext) -> SessionResponse:
    return SessionResponse(
        access_token=session.access_token,
        refresh_token=session.refresh_token,
        expires_in=session.expires_in,
        user=PublicUser.from_context(user),
    )


async def signup(*, email: str, password: str) -> SignupResponse:
    """Register a user with Supabase; it emails the confirmation link (SPEC §6.4).

    Returns a "check your email" response, not a session: with Supabase email
    confirmation enabled, no session is issued until the user clicks the link. The
    ``profiles`` shell is created by the ``auth.users`` insert trigger (migration
    ``0005``), so there is nothing to write here.
    """
    auth = await _get_auth()
    result = await auth.sign_up(email=email, password=password)
    return SignupResponse(
        email=result.email,
        confirmation_required=result.confirmation_required,
    )


async def resend_confirmation(*, email: str) -> None:
    """Re-send the Supabase confirmation email (best-effort, SPEC §6.4)."""
    auth = await _get_auth()
    await auth.resend_confirmation(email=email)


async def login(*, email: str, password: str) -> SessionResponse:
    """Authenticate with Supabase and return the issued session (SPEC §6.4).

    The profile shell normally already exists (created at signup); we ensure it as
    a safety net so login always returns the authoritative ``email_verified`` flag.
    """
    auth = await _get_auth()
    session = await auth.sign_in(email=email, password=password)
    user = await _ensure_profile(session)
    return _to_response(session, user)


async def logout(*, access_token: str, refresh_token: str | None) -> None:
    """Revoke the user's session with Supabase (best-effort, SPEC §6.4)."""
    auth = await _get_auth()
    await auth.sign_out(access_token=access_token, refresh_token=refresh_token)
