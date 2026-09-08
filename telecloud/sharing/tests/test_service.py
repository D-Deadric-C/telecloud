"""Tests for the ``sharing/`` service: management + the public download path
(SPEC §6.13, §7.3).

The DB is the in-memory ``FakeDatabase`` running the *real* ``files_repo`` /
``shares_repo`` (so query construction + model mapping match production); there is
no RLS in the fake, which is exactly why the service re-checks ownership itself.
Both ``service.get_db`` (user-scoped) and ``service.get_service_db`` (the
sanctioned RLS bypass for the public path) are monkeypatched to hand back the
shared fake. ``storage.open_download`` is mocked at the seam — ``sharing/``
orchestrates it; its chunk-streaming logic is covered by ``storage/`` tests.

Required coverage: create→download happy path, revoked rejected, expired rejected,
over-limit rejected, and that no owner identity is returned on the public path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import AsyncIterator
from uuid import UUID, uuid4

import pytest

from telecloud import storage
from telecloud.database import files_repo, shares_repo
from telecloud.database.tests._fake_client import FakeDatabase
from telecloud.shared import FileMeta, TeleCloudError, UserContext
from telecloud.storage import DownloadResponse

import telecloud.sharing.service as service

pytestmark = pytest.mark.asyncio


class _FakeDB(FakeDatabase):
    async def aclose(self) -> None:  # the service closes the user client in `finally`
        pass


@pytest.fixture
def db() -> _FakeDB:
    return _FakeDB()


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, db: _FakeDB) -> None:
    async def fake_get_db(_token: str) -> _FakeDB:
        return db

    async def fake_get_service_db() -> _FakeDB:
        return db

    monkeypatch.setattr(service, "get_db", fake_get_db)
    monkeypatch.setattr(service, "get_service_db", fake_get_service_db)


@pytest.fixture
def user() -> UserContext:
    return UserContext(id=uuid4(), email="owner@example.com", email_verified=True)


async def _bytes(*pieces: bytes) -> AsyncIterator[bytes]:
    for piece in pieces:
        yield piece


async def _add_committed_file(
    db: _FakeDB, *, owner_id: UUID, name: str = "report.pdf", size_bytes: int = 100
) -> FileMeta:
    file = await files_repo.insert_pending(
        db, owner_id=owner_id, name=name, size_bytes=size_bytes, chunk_count=1,
    )
    return await files_repo.mark_committed(db, file.id)


def _fake_download(mime: str = "application/pdf") -> DownloadResponse:
    return DownloadResponse(
        stream=_bytes(b"hello", b"world"),
        size_bytes=10,
        content_length=10,
        is_partial=False,
        content_range=None,
        mime_type=mime,
    )


@pytest.fixture
def stub_open_download(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Stub ``storage.open_download``, recording ``(db, file_id, range_)`` per call."""
    calls: list[tuple] = []

    async def fake_open(db, file_id, range_=None):
        calls.append((db, file_id, range_))
        return _fake_download()

    monkeypatch.setattr(storage, "open_download", fake_open)
    return calls


# -- create → download happy path -------------------------------------------
async def test_create_then_download_happy_path(
    db: _FakeDB, user: UserContext, stub_open_download: list[tuple]
):
    file = await _add_committed_file(db, owner_id=user.id, name="report.pdf")

    share = await service.create_share(user, access_token="jwt", file_id=file.id)
    assert share.file_id == file.id
    assert share.token  # unguessable token generated
    assert share.download_count == 0

    download, filename = await service.open_share_download(share.token)

    # storage.open_download was driven with the share's file and the service db.
    assert stub_open_download == [(db, file.id, None)]
    # Filename (not owner identity) is returned for Content-Disposition.
    assert filename == "report.pdf"
    # The download was counted (SPEC §7.3).
    persisted = await shares_repo.resolve_by_token(db, share.token)
    assert persisted is not None and persisted.download_count == 1
    # The returned download carries only bytes + framing — no owner fields exist.
    assert not hasattr(download, "owner_id")


async def test_download_passes_range_through(
    db: _FakeDB, user: UserContext, stub_open_download: list[tuple]
):
    file = await _add_committed_file(db, owner_id=user.id)
    share = await service.create_share(user, access_token="jwt", file_id=file.id)

    await service.open_share_download(share.token, range_="bytes=0-4")

    assert stub_open_download == [(db, file.id, "bytes=0-4")]


# -- unknown token -----------------------------------------------------------
async def test_download_unknown_token_is_not_found(
    db: _FakeDB, stub_open_download: list[tuple]
):
    with pytest.raises(TeleCloudError) as excinfo:
        await service.open_share_download("does-not-exist")
    assert excinfo.value.code == "not_found"
    assert stub_open_download == []  # never reached storage


# -- revoked rejected --------------------------------------------------------
async def test_download_revoked_is_rejected(
    db: _FakeDB, user: UserContext, stub_open_download: list[tuple]
):
    file = await _add_committed_file(db, owner_id=user.id)
    share = await service.create_share(user, access_token="jwt", file_id=file.id)
    await service.revoke_share(user, share.id, access_token="jwt")

    with pytest.raises(TeleCloudError) as excinfo:
        await service.open_share_download(share.token)

    assert excinfo.value.code == "share_revoked"
    # Rejected before streaming and without counting the download.
    assert stub_open_download == []
    persisted = await shares_repo.resolve_by_token(db, share.token)
    assert persisted is not None and persisted.download_count == 0


# -- expired rejected --------------------------------------------------------
async def test_download_expired_is_rejected(
    db: _FakeDB, user: UserContext, stub_open_download: list[tuple]
):
    file = await _add_committed_file(db, owner_id=user.id)
    # Insert a share whose expiry is already in the past. (create_share rejects a
    # past expiry up front, so seed the row directly to simulate elapsed time.)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    share = await shares_repo.insert(
        db, file_id=file.id, owner_id=user.id, token="tok-expired", expires_at=past,
    )

    with pytest.raises(TeleCloudError) as excinfo:
        await service.open_share_download(share.token)

    assert excinfo.value.code == "share_expired"
    assert stub_open_download == []


# -- over download limit rejected -------------------------------------------
async def test_download_over_limit_is_rejected(
    db: _FakeDB, user: UserContext, stub_open_download: list[tuple]
):
    file = await _add_committed_file(db, owner_id=user.id)
    share = await service.create_share(
        user, access_token="jwt", file_id=file.id, download_limit=1
    )

    # First download is allowed and consumes the single permitted download.
    await service.open_share_download(share.token)
    # Second download is over the limit.
    with pytest.raises(TeleCloudError) as excinfo:
        await service.open_share_download(share.token)

    assert excinfo.value.code == "forbidden"
    # storage was hit exactly once (the allowed download), not the rejected one.
    assert len(stub_open_download) == 1


# -- no owner identity leaks on the public path -----------------------------
async def test_public_download_return_value_has_no_owner_identity(
    db: _FakeDB, user: UserContext, stub_open_download: list[tuple]
):
    file = await _add_committed_file(db, owner_id=user.id, name="secret.bin")
    share = await service.create_share(user, access_token="jwt", file_id=file.id)

    download, filename = await service.open_share_download(share.token)

    # Nothing the route can serialize from this tuple reveals the owner: the
    # DownloadResponse holds only bytes + framing metadata, and the only string is
    # the (non-identifying) file name.
    framing = (
        download.mime_type,
        str(download.content_length),
        str(download.size_bytes),
        download.content_range or "",
    )
    haystack = (filename + "|" + "|".join(framing)).lower()
    assert str(user.id).lower() not in haystack
    assert user.email.lower() not in haystack


# -- create validation -------------------------------------------------------
async def test_create_share_rejects_past_expiry(db: _FakeDB, user: UserContext):
    file = await _add_committed_file(db, owner_id=user.id)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)

    with pytest.raises(TeleCloudError) as excinfo:
        await service.create_share(
            user, access_token="jwt", file_id=file.id, expires_at=past
        )
    assert excinfo.value.code == "validation_error"


async def test_create_share_rejects_naive_expiry(db: _FakeDB, user: UserContext):
    file = await _add_committed_file(db, owner_id=user.id)
    naive = datetime(2999, 1, 1)  # no tzinfo

    with pytest.raises(TeleCloudError) as excinfo:
        await service.create_share(
            user, access_token="jwt", file_id=file.id, expires_at=naive
        )
    assert excinfo.value.code == "validation_error"


async def test_create_share_for_other_users_file_is_not_found(
    db: _FakeDB, user: UserContext
):
    other = UserContext(id=uuid4(), email="o@b.com", email_verified=True)
    theirs = await _add_committed_file(db, owner_id=other.id)

    with pytest.raises(TeleCloudError) as excinfo:
        await service.create_share(user, access_token="jwt", file_id=theirs.id)
    assert excinfo.value.code == "not_found"


async def test_create_share_for_unknown_file_is_not_found(
    db: _FakeDB, user: UserContext
):
    with pytest.raises(TeleCloudError) as excinfo:
        await service.create_share(user, access_token="jwt", file_id=uuid4())
    assert excinfo.value.code == "not_found"


# -- revoke / list ownership -------------------------------------------------
async def test_revoke_other_users_share_is_not_found(db: _FakeDB, user: UserContext):
    other = UserContext(id=uuid4(), email="o@b.com", email_verified=True)
    theirs_file = await _add_committed_file(db, owner_id=other.id)
    theirs_share = await shares_repo.insert(
        db, file_id=theirs_file.id, owner_id=other.id, token="tok-theirs",
    )

    with pytest.raises(TeleCloudError) as excinfo:
        await service.revoke_share(user, theirs_share.id, access_token="jwt")
    assert excinfo.value.code == "not_found"


async def test_revoke_is_soft_and_idempotent(db: _FakeDB, user: UserContext):
    file = await _add_committed_file(db, owner_id=user.id)
    share = await service.create_share(user, access_token="jwt", file_id=file.id)

    first = await service.revoke_share(user, share.id, access_token="jwt")
    second = await service.revoke_share(user, share.id, access_token="jwt")

    assert first.revoked is True and second.revoked is True
    # Soft: the row still exists (resolvable by token) after revocation.
    assert await shares_repo.resolve_by_token(db, share.token) is not None


async def test_list_shares_returns_files_links(db: _FakeDB, user: UserContext):
    file = await _add_committed_file(db, owner_id=user.id)
    s1 = await service.create_share(user, access_token="jwt", file_id=file.id)
    s2 = await service.create_share(user, access_token="jwt", file_id=file.id)

    listed = await service.list_shares(user, access_token="jwt", file_id=file.id)

    assert {s.id for s in listed} == {s1.id, s2.id}


async def test_list_shares_for_other_users_file_is_not_found(
    db: _FakeDB, user: UserContext
):
    other = UserContext(id=uuid4(), email="o@b.com", email_verified=True)
    theirs = await _add_committed_file(db, owner_id=other.id)

    with pytest.raises(TeleCloudError) as excinfo:
        await service.list_shares(user, access_token="jwt", file_id=theirs.id)
    assert excinfo.value.code == "not_found"
