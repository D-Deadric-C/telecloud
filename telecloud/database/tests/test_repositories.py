"""Repository tests against the in-memory fake client.

These verify each repo builds the right query and maps rows to the shared read
models — not the live Supabase wire behavior (no RLS/constraints here). They lock
in the operations the SPEC §6/§7 contracts imply.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from telecloud.shared import (
    ChunkMeta,
    ChunkStatus,
    FileMeta,
    FileStatus,
    FolderMeta,
    ShareMeta,
    UserContext,
)

from telecloud.database import (
    chunks_repo,
    files_repo,
    folders_repo,
    profiles_repo,
    shares_repo,
)
from telecloud.database.tests._fake_client import FakeDatabase

pytestmark = pytest.mark.asyncio


@pytest.fixture
def db() -> FakeDatabase:
    return FakeDatabase()


# -- profiles ---------------------------------------------------------------
async def test_profiles_insert_and_get(db: FakeDatabase):
    uid = uuid4()
    created = await profiles_repo.insert(db, user_id=uid, email="a@b.com")
    assert isinstance(created, UserContext)
    assert created.email == "a@b.com" and created.email_verified is False

    fetched = await profiles_repo.get(db, uid)
    assert fetched is not None and fetched.id == uid


async def test_profiles_insert_verified(db: FakeDatabase):
    # email_verified is set by a DB trigger from auth.users in production
    # (migration 0005); the repo can still write it directly on insert.
    uid = uuid4()
    created = await profiles_repo.insert(
        db, user_id=uid, email="a@b.com", email_verified=True
    )
    assert created.email_verified is True


async def test_profiles_get_missing_returns_none(db: FakeDatabase):
    assert await profiles_repo.get(db, uuid4()) is None


async def test_profiles_adjust_storage_is_atomic_delta(db: FakeDatabase):
    uid = uuid4()
    await profiles_repo.insert(db, user_id=uid, email="a@b.com")
    assert await profiles_repo.get_storage_used(db, uid) == 0

    assert await profiles_repo.adjust_storage_used(db, uid, 1000) == 1000
    assert await profiles_repo.adjust_storage_used(db, uid, -400) == 600
    assert await profiles_repo.get_storage_used(db, uid) == 600


async def test_profiles_adjust_storage_missing_returns_none(db: FakeDatabase):
    assert await profiles_repo.adjust_storage_used(db, uuid4(), 10) is None


# -- folders ----------------------------------------------------------------
async def test_folders_crud_and_child_listing(db: FakeDatabase):
    owner = uuid4()
    root = await folders_repo.insert(db, owner_id=owner, name="root")
    child = await folders_repo.insert(
        db, owner_id=owner, name="child", parent_id=root.id
    )
    assert isinstance(root, FolderMeta) and child.parent_id == root.id

    roots = await folders_repo.list_children(db, owner_id=owner, parent_id=None)
    assert [f.name for f in roots] == ["root"]

    kids = await folders_repo.list_children(db, owner_id=owner, parent_id=root.id)
    assert [f.name for f in kids] == ["child"]

    renamed = await folders_repo.rename(db, child.id, "renamed")
    assert renamed is not None and renamed.name == "renamed"


async def test_folders_soft_delete_hides_from_listing(db: FakeDatabase):
    owner = uuid4()
    f = await folders_repo.insert(db, owner_id=owner, name="temp")
    deleted = await folders_repo.soft_delete(db, f.id)
    assert deleted is not None and deleted.deleted_at is not None

    visible = await folders_repo.list_children(db, owner_id=owner, parent_id=None)
    assert visible == []


# -- files ------------------------------------------------------------------
async def test_files_two_phase_and_listing(db: FakeDatabase):
    owner = uuid4()
    f = await files_repo.insert_pending(
        db, owner_id=owner, name="report.pdf", size_bytes=100, chunk_count=1
    )
    assert isinstance(f, FileMeta) and f.status is FileStatus.PENDING

    # pending files are not listed
    assert await files_repo.list_in_folder(db, owner_id=owner, folder_id=None) == []

    committed = await files_repo.mark_committed(db, f.id)
    assert committed is not None and committed.status is FileStatus.COMMITTED

    listed = await files_repo.list_in_folder(db, owner_id=owner, folder_id=None)
    assert [x.id for x in listed] == [f.id]


async def test_files_soft_delete_sets_deleting(db: FakeDatabase):
    owner = uuid4()
    f = await files_repo.insert_pending(
        db, owner_id=owner, name="x", size_bytes=1, chunk_count=1
    )
    await files_repo.mark_committed(db, f.id)
    deleting = await files_repo.mark_deleting(db, f.id)
    assert deleting is not None
    assert deleting.status is FileStatus.DELETING and deleting.deleted_at is not None
    # no longer visible
    assert await files_repo.list_in_folder(db, owner_id=owner, folder_id=None) == []


async def test_files_find_pending_older_than_and_find_deleting(db: FakeDatabase):
    owner = uuid4()
    old = await files_repo.insert_pending(
        db, owner_id=owner, name="old", size_bytes=1, chunk_count=1
    )
    # force the pending row to look old
    db.store["files"][0]["created_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()
    await files_repo.insert_pending(
        db, owner_id=owner, name="fresh", size_bytes=1, chunk_count=1
    )

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    stale = await files_repo.find_pending_older_than(db, cutoff)
    assert [x.id for x in stale] == [old.id]

    f2 = await files_repo.insert_pending(
        db, owner_id=owner, name="del", size_bytes=1, chunk_count=1
    )
    await files_repo.mark_deleting(db, f2.id)
    deleting = await files_repo.find_deleting(db)
    assert [x.id for x in deleting] == [f2.id]


async def test_files_delete_row(db: FakeDatabase):
    owner = uuid4()
    f = await files_repo.insert_pending(
        db, owner_id=owner, name="x", size_bytes=1, chunk_count=1
    )
    await files_repo.delete_row(db, f.id)
    assert await files_repo.get(db, f.id) is None


# -- chunks -----------------------------------------------------------------
async def test_chunks_insert_list_commit(db: FakeDatabase):
    file_id = uuid4()
    for i in range(3):
        await chunks_repo.insert_pending(
            db,
            file_id=file_id,
            chunk_index=2 - i,  # insert out of order
            size_bytes=18,
            channel_id=-1001,
            message_id=500 + i,
            telegram_file_id=f"tg{i}",
            bot_id="bot-a",
        )
    chunks = await chunks_repo.list_for_file(db, file_id)
    assert isinstance(chunks[0], ChunkMeta)
    assert [c.chunk_index for c in chunks] == [0, 1, 2]  # ordered
    assert all(c.status is ChunkStatus.PENDING for c in chunks)

    one = await chunks_repo.get_by_index(db, file_id=file_id, chunk_index=1)
    assert one is not None and one.chunk_index == 1

    committed = await chunks_repo.mark_all_committed(db, file_id)
    assert all(c.status is ChunkStatus.COMMITTED for c in committed)


async def test_chunks_delete_for_file(db: FakeDatabase):
    file_id = uuid4()
    await chunks_repo.insert_pending(
        db, file_id=file_id, chunk_index=0, size_bytes=1,
        channel_id=-1, message_id=1, telegram_file_id="t", bot_id="b",
    )
    await chunks_repo.delete_for_file(db, file_id)
    assert await chunks_repo.list_for_file(db, file_id) == []


# -- shares -----------------------------------------------------------------
async def test_shares_create_resolve_increment_revoke(db: FakeDatabase):
    owner, file_id = uuid4(), uuid4()
    share = await shares_repo.insert(
        db, file_id=file_id, owner_id=owner, token="tok-123"
    )
    assert isinstance(share, ShareMeta)
    assert share.download_count == 0 and share.revoked is False

    resolved = await shares_repo.resolve_by_token(db, "tok-123")
    assert resolved is not None and resolved.id == share.id

    assert await shares_repo.increment_download_count(db, share.id) == 1
    assert await shares_repo.increment_download_count(db, share.id) == 2

    revoked = await shares_repo.revoke(db, share.id)
    assert revoked is not None and revoked.revoked is True


async def test_shares_resolve_unknown_token(db: FakeDatabase):
    assert await shares_repo.resolve_by_token(db, "nope") is None
    assert await shares_repo.increment_download_count(db, uuid4()) is None
