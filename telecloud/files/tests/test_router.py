"""Tests for the ``files/`` router wiring + HTTP framing (SPEC §6.12, §7.1, §7.2).

Thin HTTP-level checks that routes call the service and frame responses correctly —
especially the range download's ``206`` + headers, which is ``files/``'s job (it
reflects ``storage``'s framing onto the wire and adds ``Content-Disposition``, SPEC
§6.9, §7.2). The service is stubbed (its logic lives in ``test_service.py``);
``auth.current_user`` / ``access_token`` are overridden via ``dependency_overrides``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from telecloud.auth import access_token, current_user
from telecloud.shared import FileMeta, FileStatus, UserContext
from telecloud.storage import DownloadResponse

import telecloud.files.service as service
from telecloud.files.router import router

_USER = UserContext(id=uuid4(), email="a@b.com", email_verified=True)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[current_user] = lambda: _USER
    app.dependency_overrides[access_token] = lambda: "jwt-token"
    return app


def _file(name: str = "f.bin", *, status: FileStatus = FileStatus.COMMITTED) -> FileMeta:
    return FileMeta(
        id=uuid4(),
        owner_id=_USER.id,
        folder_id=None,
        name=name,
        size_bytes=1000,
        mime_type="application/octet-stream",
        chunk_count=1,
        status=status,
        created_at=datetime.now(timezone.utc),
    )


async def _bytes(*pieces: bytes) -> AsyncIterator[bytes]:
    for piece in pieces:
        yield piece


# -- upload route -----------------------------------------------------------
def test_upload_route_returns_201_with_committed_file(monkeypatch):
    committed = _file("movie.mp4")

    async def fake_upload(user, *, access_token, name, size_bytes, stream,
                          folder_id=None, mime_type="application/octet-stream"):
        assert user is _USER and name == "movie.mp4"
        assert size_bytes == 4  # Content-Length of the body below
        assert mime_type == "video/mp4"
        return committed

    monkeypatch.setattr(service, "upload_file", fake_upload)

    resp = TestClient(_app()).post(
        "/files?name=movie.mp4",
        content=b"abcd",
        headers={"Content-Type": "video/mp4"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == str(committed.id)
    assert body["status"] == "committed"


def test_upload_without_content_length_is_validation_error(monkeypatch):
    async def fake_upload(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("service reached without a declared size")

    monkeypatch.setattr(service, "upload_file", fake_upload)

    # Force a streaming body so the client omits Content-Length.
    resp = TestClient(_app(), raise_server_exceptions=False).post(
        "/files?name=x.bin", content=_sync_stream()
    )
    # The TeleCloudError propagates (no middleware mounted here) → 500-class; with
    # raise_server_exceptions=False the response carries the unhandled error. We
    # assert the route did not succeed.
    assert resp.status_code >= 400


def _sync_stream():
    yield b"chunk-1"
    yield b"chunk-2"


# -- download route: 200 (no range) -----------------------------------------
def test_download_full_returns_200_with_headers(monkeypatch):
    download = DownloadResponse(
        stream=_bytes(b"hello", b"world"),
        size_bytes=10,
        content_length=10,
        is_partial=False,
        content_range=None,
        mime_type="text/plain",
    )

    async def fake_open(user, file_id, *, access_token, range_=None):
        assert range_ is None
        return download, "greeting.txt"

    monkeypatch.setattr(service, "open_file_download", fake_open)

    resp = TestClient(_app()).get(f"/files/{uuid4()}")

    assert resp.status_code == 200
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-length"] == "10"
    assert "content-range" not in resp.headers
    assert resp.headers["content-type"].startswith("text/plain")
    assert "greeting.txt" in resp.headers["content-disposition"]
    assert resp.content == b"helloworld"


# -- download route: 206 (range) --------------------------------------------
def test_download_range_returns_206_with_content_range(monkeypatch):
    download = DownloadResponse(
        stream=_bytes(b"partial"),
        size_bytes=1000,
        content_length=7,
        is_partial=True,
        content_range="bytes 0-6/1000",
        mime_type="application/octet-stream",
    )
    seen: dict = {}

    async def fake_open(user, file_id, *, access_token, range_=None):
        seen["range"] = range_
        return download, "data.bin"

    monkeypatch.setattr(service, "open_file_download", fake_open)

    resp = TestClient(_app()).get(
        f"/files/{uuid4()}", headers={"Range": "bytes=0-6"}
    )

    assert resp.status_code == 206
    assert seen["range"] == "bytes=0-6"  # the raw header is passed through
    assert resp.headers["content-range"] == "bytes 0-6/1000"
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-length"] == "7"
    assert "data.bin" in resp.headers["content-disposition"]
    assert resp.content == b"partial"


# -- delete route -----------------------------------------------------------
def test_delete_route_returns_204_and_calls_service(monkeypatch):
    captured: dict = {}

    async def fake_delete(user, file_id, *, access_token):
        captured["file_id"] = file_id
        captured["user"] = user

    monkeypatch.setattr(service, "soft_delete_file", fake_delete)

    fid = uuid4()
    resp = TestClient(_app()).delete(f"/files/{fid}")

    assert resp.status_code == 204
    assert captured["file_id"] == fid
    assert captured["user"] is _USER


# -- rename / move routes ---------------------------------------------------
def test_rename_route(monkeypatch):
    async def fake_rename(user, file_id, *, access_token, name):
        return _file(name)

    monkeypatch.setattr(service, "rename_file", fake_rename)

    resp = TestClient(_app()).patch(f"/files/{uuid4()}", json={"name": "renamed.txt"})

    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed.txt"


def test_move_route(monkeypatch):
    dest = uuid4()

    async def fake_move(user, file_id, *, access_token, new_folder_id):
        assert new_folder_id == dest
        return _file().model_copy(update={"folder_id": dest})

    monkeypatch.setattr(service, "move_file", fake_move)

    resp = TestClient(_app()).post(
        f"/files/{uuid4()}/move", json={"folder_id": str(dest)}
    )

    assert resp.status_code == 200
    assert resp.json()["folder_id"] == str(dest)
