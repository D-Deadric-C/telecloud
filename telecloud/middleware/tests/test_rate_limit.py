"""Rate-limiting tests: the 429 path, the key, and fail-open behaviour.

The limiter is injected (no real Redis): a small fake admits ``limit`` calls per
key within the window, then blocks. We assert the over-limit response is the
SPEC §5.1 ``rate_limited`` 429 envelope, that distinct keys get independent
buckets, and that a limiter backend failure fails open (request passes).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from telecloud.shared import ErrorCode, TeleCloudError

from telecloud.middleware.rate_limit import (
    RateLimitMiddleware,
    client_ip,
    resolve_key,
)


class _FakeLimiter:
    """Admits up to ``limit`` calls per key (sticky), then blocks — enough to
    exercise the middleware without Redis or time."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def check(self, key: str, limit: int, window: float) -> bool:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key] <= limit


def _app(limiter, *, limit: int = 2, key_resolver=None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware,
        limit=limit,
        window_seconds=60.0,
        limiter_check=limiter.check,
        **({"key_resolver": key_resolver} if key_resolver else {}),
    )

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    return app


def test_blocks_with_rate_limited_envelope_after_limit():
    limiter = _FakeLimiter()
    # Fixed key so every request shares one bucket regardless of test-client IP.
    client = TestClient(
        _app(limiter, limit=2, key_resolver=lambda _r: _const("user:abc"))
    )

    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 200

    blocked = client.get("/ping")
    assert blocked.status_code == 429
    assert blocked.json() == {
        "error": {
            "code": ErrorCode.RATE_LIMITED.value,
            "message": "Too many requests. Please slow down.",
        }
    }


def test_distinct_keys_have_independent_buckets():
    limiter = _FakeLimiter()
    keys = iter(["user:a", "user:a", "user:b"])
    client = TestClient(
        _app(limiter, limit=1, key_resolver=lambda _r: _const(next(keys)))
    )

    assert client.get("/ping").status_code == 200  # user:a #1 ok
    assert client.get("/ping").status_code == 429  # user:a #2 blocked
    assert client.get("/ping").status_code == 200  # user:b #1 ok (own bucket)


def test_fails_open_when_limiter_backend_errors():
    class _BrokenLimiter:
        async def check(self, key: str, limit: int, window: float) -> bool:
            raise TeleCloudError.from_code(
                ErrorCode.INTERNAL_ERROR, "redis unreachable"
            )

    client = TestClient(_app(_BrokenLimiter(), limit=1))

    # Backend failure must not deny the request.
    assert client.get("/ping").status_code == 200


@pytest.mark.asyncio
async def test_resolve_key_falls_back_to_ip_without_token():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/ping",
        "headers": [],
        "client": ("203.0.113.7", 1234),
    }
    request = Request(scope)

    key = await resolve_key(request)

    assert key == "ip:203.0.113.7"


@pytest.mark.asyncio
async def test_resolve_key_ignores_invalid_bearer_and_uses_ip():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/ping",
        "headers": [(b"authorization", b"Bearer not-a-real-jwt")],
        "client": ("198.51.100.4", 9000),
    }
    request = Request(scope)

    # An undecodable token must not raise out of resolve_key; it falls back to IP.
    key = await resolve_key(request)

    assert key == "ip:198.51.100.4"


def test_client_ip_prefers_forwarded_for():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-forwarded-for", b"70.0.0.1, 10.0.0.1")],
        "client": ("10.0.0.9", 1),
    }
    assert client_ip(Request(scope)) == "70.0.0.1"


async def _const(value: str) -> str:
    """Return ``value`` as the resolved key (the resolver must be a coroutine)."""
    return value
