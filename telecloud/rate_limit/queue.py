"""Retry queue for failed background work (SPEC.md §6.6).

A small, generic durable queue backed by Redis lists. ``telegram/`` and
``jobs/`` use it to retry failed sends/deletes: a job that fails transiently is
re-enqueued, and once it has failed too many times it is moved to a
**dead-letter** list for inspection instead of looping forever.

Jobs are plain **JSON-serializable dicts** (SPEC §6.6) — this module knows
nothing about what's inside them (no Telegram specifics, no DB rows). Each job
is wrapped in an envelope that adds an id, an attempt counter, and an enqueue
timestamp; :class:`QueuedJob` is that envelope as returned by :meth:`dequeue`.

Lifecycle::

    enqueue(job)            -> pushes a fresh envelope (attempts=0) onto "ready"
    dequeue()               -> pops the oldest envelope (FIFO), or None
    mark_failed(queued)     -> attempts+1; re-enqueue, or dead-letter at the cap
    dead_letter()           -> read the dead-letter envelopes (for jobs/ ops)

A successfully processed job needs no call: :meth:`dequeue` already removed it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from telecloud.rate_limit.redis_client import RedisBackend, get_redis

#: List holding jobs waiting to be processed (FIFO: ``rpush`` tail, ``lpop`` head).
READY_KEY = "retryqueue:ready"

#: List holding jobs that exhausted their attempts, kept for inspection/manual
#: handling. SPEC calls this the "dead-letter set"; a list is used so order and
#: duplicate jobs are preserved.
DEAD_LETTER_KEY = "retryqueue:dead"

#: Total attempts a job gets before it is dead-lettered. With the default, a job
#: is retried up to 4 times after its first failure, then parked.
DEFAULT_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class QueuedJob:
    """One job as stored in the queue: the payload plus retry bookkeeping.

    * :attr:`id` — stable identifier assigned at enqueue, preserved across retries.
    * :attr:`payload` — the caller's original JSON-serializable dict.
    * :attr:`attempts` — failures recorded so far (``0`` when first enqueued).
    * :attr:`enqueued_at` — UTC ISO-8601 timestamp of the first enqueue.
    """

    id: str
    payload: dict
    attempts: int
    enqueued_at: str

    def to_json(self) -> str:
        """Serialize the envelope to the JSON string stored in Redis."""
        return json.dumps(
            {
                "id": self.id,
                "payload": self.payload,
                "attempts": self.attempts,
                "enqueued_at": self.enqueued_at,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> "QueuedJob":
        """Rebuild an envelope from its stored JSON string."""
        data = json.loads(raw)
        return cls(
            id=data["id"],
            payload=data["payload"],
            attempts=int(data.get("attempts", 0)),
            enqueued_at=data["enqueued_at"],
        )


class RetryQueue:
    """A durable retry queue over a :class:`RedisBackend`.

    Stateless aside from its backend and key names, so a single instance is
    safely shared. ``max_attempts`` controls when a repeatedly-failing job is
    dead-lettered.
    """

    def __init__(
        self,
        backend: RedisBackend,
        *,
        ready_key: str = READY_KEY,
        dead_letter_key: str = DEAD_LETTER_KEY,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._backend = backend
        self._ready_key = ready_key
        self._dead_letter_key = dead_letter_key
        self._max_attempts = max_attempts

    async def enqueue(self, job: dict) -> str:
        """Add ``job`` to the back of the ready queue; return its assigned id.

        ``job`` must be a JSON-serializable dict (SPEC §6.6). Raises
        :class:`ValueError` otherwise — that's a caller bug, not a runtime
        condition.
        """
        envelope = QueuedJob(
            id=uuid.uuid4().hex,
            payload=_validate_job(job),
            attempts=0,
            enqueued_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._backend.rpush(self._ready_key, envelope.to_json())
        return envelope.id

    async def dequeue(self) -> QueuedJob | None:
        """Pop and return the oldest queued job, or ``None`` if the queue is empty.

        Removing it from the ready list is the only "claim": process the
        returned job, and on failure hand it to :meth:`mark_failed` to retry or
        dead-letter it.
        """
        raw = await self._backend.lpop(self._ready_key)
        return None if raw is None else QueuedJob.from_json(raw)

    async def mark_failed(self, job: QueuedJob) -> bool:
        """Record a failed attempt for ``job`` and route it accordingly.

        Increments the attempt count. If the job still has attempts left it is
        re-enqueued at the back of the ready queue and ``False`` is returned. If
        it has now reached ``max_attempts`` it is moved to the dead-letter list
        and ``True`` is returned.
        """
        attempted = replace(job, attempts=job.attempts + 1)
        if attempted.attempts >= self._max_attempts:
            await self._backend.rpush(self._dead_letter_key, attempted.to_json())
            return True
        await self._backend.rpush(self._ready_key, attempted.to_json())
        return False

    async def dead_letter(self) -> list[QueuedJob]:
        """Return every job currently parked in the dead-letter list."""
        raws = await self._backend.lrange(self._dead_letter_key, 0, -1)
        return [QueuedJob.from_json(raw) for raw in raws]

    async def depth(self) -> int:
        """Return how many jobs are waiting in the ready queue."""
        return await self._backend.llen(self._ready_key)

    async def purge(self) -> None:
        """Delete both the ready and dead-letter lists (test/ops helper)."""
        await self._backend.delete(self._ready_key, self._dead_letter_key)


def _validate_job(job: dict) -> dict:
    """Ensure ``job`` is a JSON-serializable dict, returning it unchanged."""
    if not isinstance(job, dict):
        raise ValueError("job must be a dict")
    try:
        json.dumps(job)
    except (TypeError, ValueError) as exc:
        raise ValueError("job must be JSON-serializable") from exc
    return job


#: Process-wide queue bound to the shared Redis client, built on first use.
_queue: RetryQueue | None = None


def _get_queue() -> RetryQueue:
    global _queue
    if _queue is None:
        _queue = RetryQueue(get_redis())
    return _queue


async def enqueue(job: dict) -> str:
    """Module-level convenience for the shared queue (SPEC §6.6 public API)."""
    return await _get_queue().enqueue(job)


async def dequeue() -> QueuedJob | None:
    """Module-level convenience for the shared queue (SPEC §6.6 public API)."""
    return await _get_queue().dequeue()


async def mark_failed(job: QueuedJob) -> bool:
    """Module-level convenience for the shared queue. See :meth:`RetryQueue.mark_failed`."""
    return await _get_queue().mark_failed(job)


async def dead_letter() -> list[QueuedJob]:
    """Module-level convenience for the shared queue. See :meth:`RetryQueue.dead_letter`."""
    return await _get_queue().dead_letter()
