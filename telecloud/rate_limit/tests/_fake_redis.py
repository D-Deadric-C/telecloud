"""An in-memory :class:`RedisBackend` for tests (no network, no real Redis).

Implements exactly the operations the limiter and queue use, with the same
semantics as the real Upstash client:

* :meth:`sliding_window_allow` mirrors the server-side Lua script — evict
  members older than the window, then admit only if under the limit.
* the list ops (``rpush``/``lpop``/``llen``/``lrange``) back the retry queue.

Being single-threaded asyncio, each method runs to completion without
interleaving, so the sliding-window read-then-write stays atomic just as the
Lua ``EVAL`` does in production.

There is precedent for an in-memory backend living beside tests rather than in
the package (``database/tests/_fake_client.py``).
"""

from __future__ import annotations


class FakeRedis:
    """A tiny in-memory stand-in implementing
    :class:`~telecloud.rate_limit.redis_client.RedisBackend`."""

    def __init__(self) -> None:
        # key -> {member: score_ms}
        self._zsets: dict[str, dict[str, int]] = {}
        # key -> list of values (index 0 is the head)
        self._lists: dict[str, list[str]] = {}

    async def sliding_window_allow(
        self, key: str, limit: int, window_ms: int, now_ms: int, member: str
    ) -> bool:
        zset = self._zsets.setdefault(key, {})
        cutoff = now_ms - window_ms
        # ZREMRANGEBYSCORE key 0 (now-window): inclusive of the upper bound.
        for old in [m for m, score in zset.items() if score <= cutoff]:
            del zset[old]
        if len(zset) < limit:
            zset[member] = now_ms
            return True
        return False

    async def rpush(self, key: str, *values: str) -> int:
        lst = self._lists.setdefault(key, [])
        lst.extend(values)
        return len(lst)

    async def lpop(self, key: str) -> str | None:
        lst = self._lists.get(key)
        if not lst:
            return None
        return lst.pop(0)

    async def llen(self, key: str) -> int:
        return len(self._lists.get(key, []))

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        lst = self._lists.get(key, [])
        # Redis LRANGE is inclusive of stop; -1 means the last element.
        end = len(lst) if stop == -1 else stop + 1
        return lst[start:end]

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self._zsets.pop(key, None) is not None:
                removed += 1
            if self._lists.pop(key, None) is not None:
                removed += 1
        return removed

    async def aclose(self) -> None:  # pragma: no cover - nothing to release
        pass
