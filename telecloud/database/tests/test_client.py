"""Tests for the client factories, the Database wrapper, and row encoding.

The factories are exercised with a stubbed ``create_async_client`` so no network
or real Supabase project is needed — we assert which key each factory uses and
that the user-scoped client forwards the JWT to PostgREST (the RLS hook).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID

import pytest

import telecloud.database.client as client_mod
from telecloud.database._encoding import to_jsonable
from telecloud.database.client import (
    Database,
    close_db_pool,
    close_service_db,
    get_db,
    get_service_db,
)

pytestmark = pytest.mark.asyncio


# -- encoding ---------------------------------------------------------------
class _Color(str, Enum):
    RED = "red"


async def test_to_jsonable_coerces_uuid_datetime_enum_and_keeps_none():
    uid = UUID("11111111-1111-1111-1111-111111111111")
    ts = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
    out = to_jsonable(
        {
            "id": uid,
            "when": ts,
            "color": _Color.RED,
            "folder_id": None,
            "n": 5,
            "ok": True,
        }
    )
    assert out == {
        "id": "11111111-1111-1111-1111-111111111111",
        "when": ts.isoformat(),
        "color": "red",
        "folder_id": None,
        "n": 5,
        "ok": True,
    }


# -- client factories -------------------------------------------------------
class _StubPostgrest:
    def __init__(self) -> None:
        self.auth_token: str | None = None
        self.closed = False

    def auth(self, token: str) -> None:
        self.auth_token = token

    async def aclose(self) -> None:
        self.closed = True


class _StubClient:
    def __init__(self, url: str, key: str, options: object) -> None:
        self.url, self.key, self.options = url, key, options
        self.postgrest = _StubPostgrest()


@pytest.fixture(autouse=True)
def _patch_create(monkeypatch: pytest.MonkeyPatch):
    created: list[_StubClient] = []

    async def fake_create(url: str, key: str, options: object) -> _StubClient:
        c = _StubClient(url, key, options)
        created.append(c)
        return c

    class _FakeSettings:
        supabase_url = "https://demo.supabase.co"
        supabase_anon_key = "anon-key"
        supabase_service_role_key = "service-key"

    monkeypatch.setattr(client_mod, "create_async_client", fake_create)
    monkeypatch.setattr(client_mod, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(client_mod, "_service_db", None)
    # Start each test with an empty user-client pool so acquisitions create fresh
    # stubs against this test's patched factory rather than reusing a prior one.
    monkeypatch.setattr(client_mod, "_user_pool", None)
    return created


async def test_get_db_uses_anon_key_and_forwards_jwt(_patch_create):
    db = await get_db("user.jwt.token")
    assert isinstance(db, Database) and db.is_service_role is False
    stub = _patch_create[-1]
    assert stub.key == "anon-key"
    assert stub.postgrest.auth_token == "user.jwt.token"


async def test_get_service_db_uses_service_key_and_is_cached(_patch_create):
    first = await get_service_db()
    second = await get_service_db()
    assert first is second  # cached singleton
    assert first.is_service_role is True
    assert _patch_create[-1].key == "service-key"
    assert len(_patch_create) == 1  # created once

    await close_service_db()


async def test_get_db_reuses_pooled_client_and_rescopes_jwt(_patch_create):
    # First acquisition creates a client; releasing it returns it to the pool.
    first = await get_db("token-a")
    stub = _patch_create[-1]
    await first.aclose()  # release back to the pool, not a real close
    assert stub.postgrest.closed is False

    # Second acquisition reuses the same underlying client (no new construction)
    # and re-applies the new caller's JWT.
    second = await get_db("token-b")
    assert len(_patch_create) == 1  # no second client constructed
    assert stub.postgrest.auth_token == "token-b"
    await second.aclose()


async def test_close_db_pool_closes_idle_clients(_patch_create):
    db = await get_db("jwt")
    stub = _patch_create[-1]
    await db.aclose()  # now idle in the pool

    await close_db_pool()
    assert stub.postgrest.closed is True
