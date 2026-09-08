"""Tests for signup/login/logout orchestration (SPEC §6.4).

Supabase and the DB are faked: a stub :class:`SupabaseAuth` records calls, and the
real ``profiles_repo`` runs against the in-memory ``FakeDatabase``. Verification is
Supabase-managed, so signup issues no session and creates no profile here (a DB
trigger does that); the focus is that signup reports "confirmation required",
login reflects the stored verification flag and ensures the shell, resend
forwards the email, and logout forwards the tokens.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from telecloud.database import profiles_repo
from telecloud.database.tests._fake_client import FakeDatabase

import telecloud.auth.service as service
from telecloud.auth.service import login, logout, resend_confirmation, set_auth, signup
from telecloud.auth.supabase_auth import AuthSession, SignupResult

pytestmark = pytest.mark.asyncio


class _FakeDB(FakeDatabase):
    async def aclose(self) -> None:  # the service closes the client in `finally`
        pass


class _FakeAuth:
    """Stub adapter recording calls and returning fixed results."""

    def __init__(self, session: AuthSession) -> None:
        self._session = session
        self.signed_out: list[tuple[str, str | None]] = []
        self.resent: list[str] = []

    async def sign_up(self, *, email: str, password: str) -> SignupResult:
        return SignupResult(
            user_id=self._session.user_id, email=email, confirmation_required=True
        )

    async def sign_in(self, *, email: str, password: str) -> AuthSession:
        return self._session

    async def resend_confirmation(self, *, email: str) -> None:
        self.resent.append(email)

    async def sign_out(self, *, access_token: str, refresh_token: str | None) -> None:
        self.signed_out.append((access_token, refresh_token))


@pytest.fixture
def db() -> _FakeDB:
    return _FakeDB()


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, db: _FakeDB):
    """Point the service at the fake DB; clear the adapter after each test."""

    async def fake_get_db(_token: str) -> _FakeDB:
        return db

    monkeypatch.setattr(service, "get_db", fake_get_db)
    yield
    set_auth(None)


def _session(user_id, email="a@b.com") -> AuthSession:
    return AuthSession(
        user_id=user_id,
        email=email,
        access_token="access.jwt",
        refresh_token="refresh.jwt",
        expires_in=3600,
    )


async def test_signup_reports_confirmation_required_and_creates_no_profile(db: _FakeDB):
    uid = uuid4()
    set_auth(_FakeAuth(_session(uid)))

    resp = await signup(email="a@b.com", password="password123")

    # No session is issued; the client is told to check their email.
    assert resp.confirmation_required is True
    assert resp.email == "a@b.com"
    assert resp.message
    # The app does NOT create the profile on signup (the auth.users trigger does).
    assert await profiles_repo.get(db, uid) is None
    assert db.store["profiles"] == []


async def test_resend_confirmation_forwards_email():
    adapter = _FakeAuth(_session(uuid4()))
    set_auth(adapter)

    await resend_confirmation(email="a@b.com")
    assert adapter.resent == ["a@b.com"]


async def test_login_reflects_stored_verified_flag(db: _FakeDB):
    uid = uuid4()
    await profiles_repo.insert(db, user_id=uid, email="a@b.com", email_verified=True)
    set_auth(_FakeAuth(_session(uid)))

    resp = await login(email="a@b.com", password="password123")
    assert resp.user.email_verified is True


async def test_login_ensures_profile_shell_when_missing(db: _FakeDB):
    uid = uuid4()
    set_auth(_FakeAuth(_session(uid)))

    resp = await login(email="a@b.com", password="password123")

    # Safety net: login creates the shell if the trigger somehow hasn't.
    stored = await profiles_repo.get(db, uid)
    assert stored is not None
    assert resp.access_token == "access.jwt"


async def test_logout_forwards_tokens_to_adapter():
    adapter = _FakeAuth(_session(uuid4()))
    set_auth(adapter)

    await logout(access_token="access.jwt", refresh_token="refresh.jwt")
    assert adapter.signed_out == [("access.jwt", "refresh.jwt")]
