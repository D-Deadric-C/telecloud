"""Integration test for ``register_middleware``: the whole pipeline on one app.

Wires everything via the public entry point and checks the pieces cooperate:
the §5.1 envelope for a route-raised ``TeleCloudError``, the ``rate_limited`` 429
once the (deliberately tiny) limit is exceeded, and a CORS header on responses.
The limiter backend and CORS origins are injected so the test needs no Redis or
real config.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from telecloud.shared import ErrorCode, TeleCloudError

from telecloud.middleware.registration import register_middleware


class _CountingLimiter:
    """Admits ``limit`` calls per key, then blocks — a Redis-free stand-in."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def check(self, key: str, limit: int, window: float) -> bool:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key] <= limit


def _client(*, limit: int) -> TestClient:
    app = FastAPI()

    @app.get("/files")
    async def files():
        return {"ok": True}

    @app.get("/missing")
    async def missing():
        raise TeleCloudError.from_code(ErrorCode.NOT_FOUND, "No such file")

    register_middleware(
        app,
        rate_limit=limit,
        rate_window_seconds=60.0,
        limiter_check=_CountingLimiter().check,
        cors_origins=["https://frontend.example"],
    )
    return TestClient(app, raise_server_exceptions=False)


def test_pipeline_renders_error_envelope():
    client = _client(limit=100)  # high limit: rate limiting stays out of the way

    resp = client.get("/missing")

    assert resp.status_code == 404
    assert resp.json() == {"error": {"code": "not_found", "message": "No such file"}}


def test_pipeline_rate_limits_and_sets_cors_header():
    client = _client(limit=1)

    ok = client.get("/files", headers={"Origin": "https://frontend.example"})
    assert ok.status_code == 200
    # CORS middleware echoes the allowed origin back.
    assert ok.headers.get("access-control-allow-origin") == "https://frontend.example"

    blocked = client.get("/files")
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == ErrorCode.RATE_LIMITED.value
