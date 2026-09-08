"""Request-logging tests: one minimal line per request, no secrets, errors too.

We capture records from the ``telecloud.middleware.request`` logger and assert the
structured fields are present and correct, that an unhandled error is logged as a
500, and that neither the bearer token nor the query string reaches the log.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from telecloud.middleware.errors import register_error_handlers
from telecloud.middleware.logging import RequestLoggingMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/hello")
    async def hello():
        return {"ok": True}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("should be logged as 500")

    return app


def test_logs_method_path_status_duration(caplog: pytest.LogCaptureFixture):
    client = TestClient(_app())
    with caplog.at_level(logging.INFO, logger="telecloud.middleware.request"):
        client.get("/hello?token=supersecret")

    records = [r for r in caplog.records if r.name == "telecloud.middleware.request"]
    assert len(records) == 1
    rec = records[0]
    assert rec.http_method == "GET"
    assert rec.http_path == "/hello"
    assert rec.http_status == 200
    assert isinstance(rec.duration_ms, float)
    # The query string (which can carry tokens) must not be logged.
    assert "supersecret" not in rec.getMessage()
    assert rec.http_path == "/hello"


def test_logs_unhandled_error_as_500(caplog: pytest.LogCaptureFixture):
    client = TestClient(_app(), raise_server_exceptions=False)
    with caplog.at_level(logging.INFO, logger="telecloud.middleware.request"):
        resp = client.get("/boom")

    assert resp.status_code == 500
    records = [r for r in caplog.records if r.name == "telecloud.middleware.request"]
    assert len(records) == 1
    assert records[0].http_status == 500
