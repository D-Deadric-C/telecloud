"""The cross-module seam folders/ uses to delete files (SPEC.md §6.11, §6.12).

A folder soft-delete must cascade into the files it contains, but actually
deleting a file (mark ``deleting`` → decrement quota → enqueue the Telegram
deletion job, SPEC §6.12, §7.4) is **owned by ``files/``**, not here. ``folders/``
must NOT reach into the ``files`` tables to do that itself — it would skip the
quota decrement and the deletion job.

The dependency direction is fixed by SPEC §6.12: ``files/`` depends on
``folders/``, so ``folders/`` can never import ``files/`` (that would be a cycle).
We therefore invert the dependency: ``folders/`` defines the *port*
(:class:`FileDeleter`) it needs, and ``files/`` (module 13) provides the adapter
and registers it via :func:`set_file_deleter` at app-composition time.

⚠️ FLAGGED (see ``folders/README.md``): ``files/`` is not built yet, so the
concrete deleter does not exist. Until it is registered, :func:`get_file_deleter`
raises ``internal_error`` rather than silently corrupting state. The exact
signature of the files-deletion entrypoint must be reconciled when ``files/`` is
built; :class:`FileDeleter` is this module's expectation of that contract.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from telecloud.shared import ErrorCode, TeleCloudError, UserContext


class FileDeleter(Protocol):
    """The slice of ``files/``'s public service that ``folders/`` calls.

    A single file's soft-delete: mark it ``deleting``, decrement the owner's
    quota, and enqueue its Telegram-deletion job (SPEC §6.12). It is owner-scoped
    via ``access_token`` (RLS) exactly like the rest of the request path.

    Implemented by ``files/`` (module 13). Mirrors the ``(user, *, access_token)``
    shape the other service layers use; reconcile against ``files/``'s actual
    entrypoint when that module lands.
    """

    async def __call__(
        self, user: UserContext, file_id: UUID, *, access_token: str
    ) -> None: ...


_file_deleter: FileDeleter | None = None


def set_file_deleter(deleter: FileDeleter) -> None:
    """Register the concrete file-deletion entrypoint (called by ``files/``).

    ``files/`` invokes this once at composition/startup so the folder cascade can
    hand each contained file off to the real deletion path without importing
    ``files/`` (which would be a circular dependency).
    """
    global _file_deleter
    _file_deleter = deleter


def get_file_deleter() -> FileDeleter:
    """Return the registered file deleter, or raise if none is wired yet.

    Raising ``internal_error`` (rather than degrading) keeps a cascading folder
    delete from silently orphaning files while ``files/`` is unbuilt/unregistered.
    """
    if _file_deleter is None:
        raise TeleCloudError.from_code(
            ErrorCode.INTERNAL_ERROR,
            "File deletion is unavailable: files/ has not registered a deleter.",
        )
    return _file_deleter
