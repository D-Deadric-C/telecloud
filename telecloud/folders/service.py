"""The virtual folder hierarchy: CRUD, listing, move, cascading delete (SPEC §6.11).

``folders/`` owns the adjacency-list tree (``public.folders``, SPEC §4.2): create
under an optional parent, list a folder's contents, rename, move (re-parent with
cycle rejection), and soft-delete cascading to descendant folders and their files.
Every operation is **owner-scoped** — reads/writes go through the RLS-honoring
user client (``database.get_db`` with the caller's JWT, SPEC §6.3), and each loaded
row is re-checked against ``user.id`` as defense-in-depth.

Boundaries (SPEC §6.11): this module does NOT manage file bytes, talk to Telegram,
or compute quota. The cascade hands each contained file to ``files/``'s deletion
entrypoint through the :mod:`telecloud.folders.ports` seam — it never marks files
``deleting`` or touches the ``files`` table writes itself (that path also owns the
quota decrement + Telegram-deletion job, SPEC §6.12, §7.4).

Dependencies are the §6.11 set: ``config`` (transitively), ``shared``,
``database``, and ``auth`` (the router only, for ``current_user``).
"""

from __future__ import annotations

from uuid import UUID

from telecloud.database import Database, files_repo, folders_repo, get_db
from telecloud.shared import (
    ErrorCode,
    FileMeta,
    FolderMeta,
    TeleCloudError,
    UserContext,
)

from telecloud.folders.ports import FileDeleter
from telecloud.folders.schemas import MAX_NAME_LENGTH

#: Characters never allowed in a (virtual) folder name. Path separators are
#: meaningless here — the hierarchy is modeled by ``parent_id``, not by a path —
#: so rejecting them avoids any later ambiguity in clients that join names.
_FORBIDDEN_CHARS = frozenset({"/", "\\"})


def validate_name(name: str) -> str:
    """Validate and normalize a folder name; return the trimmed value (SPEC §6.11).

    A name must be non-empty after trimming, within :data:`MAX_NAME_LENGTH`, free
    of path separators and control characters, and not the relative-path tokens
    ``.``/``..``. Raises ``validation_error`` (422) otherwise. This is the
    authoritative check — the schema's length bounds are just an early gate.
    """
    trimmed = name.strip()
    if not trimmed:
        raise _invalid_name("Folder name must not be empty.")
    if len(trimmed) > MAX_NAME_LENGTH:
        raise _invalid_name(f"Folder name must be at most {MAX_NAME_LENGTH} characters.")
    if any(ch in _FORBIDDEN_CHARS for ch in trimmed):
        raise _invalid_name("Folder name must not contain path separators.")
    if any(ord(ch) < 32 for ch in trimmed):
        raise _invalid_name("Folder name must not contain control characters.")
    if trimmed in {".", ".."}:
        raise _invalid_name("Folder name is reserved.")
    return trimmed


def _invalid_name(message: str) -> TeleCloudError:
    return TeleCloudError.from_code(ErrorCode.VALIDATION_ERROR, message)


def _not_found() -> TeleCloudError:
    # Use not_found (never forbidden) for rows the caller doesn't own so we never
    # confirm the existence of another user's folder.
    return TeleCloudError.from_code(ErrorCode.NOT_FOUND, "Folder not found.")


async def _load_owned_folder(
    db: Database, folder_id: UUID, user: UserContext
) -> FolderMeta:
    """Load a live folder the caller owns, or raise ``not_found``.

    Treats a missing row, another owner's row, and an already soft-deleted row
    identically (``not_found``) so the API leaks nothing about folders the caller
    can't act on.
    """
    folder = await folders_repo.get(db, folder_id)
    if folder is None or folder.owner_id != user.id or folder.deleted_at is not None:
        raise _not_found()
    return folder


# -- create -----------------------------------------------------------------
async def create_folder(
    user: UserContext,
    *,
    access_token: str,
    name: str,
    parent_id: UUID | None = None,
) -> FolderMeta:
    """Create a folder (root when ``parent_id`` is ``None``); return it (SPEC §6.11).

    Validates the name and, when a parent is given, that it exists, belongs to the
    caller, and is not deleted (``not_found`` otherwise).
    """
    clean_name = validate_name(name)
    db = await get_db(access_token)
    try:
        if parent_id is not None:
            await _load_owned_folder(db, parent_id, user)
        return await folders_repo.insert(
            db, owner_id=user.id, name=clean_name, parent_id=parent_id
        )
    finally:
        await db.aclose()


# -- list -------------------------------------------------------------------
async def list_contents(
    user: UserContext, *, access_token: str, folder_id: UUID | None = None
) -> tuple[list[FolderMeta], list[FileMeta]]:
    """List a folder's child folders and files (root when ``folder_id`` is ``None``).

    Returns ``(subfolders, files)`` — non-deleted child folders and the owner's
    ``committed`` files in this folder (SPEC §7.1). When listing a specific folder,
    validates it belongs to the caller and is live (``not_found`` otherwise).
    """
    db = await get_db(access_token)
    try:
        if folder_id is not None:
            await _load_owned_folder(db, folder_id, user)
        subfolders = await folders_repo.list_children(
            db, owner_id=user.id, parent_id=folder_id
        )
        files = await files_repo.list_in_folder(
            db, owner_id=user.id, folder_id=folder_id
        )
        return subfolders, files
    finally:
        await db.aclose()


# -- rename -----------------------------------------------------------------
async def rename_folder(
    user: UserContext, folder_id: UUID, *, access_token: str, name: str
) -> FolderMeta:
    """Rename a folder the caller owns; return the updated row (SPEC §6.11)."""
    clean_name = validate_name(name)
    db = await get_db(access_token)
    try:
        await _load_owned_folder(db, folder_id, user)
        updated = await folders_repo.rename(db, folder_id, clean_name)
        if updated is None:  # pragma: no cover - row vanished between read & write
            raise _not_found()
        return updated
    finally:
        await db.aclose()


# -- move -------------------------------------------------------------------
async def move_folder(
    user: UserContext,
    folder_id: UUID,
    *,
    access_token: str,
    new_parent_id: UUID | None,
) -> FolderMeta:
    """Re-parent a folder, rejecting cycles (SPEC §6.11).

    A folder cannot become its own parent or a descendant of itself. Validates
    that both the folder and (when given) the new parent belong to the caller and
    are live; a move that would form a cycle raises ``validation_error``.
    """
    db = await get_db(access_token)
    try:
        await _load_owned_folder(db, folder_id, user)
        if new_parent_id is not None:
            if new_parent_id == folder_id:
                raise _invalid_name("A folder cannot be moved into itself.")
            await _load_owned_folder(db, new_parent_id, user)
            if await _would_create_cycle(
                db, owner_id=user.id, folder_id=folder_id, new_parent_id=new_parent_id
            ):
                raise TeleCloudError.from_code(
                    ErrorCode.VALIDATION_ERROR,
                    "A folder cannot be moved into its own descendant.",
                )
        updated = await folders_repo.move(db, folder_id, new_parent_id=new_parent_id)
        if updated is None:  # pragma: no cover - row vanished between read & write
            raise _not_found()
        return updated
    finally:
        await db.aclose()


async def _would_create_cycle(
    db: Database, *, owner_id: UUID, folder_id: UUID, new_parent_id: UUID
) -> bool:
    """Return ``True`` if re-parenting ``folder_id`` under ``new_parent_id`` cycles.

    Walks the ancestor chain upward from the proposed parent: if ``folder_id``
    appears among its ancestors, the move would make the folder its own descendant.
    A visited set bounds the walk even if the data already holds a cycle.
    """
    current: UUID | None = new_parent_id
    seen: set[UUID] = set()
    while current is not None:
        if current == folder_id:
            return True
        if current in seen:
            break
        seen.add(current)
        node = await folders_repo.get(db, current)
        if node is None or node.owner_id != owner_id:
            break
        current = node.parent_id
    return False


# -- soft-delete (cascading) ------------------------------------------------
async def soft_delete_folder(
    user: UserContext,
    folder_id: UUID,
    *,
    access_token: str,
    delete_file: FileDeleter,
) -> None:
    """Soft-delete a folder, cascading to descendants and their files (SPEC §6.11).

    Collects the folder and every live descendant folder, hands each contained
    ``committed`` file to ``files/``'s deletion entrypoint (``delete_file`` — which
    owns the ``deleting`` mark, quota decrement, and Telegram-deletion job, SPEC
    §6.12), then stamps ``deleted_at`` on each folder. Files are handed off before
    their folder is marked deleted so none is stranded if the run is interrupted.

    ``delete_file`` is injected (the :mod:`telecloud.folders.ports` seam) so this
    module never imports ``files/`` and stays unit-testable.
    """
    db = await get_db(access_token)
    try:
        await _load_owned_folder(db, folder_id, user)
        subtree = await _collect_subtree(db, owner_id=user.id, root_id=folder_id)
        for node_id in subtree:
            files = await files_repo.list_in_folder(
                db, owner_id=user.id, folder_id=node_id
            )
            for file in files:
                await delete_file(user, file.id, access_token=access_token)
            await folders_repo.soft_delete(db, node_id)
    finally:
        await db.aclose()


async def _collect_subtree(
    db: Database, *, owner_id: UUID, root_id: UUID
) -> list[UUID]:
    """Return ``root_id`` plus all its live descendant folder ids (breadth-first).

    Uses the single-level ``list_children`` primitive (which already excludes
    soft-deleted rows) and a visited set so a malformed cycle can't loop forever.
    """
    ordered: list[UUID] = [root_id]
    seen: set[UUID] = {root_id}
    queue: list[UUID] = [root_id]
    while queue:
        parent_id = queue.pop(0)
        children = await folders_repo.list_children(
            db, owner_id=owner_id, parent_id=parent_id
        )
        for child in children:
            if child.id not in seen:
                seen.add(child.id)
                ordered.append(child.id)
                queue.append(child.id)
    return ordered
