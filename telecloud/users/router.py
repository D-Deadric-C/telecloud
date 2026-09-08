"""FastAPI router for the profile endpoint (SPEC.md §6.5).

A single thin route that delegates to :mod:`telecloud.users.service`:

* ``GET /users/me`` — the caller's profile (authed).

Email verification is **Supabase-managed** (the confirmation email + link are
Supabase's, kept in sync by a DB trigger), so ``users/`` no longer exposes start
/ confirm verification routes — that lived in the old custom Resend flow. Resend
of the confirmation email lives in ``auth/`` (``POST /auth/resend-confirmation``),
since an unconfirmed user has no session to authenticate a ``users/`` route.

Errors raised below the route surface as :class:`TeleCloudError` and are rendered
to the canonical JSON envelope by ``middleware/`` (SPEC §5.1).

FLAGGED (see ``users/README.md``): the authed route uses ``auth.current_user`` /
``auth.access_token``, so the *router* depends on ``auth`` even though SPEC §6.5
lists the ``users/`` dependency set as config/shared/database. This is a
build-order-safe composition dependency (``auth`` is already built and does not
import ``users``); the service layer stays free of it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from telecloud.auth import access_token, current_user
from telecloud.shared import UserContext

from telecloud.users import service
from telecloud.users.schemas import ProfileResponse

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=ProfileResponse)
async def read_profile(
    user: UserContext = Depends(current_user),
    token: str = Depends(access_token),
) -> ProfileResponse:
    """Return the caller's profile (read fresh via the repo)."""
    profile = await service.get_profile(user, access_token=token)
    return ProfileResponse.from_context(profile)
