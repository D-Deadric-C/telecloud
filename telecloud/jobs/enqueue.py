"""The deferred-deletion enqueue helper ``files/`` calls (SPEC.md §6.14, §6.12).

``files/`` soft-deletes a file by marking the row ``deleting``, decrementing quota,
and then handing off to ``jobs/`` to actually remove the Telegram messages + rows
later (SPEC §6.12). It can't import ``jobs/`` (``jobs/`` is outside its frozen
dependency set), so it defined a port — :class:`telecloud.files.ports.DeletionEnqueuer`
(``async (file_id) -> None``) — and expects ``jobs/`` to register a concrete
implementation via ``files.ports.set_deletion_enqueuer`` at app composition. This
module is that registration, kept deliberately **thin** (SPEC §6.14).

What "enqueue" means here, and a FLAG
-------------------------------------
The authoritative signal that a file needs deferred deletion is its **DB status**:
``files/`` has already set it to ``deleting``, and :func:`jobs.service.delete_deferred`
finds *every* ``deleting`` file via ``files_repo.find_deleting`` (SPEC §7.4). So the
periodic QStash-triggered deferred-delete job reclaims the file whether or not this
enqueue fires — exactly the backstop ``files/ports`` documents.

To *expedite* a delete (trigger the job immediately instead of waiting for the next
scheduled run) we would publish a one-off message to QStash's **publish** API. That
needs QStash **publish** credentials (a token + base URL). SPEC §6.1 says ``config``
owns "QStash URLs + keys", but the built ``config`` exposes only the two *signing*
keys (the verify side) — there is no publish token. Adding one is a change to the
``config`` contract, so per the build rules we **flag, not make** it here.

Until those credentials exist, the registered enqueuer is **sweep-backed**: the
durable ``deleting`` status (already written by ``files/``) is the queue, and the
scheduled job drains it. The enqueuer therefore just records the intent and returns
— thin and correct. The optional :class:`QStashPublisher` seam is provided so that,
once publish credentials land in ``config``, on-demand triggering can be wired by
passing a real publisher to :func:`register` with **no change to ``files/``**.
"""

from __future__ import annotations

import logging
from typing import Protocol
from uuid import UUID

from telecloud.files import ports as files_ports

logger = logging.getLogger("telecloud.jobs")


class QStashPublisher(Protocol):
    """Publishes a one-off message that triggers the deferred-delete job for a file.

    The seam for on-demand triggering (see module FLAG). A real implementation
    POSTs to QStash's publish API so the job runs promptly; it requires publish
    credentials that are **not** in ``config`` yet, so none is wired by default.
    """

    async def publish_deferred_delete(self, file_id: UUID) -> None: ...


async def _sweep_backed_enqueue(file_id: UUID) -> None:
    """Thin default enqueuer: rely on the ``deleting``-status sweep (SPEC §7.4).

    ``files/`` has already persisted ``status='deleting'`` before calling this, and
    :func:`jobs.service.delete_deferred` reclaims every such file via
    ``find_deleting``. With no QStash publish credentials in ``config`` (module
    FLAG), there is nothing to trigger on demand, so this records the intent and
    returns. It deliberately never raises: a deferred delete must not fail the
    user-facing soft-delete, and the sweep is the guaranteed backstop.
    """
    logger.info("deferred deletion queued for file %s (sweep-backed)", file_id)


def make_enqueuer(
    publisher: QStashPublisher | None = None,
) -> files_ports.DeletionEnqueuer:
    """Build the deletion enqueuer ``files/`` will call.

    With no ``publisher`` (the default, since ``config`` lacks publish credentials)
    the returned enqueuer is :func:`_sweep_backed_enqueue`. When a ``publisher`` is
    supplied it is used to trigger the job on demand, falling back to the sweep if
    the publish fails — losing an expedite request must never strand the delete,
    because the scheduled sweep still reclaims the ``deleting`` row.
    """
    if publisher is None:
        return _sweep_backed_enqueue

    async def _enqueue(file_id: UUID) -> None:
        try:
            await publisher.publish_deferred_delete(file_id)
        except Exception:  # pragma: no cover - defensive; sweep is the backstop
            logger.exception(
                "QStash publish failed for file %s; relying on the deferred-delete "
                "sweep",
                file_id,
            )

    return _enqueue


def register(publisher: QStashPublisher | None = None) -> files_ports.DeletionEnqueuer:
    """Register the deferred-deletion enqueuer with ``files/`` (app composition).

    Call once at startup so ``files.service.soft_delete_file`` can hand each
    soft-deleted file to ``jobs/`` without importing it (SPEC §6.12 dependency
    inversion). Returns the registered enqueuer for convenience/tests.
    """
    enqueuer = make_enqueuer(publisher)
    files_ports.set_deletion_enqueuer(enqueuer)
    return enqueuer
