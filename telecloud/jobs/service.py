"""The cleanup jobs themselves: orphan sweep + deferred delete (SPEC.md §6.14, §7.4).

Two QStash-triggered jobs, plus the retry-drain they share:

* **Orphan sweep** (:func:`sweep_orphans`) — reclaim abandoned uploads. Finds
  ``files`` stuck in ``pending`` older than a threshold (a two-phase commit that
  never finished, SPEC §7.1 step 6), deletes every chunk's Telegram message, then
  removes the chunk + file rows.
* **Deferred delete** (:func:`delete_deferred`) — finish soft-deletes. Finds
  ``files`` in ``deleting`` (``files/`` already marked them and **already
  decremented quota**, SPEC §6.12), deletes their Telegram messages, then removes
  the rows. Quota is **not** touched here — doing so would double-count (SPEC §6.14
  "Must NOT").
* **Retry drain** (:func:`process_retries`) — transient Telegram failures during a
  delete are enqueued on ``rate_limit.queue``; a later run replays a bounded batch,
  dead-lettering an op once it has failed ``max_attempts`` times instead of looping
  on it forever (SPEC §6.6, §7.4).

Every job is **idempotent** and **bounded**: it processes at most ``batch_size``
files and returns (SPEC §6.14 "drain a bounded batch, not loop forever"). Each chunk
message is either deleted inline or, on a *transient* failure, handed to the retry
queue as a **self-contained** descriptor (it carries the chunk's ``channel_id`` /
``message_id`` / ``bot_id``, so finishing the delete needs no DB row). That lets the
file's rows be removed in a single pass while the queue finishes any straggler
message with a bounded number of attempts. Re-running is safe: a second pass finds
no rows, and deleting an already-gone Telegram message is tolerated.

The Telegram identifiers needed to delete a message live on each ``chunks`` row
(``channel_id``, ``message_id``, ``bot_id``; SPEC §4.4). This module reads rows via
``database`` repos and moves bytes via ``telegram`` — it never reaches into bot or
Redis internals (SPEC §6.14). Repos, the transport delete, and the queue are all
referenced as module globals so tests can substitute fakes (mirroring ``storage/``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telecloud import telegram
from telecloud.database import Database, chunks_repo, files_repo, get_service_db
from telecloud.rate_limit import queue as retry_queue
from telecloud.shared import ChunkMeta, FileMeta
from telecloud.telegram import TelegramError

logger = logging.getLogger("telecloud.jobs")

#: A ``pending`` file older than this is treated as an abandoned upload and swept
#: (SPEC §7.1 step 6, §7.4). Generous so a slow-but-live upload is never reaped.
DEFAULT_ORPHAN_AGE = timedelta(hours=24)

#: Upper bound on how many files a single job run processes before returning, so a
#: run is always bounded work (SPEC §6.14). The remainder is picked up next run.
DEFAULT_BATCH_SIZE = 50

#: Upper bound on queued retries a single run replays (see :func:`process_retries`).
DEFAULT_RETRY_BATCH = 100

#: Retry-queue op tag for a deferred Telegram-message delete (SPEC §6.6 payloads
#: are plain JSON dicts).
OP_DELETE_MESSAGE = "delete_message"

# Type alias for the injectable transport delete (matches telegram.delete_message).
DeleteMessage = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class CleanupResult:
    """What a cleanup job run reports back (returned by the routes as JSON).

    * :attr:`files_removed` — files cleaned this run (rows deleted).
    * :attr:`messages_deleted` — Telegram messages successfully deleted inline.
    * :attr:`messages_queued` — messages whose delete failed transiently and were
      enqueued for a later retry (the file's rows are still removed; the queue
      finishes the message).
    * :attr:`retries_processed` — queued retry ops replayed successfully this run.
    * :attr:`retries_dead_lettered` — retry ops parked in the dead-letter list this
      run after exhausting their attempts.
    """

    files_removed: int = 0
    messages_deleted: int = 0
    messages_queued: int = 0
    retries_processed: int = 0
    retries_dead_lettered: int = 0

    def as_dict(self) -> dict[str, int]:
        """Return the result as a JSON-serializable dict for the HTTP response."""
        return {
            "files_removed": self.files_removed,
            "messages_deleted": self.messages_deleted,
            "messages_queued": self.messages_queued,
            "retries_processed": self.retries_processed,
            "retries_dead_lettered": self.retries_dead_lettered,
        }


def _utcnow() -> datetime:
    """Current UTC time (indirection so tests can pin the clock)."""
    return datetime.now(timezone.utc)


def _delete_retry_job(chunk: ChunkMeta) -> dict:
    """Build the JSON retry descriptor for a failed chunk-message delete.

    Self-contained so a later run can replay the delete with no other context
    (SPEC §6.6 stores plain JSON dicts).
    """
    return {
        "op": OP_DELETE_MESSAGE,
        "channel_id": chunk.channel_id,
        "message_id": chunk.message_id,
        "bot_id": chunk.bot_id,
    }


async def _delete_file_messages(
    db: Database,
    file: FileMeta,
    *,
    delete_message: DeleteMessage,
    queue,
) -> tuple[int, int]:
    """Delete every Telegram message backing ``file``; report ``(deleted, queued)``.

    Walks the file's chunk rows and deletes each one's Telegram message:

    * **success** → counts toward ``deleted``;
    * **transient** failure (rate-limit/``429``/``5xx``/network) → enqueue a
      self-contained retry descriptor (``channel_id`` / ``message_id`` / ``bot_id``)
      and count it as ``queued``. The descriptor carries everything needed to finish
      the delete later, so the caller may still remove the rows now; the queue
      retries the straggler with a bounded number of attempts (SPEC §6.6, §7.4). We
      do *not* re-raise: one flaky chunk must not abort the whole batch;
    * **permanent** failure (e.g. the message is already gone, a ``400``) → treated
      as done for this message (retrying can't help); logged and counted as deleted.

    Returns how many messages were deleted inline and how many were queued for retry.
    """
    chunks = await chunks_repo.list_for_file(db, file.id)
    deleted = queued = 0
    for chunk in chunks:
        try:
            await delete_message(chunk.channel_id, chunk.message_id, bot_id=chunk.bot_id)
            deleted += 1
        except TelegramError as exc:
            if exc.transient:
                await queue.enqueue(_delete_retry_job(chunk))
                queued += 1
                logger.info(
                    "queued chunk %s delete for retry (transient: %s)",
                    chunk.id,
                    exc.message,
                )
            else:
                logger.warning(
                    "treating permanent delete failure for chunk %s as done (%s)",
                    chunk.id,
                    exc.message,
                )
                deleted += 1
    return deleted, queued


async def _cleanup_files(
    db: Database,
    files: list[FileMeta],
    *,
    delete_message: DeleteMessage,
    queue,
) -> tuple[int, int, int]:
    """Delete messages + rows for each file; return ``(removed, deleted, queued)``.

    Shared core of both cleanup jobs. Every chunk message is deleted inline or, on a
    transient failure, queued as a self-contained retry; either way the file's rows
    are then removed in a single pass (``delete_row`` cascades the chunk rows, SPEC
    §4.4). Removing the rows while a straggler message is still queued is safe — the
    descriptor doesn't depend on the rows — and keeps the job idempotent: a re-run
    finds no rows.
    """
    removed = deleted = queued = 0
    for file in files:
        d, q = await _delete_file_messages(
            db, file, delete_message=delete_message, queue=queue
        )
        deleted += d
        queued += q
        await files_repo.delete_row(db, file.id)
        removed += 1
    return removed, deleted, queued


# ---------------------------------------------------------------------------
# Retry drain
# ---------------------------------------------------------------------------


async def _replay(payload: dict, *, delete_message: DeleteMessage) -> None:
    """Re-run a queued retry op. Raises :class:`TelegramError` if it fails again."""
    op = payload.get("op")
    if op == OP_DELETE_MESSAGE:
        await delete_message(
            payload["channel_id"], payload["message_id"], bot_id=payload.get("bot_id")
        )
        return
    # Unknown op: nothing sane to retry. Drop it rather than loop on it forever.
    logger.warning("dropping unknown retry op: %r", op)


async def process_retries(
    *,
    queue=retry_queue,
    delete_message: DeleteMessage = telegram.delete_message,
    batch_size: int = DEFAULT_RETRY_BATCH,
) -> tuple[int, int]:
    """Replay up to ``batch_size`` queued retry ops; return ``(processed, dead)``.

    Drains the retry queue a **bounded** amount (SPEC §6.14): for each dequeued op it
    replays the Telegram delete. A success simply moves on (``dequeue`` already
    removed it). A failure is handed to :meth:`queue.mark_failed`, which re-enqueues
    it until it has been attempted ``max_attempts`` times and then parks it in the
    dead-letter list — so a permanently-failing op is retired instead of cycling
    forever (SPEC §6.6, §7.4).

    ``queue`` defaults to the shared ``rate_limit.queue`` module (whose module-level
    ``dequeue`` / ``mark_failed`` act on the process-wide queue); tests pass a
    ``RetryQueue`` instance or a fake exposing the same methods.
    """
    processed = 0
    dead = 0
    for _ in range(batch_size):
        job = await queue.dequeue()
        if job is None:
            break
        try:
            await _replay(job.payload, delete_message=delete_message)
            processed += 1
        except TelegramError as exc:
            # Transient or not, advance the attempt counter; mark_failed parks it in
            # the dead-letter list once attempts are exhausted. A permanent failure
            # won't get better, but the same attempt budget keeps it bounded.
            if await queue.mark_failed(job):
                dead += 1
                logger.warning(
                    "dead-lettered retry op after repeated failures: %s", exc.message
                )
    return processed, dead


# ---------------------------------------------------------------------------
# The two QStash jobs (SPEC §7.4)
# ---------------------------------------------------------------------------


async def sweep_orphans(
    *,
    db: Database | None = None,
    older_than: timedelta = DEFAULT_ORPHAN_AGE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    retry_batch: int = DEFAULT_RETRY_BATCH,
    delete_message: DeleteMessage = telegram.delete_message,
    queue=retry_queue,
    now: Callable[[], datetime] = _utcnow,
) -> CleanupResult:
    """Orphan sweep: reclaim abandoned ``pending`` uploads (SPEC §7.4).

    Finds ``files`` in ``pending`` created before ``now() - older_than`` (a
    two-phase commit that never finished, SPEC §7.1 step 6), deletes each one's
    Telegram messages, and removes the chunk + file rows. Drains a bounded batch of
    earlier retries first, then sweeps at most ``batch_size`` files; the rest wait
    for the next run. Idempotent.

    Runs against the **service-role** client (``db`` defaults to
    :func:`get_service_db`): a QStash job has no user JWT, and the rows it sweeps
    span all owners.
    """
    db = db or await get_service_db()
    processed, dead = await process_retries(
        queue=queue, delete_message=delete_message, batch_size=retry_batch
    )

    cutoff = now() - older_than
    candidates = await files_repo.find_pending_older_than(db, cutoff)
    batch = candidates[:batch_size]
    removed, deleted, queued = await _cleanup_files(
        db, batch, delete_message=delete_message, queue=queue
    )
    return CleanupResult(
        files_removed=removed,
        messages_deleted=deleted,
        messages_queued=queued,
        retries_processed=processed,
        retries_dead_lettered=dead,
    )


async def delete_deferred(
    *,
    db: Database | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    retry_batch: int = DEFAULT_RETRY_BATCH,
    delete_message: DeleteMessage = telegram.delete_message,
    queue=retry_queue,
) -> CleanupResult:
    """Deferred delete: finish soft-deleted files (SPEC §7.4).

    Finds ``files`` in ``deleting`` (``files/`` set this and **already decremented
    quota**, SPEC §6.12), deletes their Telegram messages, then removes the rows.
    Quota is deliberately **left untouched** so deferred deletes are never
    double-counted (SPEC §6.14 "Must NOT"). Drains a bounded batch of earlier
    retries first, then processes at most ``batch_size`` files. Idempotent.

    Service-role client by default, as with :func:`sweep_orphans` (a job has no user
    JWT and the ``deleting`` rows span all owners).
    """
    db = db or await get_service_db()
    processed, dead = await process_retries(
        queue=queue, delete_message=delete_message, batch_size=retry_batch
    )

    candidates = await files_repo.find_deleting(db)
    batch = candidates[:batch_size]
    removed, deleted, queued = await _cleanup_files(
        db, batch, delete_message=delete_message, queue=queue
    )
    return CleanupResult(
        files_removed=removed,
        messages_deleted=deleted,
        messages_queued=queued,
        retries_processed=processed,
        retries_dead_lettered=dead,
    )
