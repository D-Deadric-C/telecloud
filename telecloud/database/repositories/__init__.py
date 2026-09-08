"""Per-table repositories (SPEC §6.3).

Each module is a small, stateless namespace of ``async`` functions that take a
:class:`telecloud.database.client.Database` and return shared read models (never
raw rows). Import the modules and call their functions::

    from telecloud.database import files_repo
    file = await files_repo.insert_pending(db, owner_id=..., ...)
"""

from telecloud.database.repositories import (
    chunks_repo,
    files_repo,
    folders_repo,
    profiles_repo,
    shares_repo,
)

__all__ = [
    "profiles_repo",
    "folders_repo",
    "files_repo",
    "chunks_repo",
    "shares_repo",
]
