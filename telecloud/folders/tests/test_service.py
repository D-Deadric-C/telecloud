"""Tests for the ``folders/`` service (SPEC §6.11).

The DB is the in-memory ``FakeDatabase`` running the *real* ``folders_repo`` /
``files_repo`` (so query construction + model mapping match production); there is
no RLS in the fake, which is exactly why the service re-checks ownership itself.
``service.get_db`` is monkeypatched to hand back the shared fake. The file-deletion
entrypoint is a stub recorder, since ``files/`` is not built (the cascade calls it
through the injected port).

Required coverage: create under a parent, move-cycle rejection, and cascading
soft-delete marking descendants (+ handing their files to the deleter).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from telecloud.database import files_repo, folders_repo
from telecloud.database.tests._fake_client import FakeDatabase
from telecloud.shared import TeleCloudError, UserContext

import telecloud.folders.service as service
from telecloud.folders.service import (
    create_folder,
    list_contents,
    move_folder,
    rename_folder,
    soft_delete_folder,
    validate_name,
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


def _recorder() -> tuple[list, "object"]:
    """A fake FileDeleter that records the file ids it was asked to delete."""
    deleted: list[UUID] = []

    async def delete_file(u: UserContext, file_id: UUID, *, access_token: str) -> None:
        deleted.append(file_id)

    return deleted, delete_file


async def _add_committed_file(
    db: _FakeDB, *, owner_id: UUID, folder_id: UUID | None, name: str
) -> UUID:
    file = await files_repo.insert_pending(
        db, owner_id=owner_id, name=name, size_bytes=1, chunk_count=1, folder_id=folder_id
    )
    await files_repo.mark_committed(db, file.id)
    return file.id


# -- name validation --------------------------------------------------------
async def test_validate_name_trims_and_accepts():
    assert validate_name("  Photos  ") == "Photos"


@pytest.mark.parametrize("bad", ["", "   ", "a/b", "a\\b", ".", "..", "x\tnope"])
async def test_validate_name_rejects(bad: str):
    with pytest.raises(TeleCloudError) as excinfo:
        validate_name(bad)
    assert excinfo.value.code == "validation_error"


# -- create under a parent --------------------------------------------------
async def test_create_under_parent_sets_parent_id(db: _FakeDB, user: UserContext):
    root = await create_folder(user, access_token="jwt", name="root")

    child = await create_folder(
        user, access_token="jwt", name="child", parent_id=root.id
    )

    assert child.parent_id == root.id
    assert child.owner_id == user.id


async def test_create_root_has_no_parent(db: _FakeDB, user: UserContext):
    root = await create_folder(user, access_token="jwt", name="root")
    assert root.parent_id is None


async def test_create_under_missing_parent_is_not_found(db: _FakeDB, user: UserContext):
    with pytest.raises(TeleCloudError) as excinfo:
        await create_folder(user, access_token="jwt", name="x", parent_id=uuid4())
    assert excinfo.value.code == "not_found"


async def test_create_under_other_users_parent_is_not_found(
    db: _FakeDB, user: UserContext
):
    other = UserContext(id=uuid4(), email="o@b.com", email_verified=True)
    others_folder = await folders_repo.insert(db, owner_id=other.id, name="theirs")

    with pytest.raises(TeleCloudError) as excinfo:
        await create_folder(
            user, access_token="jwt", name="mine", parent_id=others_folder.id
        )
    assert excinfo.value.code == "not_found"


# -- listing ----------------------------------------------------------------
async def test_list_contents_returns_subfolders_and_files(
    db: _FakeDB, user: UserContext
):
    root = await create_folder(user, access_token="jwt", name="root")
    sub = await create_folder(user, access_token="jwt", name="sub", parent_id=root.id)
    fid = await _add_committed_file(db, owner_id=user.id, folder_id=root.id, name="f.txt")

    folders, files = await list_contents(user, access_token="jwt", folder_id=root.id)

    assert [f.id for f in folders] == [sub.id]
    assert [f.id for f in files] == [fid]


async def test_list_root_excludes_nested(db: _FakeDB, user: UserContext):
    root = await create_folder(user, access_token="jwt", name="root")
    await create_folder(user, access_token="jwt", name="sub", parent_id=root.id)

    folders, _ = await list_contents(user, access_token="jwt", folder_id=None)

    assert [f.id for f in folders] == [root.id]


# -- rename -----------------------------------------------------------------
async def test_rename_folder(db: _FakeDB, user: UserContext):
    root = await create_folder(user, access_token="jwt", name="root")

    renamed = await rename_folder(user, root.id, access_token="jwt", name="renamed")

    assert renamed.name == "renamed"


async def test_rename_other_users_folder_is_not_found(db: _FakeDB, user: UserContext):
    other = UserContext(id=uuid4(), email="o@b.com", email_verified=True)
    theirs = await folders_repo.insert(db, owner_id=other.id, name="theirs")

    with pytest.raises(TeleCloudError) as excinfo:
        await rename_folder(user, theirs.id, access_token="jwt", name="x")
    assert excinfo.value.code == "not_found"


# -- move (incl. cycle rejection) -------------------------------------------
async def test_move_to_new_parent(db: _FakeDB, user: UserContext):
    a = await create_folder(user, access_token="jwt", name="a")
    b = await create_folder(user, access_token="jwt", name="b")

    moved = await move_folder(user, b.id, access_token="jwt", new_parent_id=a.id)

    assert moved.parent_id == a.id


async def test_move_to_root(db: _FakeDB, user: UserContext):
    a = await create_folder(user, access_token="jwt", name="a")
    b = await create_folder(user, access_token="jwt", name="b", parent_id=a.id)

    moved = await move_folder(user, b.id, access_token="jwt", new_parent_id=None)

    assert moved.parent_id is None


async def test_move_into_self_is_rejected(db: _FakeDB, user: UserContext):
    a = await create_folder(user, access_token="jwt", name="a")

    with pytest.raises(TeleCloudError) as excinfo:
        await move_folder(user, a.id, access_token="jwt", new_parent_id=a.id)
    assert excinfo.value.code == "validation_error"


async def test_move_into_descendant_is_rejected(db: _FakeDB, user: UserContext):
    root = await create_folder(user, access_token="jwt", name="root")
    child = await create_folder(
        user, access_token="jwt", name="child", parent_id=root.id
    )
    grandchild = await create_folder(
        user, access_token="jwt", name="grandchild", parent_id=child.id
    )

    # Moving root under its own grandchild would form a cycle.
    with pytest.raises(TeleCloudError) as excinfo:
        await move_folder(
            user, root.id, access_token="jwt", new_parent_id=grandchild.id
        )
    assert excinfo.value.code == "validation_error"
    # The row was left untouched.
    unchanged = await folders_repo.get(db, root.id)
    assert unchanged is not None and unchanged.parent_id is None


# -- cascading soft-delete --------------------------------------------------
async def test_soft_delete_cascades_to_descendants_and_files(
    db: _FakeDB, user: UserContext
):
    root = await create_folder(user, access_token="jwt", name="root")
    child = await create_folder(
        user, access_token="jwt", name="child", parent_id=root.id
    )
    grandchild = await create_folder(
        user, access_token="jwt", name="grandchild", parent_id=child.id
    )
    f_root = await _add_committed_file(
        db, owner_id=user.id, folder_id=root.id, name="r.txt"
    )
    f_grand = await _add_committed_file(
        db, owner_id=user.id, folder_id=grandchild.id, name="g.txt"
    )
    deleted_files, deleter = _recorder()

    await soft_delete_folder(
        user, root.id, access_token="jwt", delete_file=deleter
    )

    # Every folder in the subtree is now soft-deleted.
    for fid in (root.id, child.id, grandchild.id):
        row = await folders_repo.get(db, fid)
        assert row is not None and row.deleted_at is not None
    # Every contained committed file was handed to files/'s deletion path.
    assert set(deleted_files) == {f_root, f_grand}


async def test_soft_delete_only_affects_targets_subtree(
    db: _FakeDB, user: UserContext
):
    target = await create_folder(user, access_token="jwt", name="target")
    sibling = await create_folder(user, access_token="jwt", name="sibling")
    f_sibling = await _add_committed_file(
        db, owner_id=user.id, folder_id=sibling.id, name="s.txt"
    )
    deleted_files, deleter = _recorder()

    await soft_delete_folder(
        user, target.id, access_token="jwt", delete_file=deleter
    )

    sibling_row = await folders_repo.get(db, sibling.id)
    assert sibling_row is not None and sibling_row.deleted_at is None
    assert f_sibling not in deleted_files


async def test_soft_delete_other_users_folder_is_not_found(
    db: _FakeDB, user: UserContext
):
    other = UserContext(id=uuid4(), email="o@b.com", email_verified=True)
    theirs = await folders_repo.insert(db, owner_id=other.id, name="theirs")
    _, deleter = _recorder()

    with pytest.raises(TeleCloudError) as excinfo:
        await soft_delete_folder(
            user, theirs.id, access_token="jwt", delete_file=deleter
        )
    assert excinfo.value.code == "not_found"
