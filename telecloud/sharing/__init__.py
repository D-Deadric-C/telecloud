"""``telecloud.sharing`` — public, URL-based file sharing (SPEC.md §6.13).

Two surfaces with different trust models:

* **Authed management** (owner-scoped, RLS via the caller's JWT): create a public
  link for one of the caller's committed files, list a file's links, and revoke a
  link (soft). Service: :func:`create_share`, :func:`list_shares`,
  :func:`revoke_share`; routes on :data:`router` (``/shares``).
* **Public download** (no auth): ``GET /s/{token}`` resolves the share via the
  **service-role** client — the sole sanctioned RLS bypass (SPEC §4, §7.3) —
  enforces ``revoked`` / ``expires_at`` / ``download_limit``, bumps the counter,
  then reuses ``storage.open_download`` to stream (range-aware). Service:
  :func:`open_share_download`; route on :data:`public_router` (``/s``). It exposes
  nothing about the owner (SPEC §6.13).

Public surface (what other modules / the app import):

* :func:`create_share`, :func:`list_shares`, :func:`revoke_share`,
  :func:`open_share_download` — the service functions.
* :data:`router` — the authed ``/shares`` ``APIRouter``.
* :data:`public_router` — the unauthenticated ``/s`` ``APIRouter``.

**Boundaries (SPEC §6.13):** never bypasses the share checks, never uses the
service-role client beyond resolving + streaming the shared file, and never
reimplements chunk streaming (reuses ``storage.open_download``).

**Dependencies.** SPEC §6.13 permits ``config``, ``shared``, ``database``,
``files``, ``storage``. In practice ``sharing/`` uses ``config`` / ``shared`` /
``database`` / ``storage`` and does **not** import ``files/``: the only thing it
needs from the file domain is an owner+committed check on a ``files`` row, which it
does at the ``database.files_repo`` layer exactly as ``files/`` itself does (its
``_load_owned_file`` is private and not reusable). Using fewer of the allowed
dependencies breaks no contract; noted here for transparency (see
``sharing/README.md``).

**Design decisions** (SPEC §6.13 left these open; see ``sharing/README.md``):
default expiry is **none**, default download limit is **none/unlimited**, and
revocation is **soft** (``revoked=true``).
"""

from telecloud.sharing.public import public_router
from telecloud.sharing.router import router
from telecloud.sharing.service import (
    create_share,
    list_shares,
    open_share_download,
    revoke_share,
)

__all__ = [
    # service
    "create_share",
    "list_shares",
    "revoke_share",
    "open_share_download",
    # routers
    "router",
    "public_router",
]
