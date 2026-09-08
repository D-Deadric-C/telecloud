"""Tests for the ``folders/`` router wiring (SPEC §6.11).

Thin HTTP-level checks that routes call the service, shape responses, and resolve
the right dependencies. The service is stubbed (its logic is covered in
``test_service.py``); ``auth.current_user`` / ``access_token`` are overridden via
FastAPI ``dependency_overrides``, and the cascade's file-deletion port is supplied
through ``get_file_deleter`` (which would otherwise raise, since ``files/`` is
unbuilt) — proving the seam is wired but not entangled.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from telecloud.auth import access_token, current_user
from telecloud.shared import FolderMeta, UserContext

import telecloud.folders.service as service
from telecloud.folders.ports import get_file_deleter
from telecloud.folders.router import router

_USER = UserContext(id=uuid4(), email="a@b.com", email_verified=True)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[current_user] = lambda: _USER
    app.dependency_overrides[access_token] = lambda: "jwt-token"
    return app


def _folder(name: str, *, parent_id: UUID | None = None) -> FolderMeta:
    return FolderMeta(
        id=uuid4(),
        owner_id=_USER.id,
        parent_id=parent_id,
        name=name,
        created_at=datetime.now(timezone.utc),
    )


def test_create_route_returns_201_and_folder(monkeypatch):
    created = _folder("Photos")

    async def fake_create(user, *, access_token, name, parent_id=None):
        assert user is _USER and name == "Photos" and parent_id is None
        return created

    monkeypatch.setattr(service, "create_folder", fake_create)

    resp = TestClient(_app()).post("/folders", json={"name": "Photos"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == str(created.id)
    assert body["name"] == "Photos"
    assert "deleted_at" not in body  # response view drops it


def test_move_route_rejects_cycle_as_validation_error(monkeypatch):
    from telecloud.shared import ErrorCode, TeleCloudError

    async def fake_move(user, folder_id, *, access_token, new_parent_id):
        raise TeleCloudError.from_code(ErrorCode.VALIDATION_ERROR, "cycle")

    monkeypatch.setattr(service, "move_folder", fake_move)

    # The router does not format errors; assert it propagates the TeleCloudError
    # (middleware/ renders the envelope in the real app).
    app = _app()
    client = TestClient(app, raise_server_exceptions=True)
    target = uuid4()
    try:
        client.post(f"/folders/{target}/move", json={"new_parent_id": str(uuid4())})
        raised = None
    except TeleCloudError as exc:  # propagated, unhandled without middleware
        raised = exc
    assert raised is not None and raised.code == "validation_error"


def test_delete_route_uses_injected_file_deleter(monkeypatch):
    captured: dict = {}

    async def fake_soft_delete(user, folder_id, *, access_token, delete_file):
        captured["delete_file"] = delete_file

    async def my_deleter(user, file_id, *, access_token):  # the registered port
        pass

    monkeypatch.setattr(service, "soft_delete_folder", fake_soft_delete)

    app = _app()
    app.dependency_overrides[get_file_deleter] = lambda: my_deleter

    resp = TestClient(app).delete(f"/folders/{uuid4()}")

    assert resp.status_code == 204
    assert captured["delete_file"] is my_deleter
