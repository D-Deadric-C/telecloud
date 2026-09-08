"""Tests for the quota service: the pre-upload gate + usage accounting (SPEC §3, §6.10).

The DB is the in-memory ``FakeDatabase`` running the **real** ``profiles_repo``
(so usage reads and the atomic ``adjust_storage_used`` RPC behave as in
production), and ``users.get_profile`` is stubbed to read the verification flag off
that same fake profile row. Focus: the four ``check_can_upload`` outcomes
(unverified over per-file cap, over total cap, under both, verified unlimited) and
``add``/``subtract`` correctness including the no-negative floor.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from telecloud.config import MAX_FILE_SIZE_UNVERIFIED, QUOTA_UNVERIFIED_BYTES
from telecloud.database import profiles_repo
from telecloud.database.tests._fake_client import FakeDatabase
from telecloud.shared import ErrorCode, TeleCloudError, UserContext

import telecloud.quota.service as service
from telecloud.quota import add_usage, check_can_upload, subtract_usage

pytestmark = pytest.mark.asyncio


class _FakeDB(FakeDatabase):
    async def aclose(self) -> None:  # the service closes the user client in `finally`
        pass


@pytest.fixture
def db() -> _FakeDB:
    return _FakeDB()


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, db: _FakeDB) -> None:
    """Point the service's get_db / get_profile at the in-memory fake."""

    async def fake_get_db(_token: str) -> _FakeDB:
        return db

    async def fake_get_profile(user: UserContext, *, access_token: str) -> UserContext:
        profile = await profiles_repo.get(db, user.id)
        if profile is None:
            raise TeleCloudError.from_code(ErrorCode.NOT_FOUND, "Profile not found.")
        return profile

    monkeypatch.setattr(service, "get_db", fake_get_db)
    monkeypatch.setattr(service, "get_profile", fake_get_profile)


async def _seed(
    db: _FakeDB, *, verified: bool, used: int = 0
) -> UserContext:
    """Create a profile row and return a (possibly stale) UserContext for it."""
    uid = uuid4()
    await profiles_repo.insert(db, user_id=uid, email="a@b.com", email_verified=verified)
    if used:
        await profiles_repo.adjust_storage_used(db, uid, used)
    # Return a context whose flag may differ from the row to prove the service
    # reads the fresh profile, not the token.
    return UserContext(id=uid, email="a@b.com", email_verified=verified)


# -- check_can_upload: unverified -------------------------------------------
async def test_unverified_over_per_file_cap_raises_file_too_large(db: _FakeDB):
    user = await _seed(db, verified=False)

    with pytest.raises(TeleCloudError) as excinfo:
        await check_can_upload(
            user, MAX_FILE_SIZE_UNVERIFIED + 1, access_token="jwt"
        )
    assert excinfo.value.code == "file_too_large"


async def test_unverified_over_total_cap_raises_quota_exceeded(db: _FakeDB):
    # Already at the 500 MiB ceiling; even a 1-byte file overflows.
    user = await _seed(db, verified=False, used=QUOTA_UNVERIFIED_BYTES)

    with pytest.raises(TeleCloudError) as excinfo:
        await check_can_upload(user, 1, access_token="jwt")
    assert excinfo.value.code == "quota_exceeded"


async def test_unverified_under_both_caps_is_allowed(db: _FakeDB):
    user = await _seed(db, verified=False, used=1024)

    await check_can_upload(user, 2048, access_token="jwt")  # no raise


async def test_unverified_reads_fresh_verification_flag(db: _FakeDB):
    # Row is verified; the stale token context says unverified. The service must
    # trust the fresh profile and allow an otherwise-too-large upload.
    user = await _seed(db, verified=True)
    stale = UserContext(id=user.id, email="a@b.com", email_verified=False)

    await check_can_upload(
        stale, MAX_FILE_SIZE_UNVERIFIED * 10, access_token="jwt"
    )  # no raise — fresh row is verified


# -- check_can_upload: verified ---------------------------------------------
async def test_verified_is_unlimited(db: _FakeDB):
    user = await _seed(db, verified=True, used=QUOTA_UNVERIFIED_BYTES)

    # Far past both unverified caps — verified accounts have no cap.
    await check_can_upload(
        user, MAX_FILE_SIZE_UNVERIFIED * 5, access_token="jwt"
    )  # no raise


async def test_check_can_upload_missing_profile_raises_not_found(db: _FakeDB):
    ghost = UserContext(id=uuid4(), email="a@b.com", email_verified=False)

    with pytest.raises(TeleCloudError) as excinfo:
        await check_can_upload(ghost, 1024, access_token="jwt")
    assert excinfo.value.code == "not_found"


# -- add_usage / subtract_usage ---------------------------------------------
async def test_add_usage_increments_and_returns_new_total(db: _FakeDB):
    user = await _seed(db, verified=False, used=100)

    new_total = await add_usage(user, 250, access_token="jwt")

    assert new_total == 350
    assert await profiles_repo.get_storage_used(db, user.id) == 350


async def test_subtract_usage_decrements_and_returns_new_total(db: _FakeDB):
    user = await _seed(db, verified=False, used=500)

    new_total = await subtract_usage(user, 200, access_token="jwt")

    assert new_total == 300
    assert await profiles_repo.get_storage_used(db, user.id) == 300


async def test_subtract_usage_never_goes_negative(db: _FakeDB):
    user = await _seed(db, verified=False, used=100)

    new_total = await subtract_usage(user, 250, access_token="jwt")

    assert new_total == 0
    # The stored column must be floored at zero, not left negative.
    assert await profiles_repo.get_storage_used(db, user.id) == 0


async def test_subtract_exact_balance_lands_on_zero(db: _FakeDB):
    user = await _seed(db, verified=False, used=4096)

    new_total = await subtract_usage(user, 4096, access_token="jwt")

    assert new_total == 0


async def test_add_usage_rejects_negative_delta(db: _FakeDB):
    user = await _seed(db, verified=False)

    with pytest.raises(ValueError):
        await add_usage(user, -1, access_token="jwt")


async def test_subtract_usage_rejects_negative_delta(db: _FakeDB):
    user = await _seed(db, verified=False)

    with pytest.raises(ValueError):
        await subtract_usage(user, -1, access_token="jwt")


async def test_zero_delta_is_noop_returning_current_usage(db: _FakeDB):
    user = await _seed(db, verified=False, used=777)

    assert await add_usage(user, 0, access_token="jwt") == 777
    assert await subtract_usage(user, 0, access_token="jwt") == 777
    assert await profiles_repo.get_storage_used(db, user.id) == 777
