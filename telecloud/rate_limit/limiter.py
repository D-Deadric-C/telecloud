"""Generic sliding-window rate limiter (SPEC.md §6.6).

``check(key, limit, window)`` answers a single question: *may this caller take
one more action right now?* It is deliberately **generic** — the caller supplies
the key and the limits. This module hardcodes nothing about Telegram or app
routing (SPEC §6.6); ``middleware/`` uses it for app-level request limiting and
``telegram/`` uses it for Telegram's per-bot / per-channel limits, each passing
its own key and numbers.

The algorithm is a **sliding-window log**: each allowed action records a
timestamp in a Redis sorted set; an action is allowed only if fewer than
``limit`` timestamps fall within the trailing ``window``. The decision and the
record happen atomically in the backend (see
:data:`~telecloud.rate_limit.redis_client.SLIDING_WINDOW_SCRIPT`) so concurrent
callers can't both slip past the limit.
"""

from __future__ import annotations

import secrets
import time
from datetime import timedelta
from typing import Callable

from telecloud.rate_limit.redis_client import RedisBackend, get_redis

#: Namespace applied to every limiter key so rate-limit state can't collide with
#: the retry queue (or anything else) in the same Redis database.
KEY_PREFIX = "ratelimit"


def _window_to_ms(window: float | timedelta) -> int:
    """Normalize a window given as seconds or a :class:`timedelta` to integer ms.

    Raises :class:`ValueError` for a non-positive window.
    """
    seconds = window.total_seconds() if isinstance(window, timedelta) else float(window)
    if seconds <= 0:
        raise ValueError("window must be a positive duration")
    return int(seconds * 1000)


class RateLimiter:
    """A sliding-window limiter over a :class:`RedisBackend`.

    Stateless aside from its backend, so a single instance is safely shared. A
    ``time_func`` (returning epoch seconds) may be injected for deterministic
    tests; it defaults to :func:`time.time`.
    """

    def __init__(
        self,
        backend: RedisBackend,
        *,
        key_prefix: str = KEY_PREFIX,
        time_func: Callable[[], float] = time.time,
    ) -> None:
        self._backend = backend
        self._key_prefix = key_prefix
        self._time_func = time_func

    async def check(self, key: str, limit: int, window: float | timedelta) -> bool:
        """Return ``True`` if an action under ``key`` is allowed right now.

        Allows at most ``limit`` actions within any trailing ``window`` (seconds
        or a :class:`timedelta`). An allowed call is recorded against the window;
        a denied call records nothing. The key is namespaced under
        :data:`KEY_PREFIX`, so callers pass a bare logical key
        (e.g. ``"user:123"`` or ``"channel:-100…"``).

        Raises :class:`ValueError` for a non-positive ``limit`` or ``window``.
        Backend/transport failures surface as ``TeleCloudError`` for the caller
        to handle (the limiter has no opinion on fail-open vs fail-closed).
        """
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        window_ms = _window_to_ms(window)
        now_ms = int(self._time_func() * 1000)
        # Unique per call so two actions in the same millisecond are distinct
        # members of the sorted set (a duplicate member would just update the
        # score and undercount).
        member = f"{now_ms}-{secrets.token_hex(8)}"
        full_key = f"{self._key_prefix}:{key}"
        return await self._backend.sliding_window_allow(
            full_key, limit, window_ms, now_ms, member
        )


#: Process-wide limiter bound to the shared Redis client, built on first use.
_limiter: RateLimiter | None = None


def _get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(get_redis())
    return _limiter


async def check(key: str, limit: int, window: float | timedelta) -> bool:
    """Module-level convenience for the shared limiter (SPEC §6.6 public API).

    Equivalent to ``RateLimiter(get_redis()).check(...)`` but reuses one
    process-wide instance. See :meth:`RateLimiter.check`.
    """
    return await _get_limiter().check(key, limit, window)
