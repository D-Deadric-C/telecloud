"""``telecloud.rate_limit`` — Upstash Redis limiter + retry queue (SPEC.md §6.6).

Two generic primitives backed by Upstash Redis, used by ``middleware/``,
``telegram/`` and ``jobs/``:

* :mod:`limiter` — a sliding-window rate limiter. ``limiter.check(key, limit,
  window)`` returns whether one more action under ``key`` is allowed. Generic:
  the caller supplies the key and numbers; nothing here is Telegram-specific.
* :mod:`queue` — a durable retry queue for failed background work.
  ``queue.enqueue(job)`` / ``queue.dequeue()`` move JSON-serializable dicts, with
  attempt tracking and a dead-letter list after repeated failures.

Both share one process-wide async Redis client
(:func:`~telecloud.rate_limit.redis_client.get_redis`); call :func:`close` at
shutdown to release it.

**Boundaries (SPEC §6.6):** this package knows nothing about Telegram specifics,
HTTP routing, or DB rows, and depends only on ``config`` and ``shared``.
"""

from telecloud.rate_limit import limiter, queue
from telecloud.rate_limit.limiter import RateLimiter
from telecloud.rate_limit.queue import QueuedJob, RetryQueue
from telecloud.rate_limit.redis_client import (
    RedisBackend,
    UpstashRedis,
    close_redis,
    get_redis,
)

#: Release the shared Redis client's HTTP resources (call at app shutdown).
close = close_redis

__all__ = [
    # public interfaces (SPEC §6.6)
    "limiter",
    "queue",
    # building blocks for wiring / tests
    "RateLimiter",
    "RetryQueue",
    "QueuedJob",
    "RedisBackend",
    "UpstashRedis",
    "get_redis",
    "close",
]
