"""Error-handling tests: the SPEC §5.1 envelope for both error paths.

A tiny app with the handlers registered is exercised end-to-end:

* a route raising ``TeleCloudError`` returns its code/message/status verbatim;
* a route raising a plain exception returns a generic ``internal_error`` 500 and
  does **not** leak the underlying message.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from telecloud.shared import ErrorCode, TeleCloudError

from telecloud.middleware.errors import register_error_handlers


def _app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/known")
    async def known():
        raise TeleCloudError.from_code(ErrorCode.NOT_FOUND, "No such file")

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret connection string leaked here")

    return app


def test_telecloud_error_renders_canonical_envelope():
    # raise_server_exceptions=False so the catch-all handler runs instead of the
    # test client re-raising.
    client = TestClient(_app(), raise_server_exceptions=False)

    resp = client.get("/known")

    assert resp.status_code == 404
    assert resp.json() == {"error": {"code": "not_found", "message": "No such file"}}


def test_unexpected_exception_becomes_internal_error_without_leaking():
    client = TestClient(_app(), raise_server_exceptions=False)

    resp = client.get("/boom")

    assert resp.status_code == 500
    body = resp.json()
    assert body == {
        "error": {"code": "internal_error", "message": "An internal error occurred."}
    }
    # The real cause must never reach the client.
    assert "secret connection string" not in resp.text
