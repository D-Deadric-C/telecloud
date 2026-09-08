"""Tests for the sliding-window rate limiter.

A controllable clock is injected so window behavior is deterministic — no real
sleeping. The backend is the in-memory :class:`FakeRedis`, which mirrors the
production Lua script's semantics.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from telecloud.rate_limit.limiter import KEY_PREFIX, RateLimiter
from telecloud.rate_limit.tests._fake_redis import FakeRedis

pytestmark = pytest.mark.asyncio


class _Clock:
    """A mutable monotonic-ish clock returning epoch seconds."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _limiter(clock: _Clock) -> RateLimiter:
    return RateLimiter(FakeRedis(), time_func=clock)


async def test_allows_up_to_limit_then_denies_within_window():
    clock = _Clock()
    limiter = _limiter(clock)

    # 3 allowed within the 10s window...
    assert await limiter.check("user:1", limit=3, window=10) is True
    assert await limiter.check("user:1", limit=3, window=10) is True
    assert await limiter.check("user:1", limit=3, window=10) is True
    # ...the 4th is denied.
    assert await limiter.check("user:1", limit=3, window=10) is False


async def test_window_slides_so_capacity_returns():
    clock = _Clock()
    limiter = _limiter(clock)

    assert await limiter.check("k", limit=2, window=10) is True
    assert await limiter.check("k", limit=2, window=10) is True
    assert await limiter.check("k", limit=2, window=10) is False

    # Just before the window fully passes, still blocked.
    clock.advance(10)  # exactly one window later: oldest is now == now-window
    # The first entry (score t0) is at the cutoff and is evicted; capacity frees.
    assert await limiter.check("k", limit=2, window=10) is True


async def test_denied_calls_do_not_consume_capacity():
    clock = _Clock()
    limiter = _limiter(clock)

    assert await limiter.check("k", limit=1, window=10) is True
    # Several denied attempts...
    assert await limiter.check("k", limit=1, window=10) is False
    assert await limiter.check("k", limit=1, window=10) is False

    # ...record nothing, so one window later exactly one call succeeds again.
    clock.advance(11)
    assert await limiter.check("k", limit=1, window=10) is True
    assert await limiter.check("k", limit=1, window=10) is False


async def test_keys_are_independent_and_namespaced():
    clock = _Clock()
    backend = FakeRedis()
    limiter = RateLimiter(backend, time_func=clock)

    assert await limiter.check("a", limit=1, window=10) is True
    assert await limiter.check("a", limit=1, window=10) is False
    # A different key has its own budget.
    assert await limiter.check("b", limit=1, window=10) is True

    # Keys are stored under the limiter namespace.
    assert f"{KEY_PREFIX}:a" in backend._zsets
    assert f"{KEY_PREFIX}:b" in backend._zsets


async def test_window_accepts_timedelta():
    clock = _Clock()
    limiter = _limiter(clock)

    assert await limiter.check("k", limit=1, window=timedelta(seconds=5)) is True
    assert await limiter.check("k", limit=1, window=timedelta(seconds=5)) is False
    clock.advance(6)
    assert await limiter.check("k", limit=1, window=timedelta(seconds=5)) is True


async def test_same_millisecond_calls_are_distinct():
    # Clock never advances: two allowed calls land on the same timestamp and must
    # both be counted (unique members), so the third is denied.
    limiter = _limiter(_Clock())
    assert await limiter.check("k", limit=2, window=10) is True
    assert await limiter.check("k", limit=2, window=10) is True
    assert await limiter.check("k", limit=2, window=10) is False


@pytest.mark.parametrize("bad_limit", [0, -1])
async def test_non_positive_limit_rejected(bad_limit):
    limiter = _limiter(_Clock())
    with pytest.raises(ValueError):
        await limiter.check("k", limit=bad_limit, window=10)


@pytest.mark.parametrize("bad_window", [0, -5, timedelta(seconds=0)])
async def test_non_positive_window_rejected(bad_window):
    limiter = _limiter(_Clock())
    with pytest.raises(ValueError):
        await limiter.check("k", limit=1, window=bad_window)
