"""The cross-module seam ``files/`` uses to enqueue Telegram-deletion jobs (SPEC §6.12, §7.4).

A file soft-delete marks the row ``deleting``, decrements quota, and **enqueues a
deferred job** that actually deletes the file's Telegram messages and rows later
(SPEC §6.12, §7.4). ``files/`` must NOT delete Telegram messages inline (SPEC
§6.12) — that work is owned by ``jobs/`` (SPEC §6.14), which runs it via QStash.

⚠️ FLAGGED (see ``files/README.md`` "Flagged contract tension"): ``jobs/`` is
**module 14 — not built yet**, and ``files/``'s frozen dependency set (SPEC §6.12)
does **not** list ``jobs/``. We therefore cannot import a ``jobs/`` enqueue helper
here. Mirroring how ``folders/`` inverts its dependency on ``files/`` (see
``folders/ports.py``), ``files/`` defines the *port* it needs
(:class:`DeletionEnqueuer`) and ``jobs/`` (module 14) provides the concrete
enqueuer and registers it via :func:`set_deletion_enqueuer` at app composition.

Until one is registered, :func:`get_deletion_enqueuer` raises ``internal_error``
rather than silently dropping the deletion. (The §7.4 ``find_deleting`` sweep is the
independent backstop: a file left in ``deleting`` is still reclaimed by the job
sweeper even if its enqueue never fired — but we fail loudly instead of relying on
that.) The exact enqueue signature must be reconciled when ``jobs/`` is built;
:class:`DeletionEnqueuer` is this module's expectation of that contract.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from telecloud.shared import ErrorCode, TeleCloudError


class DeletionEnqueuer(Protocol):
    """The slice of ``jobs/`` that ``files/``'s soft-delete calls (SPEC §6.14, §7.4).

    Enqueues a deferred deletion job for a single file, identified by ``file_id``.
    The job itself (run later by QStash with the service-role client) deletes the
    file's Telegram messages and then its rows (SPEC §7.4); the enqueue only needs
    the file id. Implemented and registered by ``jobs/`` (module 14); reconcile
    against ``jobs/``'s actual enqueue helper when that module lands.
    """

    async def __call__(self, file_id: UUID) -> None: ...


_deletion_enqueuer: DeletionEnqueuer | None = None


def set_deletion_enqueuer(enqueuer: DeletionEnqueuer) -> None:
    """Register the concrete deletion-job enqueuer (called by ``jobs/``).

    ``jobs/`` invokes this once at composition/startup so ``files/``'s soft-delete
    can hand each deleted file off to the real deferred-deletion path without
    importing ``jobs/`` (which is unbuilt and outside ``files/``'s SPEC §6.12
    dependency set).
    """
    global _deletion_enqueuer
    _deletion_enqueuer = enqueuer


def get_deletion_enqueuer() -> DeletionEnqueuer:
    """Return the registered deletion enqueuer, or raise if none is wired yet.

    Raising ``internal_error`` (rather than degrading) keeps a soft-delete from
    silently skipping the Telegram-deletion job while ``jobs/`` is
    unbuilt/unregistered. Resolved *before* any mutation in
    :func:`telecloud.files.service.soft_delete_file`, so an unwired enqueuer fails
    fast without leaving a half-deleted file.
    """
    if _deletion_enqueuer is None:
        raise TeleCloudError.from_code(
            ErrorCode.INTERNAL_ERROR,
            "File deletion is unavailable: jobs/ has not registered an enqueuer.",
        )
    return _deletion_enqueuer
