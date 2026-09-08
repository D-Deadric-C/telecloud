"""Tests for the ``users/`` router wiring (SPEC §6.5).

Thin HTTP-level checks that the route calls the service and shapes responses
correctly. The service function is stubbed (its logic is covered in
``test_service.py``); ``auth.current_user`` / ``auth.access_token`` are overridden
via FastAPI's ``dependency_overrides`` so no real JWT/DB is needed.

Verification is Supabase-managed, so the only ``users/`` route is ``GET
/users/me``; the old start/confirm verification routes are gone.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from telecloud.auth import access_token, current_user
from telecloud.shared import UserContext

import telecloud.users.service as service
from telecloud.users.router import router


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def test_me_returns_profile(monkeypatch):
    uid = uuid4()
    user = UserContext(id=uid, email="a@b.com", email_verified=True)

    async def fake_get_profile(u: UserContext, *, access_token: str) -> UserContext:
        assert u.id == uid and access_token == "jwt-token"
        return user

    monkeypatch.setattr(service, "get_profile", fake_get_profile)

    app = _app()
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[access_token] = lambda: "jwt-token"

    resp = TestClient(app).get("/users/me")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(uid)
    assert body["email"] == "a@b.com"
    assert body["email_verified"] is True
