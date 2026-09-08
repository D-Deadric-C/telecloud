"""Tests for the ``current_user`` and ``require_verified`` dependencies (SPEC §6.4).

The profile load and Supabase are stubbed: we sign a real token (so verification
runs for real) but substitute ``_load_profile`` so no DB/network is touched. Each
dependency is exercised on its happy path and its rejection path
(unauthorized / forbidden).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.security import HTTPAuthorizationCredentials

from telecloud.shared import TeleCloudError, UserContext

import telecloud.auth.dependencies as deps
import telecloud.auth.tokens as tokens_mod
from telecloud.auth.dependencies import (
    access_token,
    current_user,
    require_verified,
)
from telecloud.auth.tokens import encode_token

pytestmark = pytest.mark.asyncio

SECRET = "dep-test-secret-padded-to-32-bytes-minimum"


@pytest.fixture(autouse=True)
def _fake_secret(monkeypatch: pytest.MonkeyPatch):
    """Make ``verify_token``'s default secret deterministic (no real env)."""

    class _FakeSettings:
        supabase_jwt_secret = SECRET

    monkeypatch.setattr(tokens_mod, "get_settings", lambda: _FakeSettings())


def _token_for(user_id) -> str:
    return encode_token({"sub": str(user_id), "email": "a@b.com"}, secret=SECRET)


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _patch_profile(monkeypatch: pytest.MonkeyPatch, profile: UserContext | None):
    async def fake_load(user_id, token):
        return profile

    monkeypatch.setattr(deps, "_load_profile", fake_load)


# -- access_token -----------------------------------------------------------
async def test_access_token_returns_raw_token():
    assert await access_token(_creds("abc.def.ghi")) == "abc.def.ghi"


async def test_access_token_missing_header_is_unauthorized():
    with pytest.raises(TeleCloudError) as excinfo:
        await access_token(None)
    assert excinfo.value.code == "unauthorized"
    assert excinfo.value.http_status == 401


# -- current_user -----------------------------------------------------------
async def test_current_user_happy_returns_profile(monkeypatch: pytest.MonkeyPatch):
    uid = uuid4()
    profile = UserContext(id=uid, email="a@b.com", email_verified=True)
    _patch_profile(monkeypatch, profile)

    result = await current_user(token=_token_for(uid))
    assert result == profile


async def test_current_user_invalid_token_is_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_profile(monkeypatch, UserContext(id=uuid4(), email="x", email_verified=True))
    with pytest.raises(TeleCloudError) as excinfo:
        await current_user(token="garbage-token")
    assert excinfo.value.code == "unauthorized"
    assert excinfo.value.http_status == 401


async def test_current_user_no_profile_is_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
):
    uid = uuid4()
    _patch_profile(monkeypatch, None)  # token valid, but no profile row
    with pytest.raises(TeleCloudError) as excinfo:
        await current_user(token=_token_for(uid))
    assert excinfo.value.code == "unauthorized"
    assert excinfo.value.http_status == 401


# -- require_verified -------------------------------------------------------
async def test_require_verified_passes_verified_user():
    user = UserContext(id=uuid4(), email="a@b.com", email_verified=True)
    assert await require_verified(user=user) is user


async def test_require_verified_forbids_unverified_user():
    user = UserContext(id=uuid4(), email="a@b.com", email_verified=False)
    with pytest.raises(TeleCloudError) as excinfo:
        await require_verified(user=user)
    assert excinfo.value.code == "forbidden"
    assert excinfo.value.http_status == 403
