"""``telecloud.auth`` — JWT authentication backed by Supabase (SPEC.md §6.4).

Owns JWT issue/verify against the Supabase JWT secret, the signup/login/logout
routes, and the dependencies other modules use to protect their routes. Password
handling and email verification are delegated to Supabase (no home-grown hashing,
no custom verification flow); signup triggers Supabase's built-in confirmation
email, and the ``profiles`` shell is created by a DB trigger (SPEC §6.4).

Public surface (what other modules import):

* :func:`current_user` — FastAPI dependency yielding a ``shared.UserContext`` or
  raising ``TeleCloudError("unauthorized", 401)``.
* :func:`require_verified` — builds on ``current_user``; raises
  ``TeleCloudError("forbidden", 403)`` when the email is unverified.
* :data:`router` — the auth ``APIRouter`` to mount on the FastAPI app.

Also exported for wiring/tests: :func:`verify_token`, the :class:`SupabaseAuth`
adapter, and :func:`close` (release the shared adapter at shutdown).

**Boundaries (SPEC §6.4):** does NOT enforce quota (``quota/``) or manage
files/folders. Email verification is Supabase-managed (the confirmation email and
link are Supabase's). Reads config only through ``config`` and the DB only through
``database/`` repos. Depends on ``config``, ``shared``, ``database``.
"""

from telecloud.auth.dependencies import (
    access_token,
    current_user,
    require_verified,
)
from telecloud.auth.router import router
from telecloud.auth.service import close
from telecloud.auth.supabase_auth import AuthSession, SupabaseAuth
from telecloud.auth.tokens import (
    close_jwks_client,
    encode_token,
    reset_jwks_cache,
    verify_supabase_token,
    verify_token,
)

__all__ = [
    # dependencies
    "current_user",
    "require_verified",
    "access_token",
    # router
    "router",
    # tokens
    "verify_supabase_token",
    "verify_token",
    "encode_token",
    "reset_jwks_cache",
    "close_jwks_client",
    # adapter / lifecycle
    "SupabaseAuth",
    "AuthSession",
    "close",
]
