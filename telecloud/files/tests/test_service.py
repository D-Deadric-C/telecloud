"""Tests for the ``files/`` service orchestration (SPEC §6.12, §7.1, §7.4).

The DB is the in-memory ``FakeDatabase`` running the *real* ``files_repo`` /
``folders_repo`` (so query construction + model mapping match production); there is
no RLS in the fake, which is exactly why the service re-checks ownership itself.
``service.get_db`` is monkeypatched to hand back the shared fake. ``quota`` and
``storage`` are mocked at the seam (``files/`` orchestrates them; their own logic is
covered in ``quota/`` / ``storage/`` tests).

Required coverage: upload happy path (mock quota/storage), the quota-reject path,
and soft-delete decrementing quota + enqueuing the deletion job.
"""

from __future__ import annotations

from typing import AsyncIterator
from uuid import UUID, uuid4

import pytest

from telecloud import quota, storage
from telecloud.database import files_repo, folders_repo
from telecloud.database.tests._fake_client import FakeDatabase
from telecloud.shared import (
    ErrorCode,
    FileMeta,
    FileStatus,
    TeleCloudError,
    UserContext,
)

import telecloud.files.service as service
from telecloud.files.service import (
    list_files,
    move_file,
    rename_file,
    soft_delete_file,
    upload_file,
    validate_file_name,
)

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

    monkeypatch.setattr(service, "get_db", fake_get_db)


@pytest.fixture
def user() -> UserContext:
    return UserContext(id=uuid4(), email="a@b.com", email_verified=True)


async def _stream(*pieces: bytes) -> AsyncIterator[bytes]:
    for piece in pieces:
        yield piece


async def _add_committed_file(
    db: _FakeDB, *, owner_id: UUID, folder_id: UUID | None = None,
    name: str = "f.bin", size_bytes: int = 100,
) -> FileMeta:
    file = await files_repo.insert_pending(
        db, owner_id=owner_id, name=name, size_bytes=size_bytes,
        chunk_count=1, folder_id=folder_id,
    )
    return await files_repo.mark_committed(db, file.id)


# -- name validation --------------------------------------------------------
async def test_validate_file_name_trims_and_keeps_extension():
    assert validate_file_name("  report.pdf  ") == "report.pdf"


@pytest.mark.parametrize("bad", ["", "   ", "a/b", "a\\b", ".", "..", "x\tnope"])
async def test_validate_file_name_rejects(bad: str):
    with pytest.raises(TeleCloudError) as excinfo:
        validate_file_name(bad)
    assert excinfo.value.code == "validation_error"


# -- upload: happy path (mock quota + storage) ------------------------------
async def test_upload_happy_path_checks_quota_stores_and_adds_usage(
    db: _FakeDB, user: UserContext, monkeypatch: pytest.MonkeyPatch
):
    calls: dict[str, object] = {}

    async def fake_check(u, size, *, access_token):
        calls["checked"] = (u.id, size)

    async def fake_store(passed_db, file_meta, stream):
        calls["stored_pending_status"] = file_meta.status
        # storage owns the commit (SPEC §7.1 step 5); mirror it on the fake.
        committed = await files_repo.mark_committed(passed_db, file_meta.id)
        return committed

    async def fake_add(u, delta, *, access_token):
        calls["added"] = (u.id, delta)
        return delta

    monkeypatch.setattr(quota, "check_can_upload", fake_check)
    monkeypatch.setattr(storage, "store_upload", fake_store)
    monkeypatch.setattr(quota, "add_usage", fake_add)

    committed = await upload_file(
        user,
        access_token="jwt",
        name="movie.mp4",
        size_bytes=512,
        stream=_stream(b"data"),
        mime_type="video/mp4",
    )

    assert committed.status == FileStatus.COMMITTED
    assert committed.name == "movie.mp4"
    assert committed.mime_type == "video/mp4"
    # Quota was checked before storing, with the declared size...
    assert calls["checked"] == (user.id, 512)
    # ...the row handed to storage was pending (the two-phase opener, SPEC §7.1)...
    assert calls["stored_pending_status"] == FileStatus.PENDING
    # ...and usage was added only after a committed return (SPEC §7.1 step 5).
    assert calls["added"] == (user.id, 512)


async def test_upload_validates_folder_ownership(
    db: _FakeDB, user: UserContext, monkeypatch: pytest.MonkeyPatch
):
    other = UserContext(id=uuid4(), email="o@b.com", email_verified=True)
    theirs = await folders_repo.insert(db, owner_id=other.id, name="theirs")

    # quota/storage should never be reached when the folder check fails.
    async def boom(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("quota/storage reached despite bad folder")

    monkeypatch.setattr(quota, "check_can_upload", boom)
    monkeypatch.setattr(storage, "store_upload", boom)

    with pytest.raises(TeleCloudError) as excinfo:
        await upload_file(
            user, access_token="jwt", name="x.bin", size_bytes=1,
            stream=_stream(b"x"), folder_id=theirs.id,
        )
    assert excinfo.value.code == "not_found"


# -- upload: quota reject ---------------------------------------------------
async def test_upload_quota_reject_short_circuits(
    db: _FakeDB, user: UserContext, monkeypatch: pytest.MonkeyPatch
):
    async def reject(u, size, *, access_token):
        raise TeleCloudError.from_code(ErrorCode.QUOTA_EXCEEDED, "over quota")

    stored = False

    async def fake_store(*a, **k):  # pragma: no cover - must not run
        nonlocal stored
        stored = True
        raise AssertionError("storage reached despite quota rejection")

    async def fake_add(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("usage added despite quota rejection")

    monkeypatch.setattr(quota, "check_can_upload", reject)
    monkeypatch.setattr(storage, "store_upload", fake_store)
    monkeypatch.setattr(quota, "add_usage", fake_add)

    with pytest.raises(TeleCloudError) as excinfo:
        await upload_file(
            user, access_token="jwt", name="big.bin",
            size_bytes=10**9, stream=_stream(b"x"),
        )

    assert excinfo.value.code == "quota_exceeded"
    assert stored is False
    # No pending row should linger when quota rejects before insert.
    assert db.store.get("files", []) == []


# -- listing ----------------------------------------------------------------
async def test_list_files_returns_committed_in_folder(
    db: _FakeDB, user: UserContext
):
    folder = await folders_repo.insert(db, owner_id=user.id, name="docs")
    f1 = await _add_committed_file(db, owner_id=user.id, folder_id=folder.id, name="a")
    # a pending file is not listed
    await files_repo.insert_pending(
        db, owner_id=user.id, name="pending", size_bytes=1, chunk_count=1,
        folder_id=folder.id,
    )

    files = await list_files(user, access_token="jwt", folder_id=folder.id)

    assert [f.id for f in files] == [f1.id]


# -- rename -----------------------------------------------------------------
async def test_rename_file(db: _FakeDB, user: UserContext):
    file = await _add_committed_file(db, owner_id=user.id, name="old.txt")

    renamed = await rename_file(user, file.id, access_token="jwt", name="new.txt")

    assert renamed.name == "new.txt"


async def test_rename_other_users_file_is_not_found(db: _FakeDB, user: UserContext):
    other = UserContext(id=uuid4(), email="o@b.com", email_verified=True)
    theirs = await _add_committed_file(db, owner_id=other.id, name="t.txt")

    with pytest.raises(TeleCloudError) as excinfo:
        await rename_file(user, theirs.id, access_token="jwt", name="x")
    assert excinfo.value.code == "not_found"


# -- move -------------------------------------------------------------------
async def test_move_file_to_folder(db: _FakeDB, user: UserContext):
    file = await _add_committed_file(db, owner_id=user.id)
    dest = await folders_repo.insert(db, owner_id=user.id, name="dest")

    moved = await move_file(user, file.id, access_token="jwt", new_folder_id=dest.id)

    assert moved.folder_id == dest.id


async def test_move_file_to_other_users_folder_is_not_found(
    db: _FakeDB, user: UserContext
):
    file = await _add_committed_file(db, owner_id=user.id)
    other = UserContext(id=uuid4(), email="o@b.com", email_verified=True)
    theirs = await folders_repo.insert(db, owner_id=other.id, name="theirs")

    with pytest.raises(TeleCloudError) as excinfo:
        await move_file(user, file.id, access_token="jwt", new_folder_id=theirs.id)
    assert excinfo.value.code == "not_found"


# -- soft-delete (decrement quota + enqueue) --------------------------------
async def test_soft_delete_marks_deleting_decrements_quota_and_enqueues(
    db: _FakeDB, user: UserContext, monkeypatch: pytest.MonkeyPatch
):
    file = await _add_committed_file(db, owner_id=user.id, size_bytes=4096)

    subtracted: list[tuple[UUID, int]] = []
    enqueued: list[UUID] = []

    async def fake_subtract(u, delta, *, access_token):
        subtracted.append((u.id, delta))
        return 0

    async def fake_enqueue(file_id: UUID) -> None:
        enqueued.append(file_id)

    monkeypatch.setattr(quota, "subtract_usage", fake_subtract)

    await soft_delete_file(
        user, file.id, access_token="jwt", enqueue_deletion=fake_enqueue
    )

    # Row is now soft-deleted (status deleting + deleted_at stamped).
    row = await files_repo.get(db, file.id)
    assert row is not None
    assert row.status == FileStatus.DELETING
    assert row.deleted_at is not None
    # Quota decremented by the file's size, and a deletion job enqueued.
    assert subtracted == [(user.id, 4096)]
    assert enqueued == [file.id]


async def test_soft_delete_other_users_file_is_not_found(
    db: _FakeDB, user: UserContext, monkeypatch: pytest.MonkeyPatch
):
    other = UserContext(id=uuid4(), email="o@b.com", email_verified=True)
    theirs = await _add_committed_file(db, owner_id=other.id)

    async def fake_subtract(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("quota touched for a file the caller doesn't own")

    async def fake_enqueue(file_id):  # pragma: no cover - must not run
        raise AssertionError("enqueued a file the caller doesn't own")

    monkeypatch.setattr(quota, "subtract_usage", fake_subtract)

    with pytest.raises(TeleCloudError) as excinfo:
        await soft_delete_file(
            user, theirs.id, access_token="jwt", enqueue_deletion=fake_enqueue
        )
    assert excinfo.value.code == "not_found"


async def test_soft_delete_unwired_enqueuer_fails_before_mutating(
    db: _FakeDB, user: UserContext, monkeypatch: pytest.MonkeyPatch
):
    # With no enqueuer injected and none registered, the resolve fails fast and
    # nothing is mutated (no half-deleted file).
    import telecloud.files.ports as ports

    monkeypatch.setattr(ports, "_deletion_enqueuer", None)
    file = await _add_committed_file(db, owner_id=user.id)

    async def fake_subtract(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("quota decremented before enqueuer resolved")

    monkeypatch.setattr(quota, "subtract_usage", fake_subtract)

    with pytest.raises(TeleCloudError) as excinfo:
        await soft_delete_file(user, file.id, access_token="jwt")
    assert excinfo.value.code == "internal_error"

    row = await files_repo.get(db, file.id)
    assert row is not None and row.status == FileStatus.COMMITTED
    assert row.deleted_at is None
