"""Tests for the authed share-management router wiring (SPEC §6.13).

Thin HTTP-level checks that routes call the service, frame responses, and — crucial
for §6.13 — never put owner identity on the wire. The service is stubbed (its logic
lives in ``test_service.py``); ``auth.current_user`` / ``access_token`` are
overridden via ``dependency_overrides``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from telecloud.auth import access_token, current_user
from telecloud.shared import ShareMeta, UserContext

import telecloud.sharing.service as service
from telecloud.sharing.router import router

_USER = UserContext(id=uuid4(), email="owner@example.com", email_verified=True)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[current_user] = lambda: _USER
    app.dependency_overrides[access_token] = lambda: "jwt-token"
    return app


def _share(*, revoked: bool = False, download_count: int = 0) -> ShareMeta:
    return ShareMeta(
        id=uuid4(),
        file_id=uuid4(),
        owner_id=_USER.id,
        token="secret-token",
        expires_at=None,
        download_limit=None,
        download_count=download_count,
        revoked=revoked,
        created_at=datetime.now(timezone.utc),
    )


# -- create ------------------------------------------------------------------
def test_create_route_returns_201_and_omits_owner(monkeypatch):
    share = _share()

    async def fake_create(user, *, access_token, file_id, expires_at, download_limit):
        assert user is _USER and file_id == share.file_id
        return share

    monkeypatch.setattr(service, "create_share", fake_create)

    resp = TestClient(_app()).post("/shares", json={"file_id": str(share.file_id)})

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == str(share.id)
    assert body["token"] == "secret-token"
    # SPEC §6.13: owner identity must never appear in the response.
    assert "owner_id" not in body
    assert _USER.email not in resp.text


def test_create_route_rejects_zero_download_limit(monkeypatch):
    async def fake_create(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("service reached despite invalid body")

    monkeypatch.setattr(service, "create_share", fake_create)

    resp = TestClient(_app()).post(
        "/shares", json={"file_id": str(uuid4()), "download_limit": 0}
    )
    # Field constraint (ge=1) is rejected by FastAPI before the service runs.
    assert resp.status_code == 422


# -- list --------------------------------------------------------------------
def test_list_route_requires_file_id_and_omits_owner(monkeypatch):
    file_id = uuid4()
    share = _share()

    async def fake_list(user, *, access_token, file_id):
        return [share]

    monkeypatch.setattr(service, "list_shares", fake_list)

    client = TestClient(_app())
    # file_id is a required query param.
    assert client.get("/shares").status_code == 422

    resp = client.get(f"/shares?file_id={file_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["file_id"] == str(file_id)
    assert [s["id"] for s in body["shares"]] == [str(share.id)]
    assert "owner_id" not in body["shares"][0]


# -- revoke ------------------------------------------------------------------
def test_revoke_route_returns_revoked_share(monkeypatch):
    revoked = _share(revoked=True)

    async def fake_revoke(user, share_id, *, access_token):
        assert share_id == revoked.id
        return revoked

    monkeypatch.setattr(service, "revoke_share", fake_revoke)

    resp = TestClient(_app()).post(f"/shares/{revoked.id}/revoke")

    assert resp.status_code == 200
    body = resp.json()
    assert body["revoked"] is True
    assert "owner_id" not in body
