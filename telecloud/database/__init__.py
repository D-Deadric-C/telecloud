"""``telecloud.database`` — Supabase/Postgres access layer (SPEC.md §6.3).

The data layer and nothing more: client factories, a thin client wrapper, and
per-table repositories. It owns the SQL migrations (``database/migrations/``,
SPEC §4) and returns the shared read models from ``telecloud.shared`` — never raw
DB rows. It contains **no** business rules (quota math, two-phase orchestration);
those live in ``quota/``, ``storage/``, and ``files/`` (SPEC §6.3).

Public surface:

* :func:`get_db` — user-scoped client honoring RLS via the user's JWT.
* :func:`get_service_db` — service-role client (sanctioned RLS bypass; share
  download read path only — SPEC §4, §7.3). :func:`close_service_db` releases it.
* :class:`Database` — the client wrapper repositories receive.
* The repositories: :mod:`profiles_repo`, :mod:`folders_repo`, :mod:`files_repo`,
  :mod:`chunks_repo`, :mod:`shares_repo`.

Depends only on ``telecloud.config`` and ``telecloud.shared`` (SPEC §6.3).
"""

from telecloud.database.client import (
    Database,
    close_db_pool,
    close_service_db,
    get_db,
    get_service_db,
    warm_db_pool,
)
from telecloud.database.repositories import (
    chunks_repo,
    files_repo,
    folders_repo,
    profiles_repo,
    shares_repo,
)

__all__ = [
    # clients
    "get_db",
    "get_service_db",
    "close_service_db",
    "close_db_pool",
    "warm_db_pool",
    "Database",
    # repositories
    "profiles_repo",
    "folders_repo",
    "files_repo",
    "chunks_repo",
    "shares_repo",
]
