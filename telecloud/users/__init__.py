"""``telecloud.users`` — profile state (SPEC.md §6.5).

Owns reading the profile and exposing verification status for other modules
(e.g. ``quota/``) to read off the row. It holds *only* the ``email_verified``
flag — quota usage math belongs to ``quota/`` (SPEC §6.5).

Email **verification is Supabase-managed**: Supabase sends the confirmation email
and owns the link, and a DB trigger mirrors ``auth.users.email_confirmed_at`` onto
``profiles.email_verified`` (migration ``0005``). There is no custom token flow in
this module anymore; re-sending the confirmation email lives in ``auth/``.

Public surface (what other modules import):

* :func:`get_profile` — read the caller's profile via the repo.
* :data:`router` — the ``users`` ``APIRouter`` to mount on the FastAPI app.

**Boundaries (SPEC §6.5):** does NOT compute or store quota usage (``quota/``) and
does NOT run a verification flow or send email (Supabase does). Service-layer
dependencies are config, shared, database.
"""

from telecloud.users.router import router
from telecloud.users.service import get_profile

__all__ = [
    # service
    "get_profile",
    # router
    "router",
]
