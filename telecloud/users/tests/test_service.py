"""Tests for the ``users/`` service: profile reads (SPEC §6.5).

Email verification is Supabase-managed now (no custom token flow in this module),
so the only behavior left here is reading the caller's profile fresh through the
repo. The DB is faked with the in-memory ``FakeDatabase`` running the *real*
``profiles_repo``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from telecloud.database import profiles_repo
from telecloud.database.tests._fake_client import FakeDatabase
from telecloud.shared import TeleCloudError, UserContext

import telecloud.users.service as service
from telecloud.users.service import get_profile

pytestmark = pytest.mark.asyncio


class _FakeDB(FakeDatabase):
    async def aclose(self) -> None:  # the service closes the user client in `finally`
        pass


@pytest.fixture
def db() -> _FakeDB:
    return _FakeDB()


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, db: _FakeDB) -> None:
    """Point the service's DB accessor at the in-memory fake."""

    async def fake_get_db(_token: str) -> _FakeDB:
        return db

    monkeypatch.setattr(service, "get_db", fake_get_db)


# -- get_profile ------------------------------------------------------------
async def test_get_profile_reads_fresh_row(db: _FakeDB):
    uid = uuid4()
    await profiles_repo.insert(db, user_id=uid, email="a@b.com", email_verified=True)
    # A stale context (flag false) must be overridden by the fresh repo read.
    stale = UserContext(id=uid, email="a@b.com", email_verified=False)

    profile = await get_profile(stale, access_token="jwt")

    assert profile.email_verified is True


async def test_get_profile_missing_raises_not_found(db: _FakeDB):
    user = UserContext(id=uuid4(), email="a@b.com", email_verified=False)

    with pytest.raises(TeleCloudError) as excinfo:
        await get_profile(user, access_token="jwt")
    assert excinfo.value.code == "not_found"
    assert excinfo.value.http_status == 404
