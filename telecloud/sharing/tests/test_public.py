"""Tests for the unauthenticated public download route ``GET /s/{token}`` (SPEC §7.3).

Thin HTTP-level checks that the route reflects ``storage``'s framing onto the wire
(200-vs-206, ``Content-Length`` / ``Content-Range`` / ``Accept-Ranges``), adds
``Content-Disposition``, passes the ``Range`` header through, and — per §6.13 —
exposes no owner identity. The service is stubbed (its gating logic is covered in
``test_service.py``); there is no auth dependency to override.
"""

from __future__ import annotations

from typing import AsyncIterator
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from telecloud.storage import DownloadResponse

import telecloud.sharing.service as service
from telecloud.sharing.public import public_router

_OWNER_EMAIL = "owner@example.com"
_OWNER_ID = uuid4()


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(public_router)
    return app


async def _bytes(*pieces: bytes) -> AsyncIterator[bytes]:
    for piece in pieces:
        yield piece


# -- full download (200) -----------------------------------------------------
def test_public_download_full_returns_200_with_headers(monkeypatch):
    download = DownloadResponse(
        stream=_bytes(b"hello", b"world"),
        size_bytes=10,
        content_length=10,
        is_partial=False,
        content_range=None,
        mime_type="text/plain",
    )

    async def fake_open(token, *, range_=None):
        assert token == "tok-abc" and range_ is None
        return download, "greeting.txt"

    monkeypatch.setattr(service, "open_share_download", fake_open)

    resp = TestClient(_app()).get("/s/tok-abc")

    assert resp.status_code == 200
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-length"] == "10"
    assert "content-range" not in resp.headers
    assert resp.headers["content-type"].startswith("text/plain")
    assert "greeting.txt" in resp.headers["content-disposition"]
    assert resp.content == b"helloworld"


# -- ranged download (206) ---------------------------------------------------
def test_public_download_range_returns_206_and_passes_header(monkeypatch):
    download = DownloadResponse(
        stream=_bytes(b"partial"),
        size_bytes=1000,
        content_length=7,
        is_partial=True,
        content_range="bytes 0-6/1000",
        mime_type="application/octet-stream",
    )
    seen: dict = {}

    async def fake_open(token, *, range_=None):
        seen["range"] = range_
        return download, "data.bin"

    monkeypatch.setattr(service, "open_share_download", fake_open)

    resp = TestClient(_app()).get("/s/tok-xyz", headers={"Range": "bytes=0-6"})

    assert resp.status_code == 206
    assert seen["range"] == "bytes=0-6"  # raw header passed through
    assert resp.headers["content-range"] == "bytes 0-6/1000"
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-length"] == "7"
    assert resp.content == b"partial"


# -- no owner identity on the wire ------------------------------------------
def test_public_download_response_leaks_no_owner_identity(monkeypatch):
    download = DownloadResponse(
        stream=_bytes(b"data"),
        size_bytes=4,
        content_length=4,
        is_partial=False,
        content_range=None,
        mime_type="application/octet-stream",
    )

    async def fake_open(token, *, range_=None):
        return download, "secret.bin"

    monkeypatch.setattr(service, "open_share_download", fake_open)

    resp = TestClient(_app()).get("/s/tok-abc")

    blob = (resp.text + "\n" + "\n".join(f"{k}: {v}" for k, v in resp.headers.items()))
    blob = blob.lower()
    assert _OWNER_EMAIL.lower() not in blob
    assert str(_OWNER_ID).lower() not in blob
