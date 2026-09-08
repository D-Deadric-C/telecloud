"""File lifecycle orchestration: upload, download, list, rename, move, delete (SPEC §6.12).

``files/`` is the orchestrator that ties ``quota`` + ``storage`` + ``folders``
together (SPEC §6.12). It owns the *decisions* and the request-scoped DB lifecycle;
the heavy lifting is delegated:

* **quota** decides whether an upload may proceed and keeps usage accurate
  (``check_can_upload`` / ``add_usage`` / ``subtract_usage``, SPEC §3, §7.1, §7.4).
* **storage** chunks the upload stream to Telegram and two-phase-commits it, and
  streams ranged downloads back (SPEC §7.1, §7.2). ``files/`` never talks to
  Telegram directly (SPEC §6.12).
* The ``database`` repos move the ``files`` rows between states.

Every operation is **owner-scoped**: reads/writes go through the RLS-honoring user
client (``database.get_db`` with the caller's JWT, SPEC §6.3), and each loaded row
is re-checked against ``user.id`` as defense-in-depth (the in-memory test fake has
no RLS). Errors raised here surface as :class:`TeleCloudError`; ``middleware/``
renders the canonical JSON envelope (SPEC §5.1).

Boundaries (SPEC §6.12): does NOT reimplement quota math or chunking, and does NOT
delete Telegram messages inline — soft-delete enqueues a deferred ``jobs/`` deletion
job through the :mod:`telecloud.files.ports` seam (FLAGGED: ``jobs/`` is unbuilt;
see ``files/README.md``).
"""

from __future__ import annotations

from typing import AsyncIterator
from uuid import UUID

from telecloud import quota, storage
from telecloud.database import Database, files_repo, folders_repo, get_db
from telecloud.shared import (
    ErrorCode,
    FileMeta,
    FileStatus,
    TeleCloudError,
    UserContext,
    compute_chunk_count,
)
from telecloud.storage import ByteRange, DownloadResponse

from telecloud.files.ports import DeletionEnqueuer, get_deletion_enqueuer
from telecloud.files.schemas import MAX_NAME_LENGTH

#: Characters never allowed in a (virtual) file name. The hierarchy is modeled by
#: ``folder_id``, not by a path, so path separators are meaningless and rejected to
#: avoid any later ambiguity in clients that join names into paths.
_FORBIDDEN_CHARS = frozenset({"/", "\\"})

#: Default MIME when the client declares none (SPEC §4.3 column default).
DEFAULT_MIME_TYPE = "application/octet-stream"


def validate_file_name(name: str) -> str:
    """Validate and normalize a file name; return the trimmed value (SPEC §6.12).

    A name must be non-empty after trimming, within :data:`MAX_NAME_LENGTH`, free
    of path separators and control characters, and not the relative-path tokens
    ``.``/``..``. Raises ``validation_error`` (422) otherwise. Mirrors the folder
    name rule so the two namespaces validate consistently.
    """
    trimmed = name.strip()
    if not trimmed:
        raise _invalid("File name must not be empty.")
    if len(trimmed) > MAX_NAME_LENGTH:
        raise _invalid(f"File name must be at most {MAX_NAME_LENGTH} characters.")
    if any(ch in _FORBIDDEN_CHARS for ch in trimmed):
        raise _invalid("File name must not contain path separators.")
    if any(ord(ch) < 32 for ch in trimmed):
        raise _invalid("File name must not contain control characters.")
    if trimmed in {".", ".."}:
        raise _invalid("File name is reserved.")
    return trimmed


def _invalid(message: str) -> TeleCloudError:
    return TeleCloudError.from_code(ErrorCode.VALIDATION_ERROR, message)


def _not_found() -> TeleCloudError:
    # Use not_found (never forbidden) for rows the caller doesn't own so we never
    # confirm the existence of another user's file (SPEC §6.13 privacy posture).
    return TeleCloudError.from_code(ErrorCode.NOT_FOUND, "File not found.")


async def _load_owned_file(
    db: Database, file_id: UUID, user: UserContext, *, statuses: tuple[FileStatus, ...]
) -> FileMeta:
    """Load a live file the caller owns in one of ``statuses``, or raise ``not_found``.

    Treats a missing row, another owner's row, an already soft-deleted row, and a
    row in an unexpected status identically (``not_found``) so the API leaks nothing
    about files the caller can't act on.
    """
    file = await files_repo.get(db, file_id)
    if (
        file is None
        or file.owner_id != user.id
        or file.deleted_at is not None
        or file.status not in statuses
    ):
        raise _not_found()
    return file


async def _assert_owned_folder(
    db: Database, folder_id: UUID, user: UserContext
) -> None:
    """Validate that ``folder_id`` exists, is live, and belongs to the caller.

    Raises ``not_found`` otherwise. Resolved via the ``database`` folder repo so
    ``files/`` never reaches into ``folders/`` internals; ownership is re-checked
    here as defense-in-depth on top of RLS.
    """
    folder = await folders_repo.get(db, folder_id)
    if folder is None or folder.owner_id != user.id or folder.deleted_at is not None:
        raise TeleCloudError.from_code(ErrorCode.NOT_FOUND, "Folder not found.")


# -- upload -----------------------------------------------------------------
async def upload_file(
    user: UserContext,
    *,
    access_token: str,
    name: str,
    size_bytes: int,
    stream: AsyncIterator[bytes],
    folder_id: UUID | None = None,
    mime_type: str = DEFAULT_MIME_TYPE,
) -> FileMeta:
    """Two-phase-commit an upload and return the committed file (SPEC §7.1).

    The orchestration of SPEC §7.1, in order:

    1. validate the name and (when given) that ``folder_id`` is the caller's live
       folder;
    2. ``quota.check_can_upload`` — reject early on ``file_too_large`` /
       ``quota_exceeded`` *before* a single byte is read;
    3. create the ``pending`` ``files`` row (``chunk_count`` from the declared
       size);
    4. ``storage.store_upload`` chunks the stream to Telegram and flips the file
       (and its chunks) to ``committed``;
    5. ``quota.add_usage`` records the now-committed bytes.

    ``files/`` owns the quota calls; ``storage/`` owns the chunk/file commit — the
    boundary SPEC §7.1 step 5 splits between them (see ``storage/README.md``). The
    quota increment runs only after ``store_upload`` returns committed, so a
    mid-upload failure (which leaves the file ``pending`` for the ``jobs/`` sweeper,
    SPEC §7.1 step 6) never inflates usage.

    :raises TeleCloudError: ``validation_error`` (bad name), ``not_found``
        (folder), ``file_too_large`` / ``quota_exceeded`` (quota), or whatever
        ``storage`` raises mid-upload.
    """
    clean_name = validate_file_name(name)
    if size_bytes < 0:
        raise _invalid("Declared size must not be negative.")

    db = await get_db(access_token)
    try:
        if folder_id is not None:
            await _assert_owned_folder(db, folder_id, user)

        # Reject early, before reading the stream (SPEC §3, §7.1 step 2).
        await quota.check_can_upload(user, size_bytes, access_token=access_token)

        pending = await files_repo.insert_pending(
            db,
            owner_id=user.id,
            name=clean_name,
            size_bytes=size_bytes,
            chunk_count=compute_chunk_count(size_bytes),
            folder_id=folder_id,
            mime_type=mime_type or DEFAULT_MIME_TYPE,
        )
        committed = await storage.store_upload(db, pending, stream)
    finally:
        await db.aclose()

    # Usage is added only once the file is committed (SPEC §7.1 step 5). quota
    # opens its own request-scoped client, so this runs after the upload db closes.
    await quota.add_usage(user, committed.size_bytes, access_token=access_token)
    return committed


# -- download ---------------------------------------------------------------
async def open_file_download(
    user: UserContext,
    file_id: UUID,
    *,
    access_token: str,
    range_: ByteRange | str | None = None,
) -> tuple[DownloadResponse, str]:
    """Open a streaming download for the caller's file, optionally ranged (SPEC §7.2).

    Returns ``(download, filename)``: the storage :class:`DownloadResponse` (an
    async byte iterator plus the framing metadata — 200-vs-206, ``Content-Length`` /
    ``Content-Range`` / ``Accept-Ranges``) and the file's name, which the router
    needs for ``Content-Disposition`` (a ``files/`` concern, not storage's, SPEC
    §6.9). ``range_`` is an optional ``bytes=...`` header string or a
    :class:`ByteRange`.

    Ownership is re-checked here (defense-in-depth over RLS) so a download never
    leaks another user's file; ``storage.open_download`` independently enforces the
    ``committed`` lifecycle (``upload_incomplete`` while ``pending``, ``not_found``
    if missing/deleted). The request-scoped ``db`` is closed before returning — the
    returned stream reads only from Telegram, never the DB.

    :raises TeleCloudError: ``not_found`` (missing/foreign/deleted file),
        ``upload_incomplete`` (still uploading), or ``validation_error`` (HTTP
        416/422) for a bad range.
    """
    db = await get_db(access_token)
    try:
        file = await files_repo.get(db, file_id)
        if file is None or file.owner_id != user.id or file.deleted_at is not None:
            raise _not_found()
        download = await storage.open_download(db, file_id, range_)
    finally:
        await db.aclose()
    return download, file.name


# -- list -------------------------------------------------------------------
async def list_files(
    user: UserContext, *, access_token: str, folder_id: UUID | None = None
) -> list[FileMeta]:
    """List the owner's committed files in a folder (root when ``folder_id`` is ``None``).

    Only ``committed``, non-deleted files are returned (SPEC §7.1). When a folder is
    given, validates it belongs to the caller and is live (``not_found`` otherwise).
    """
    db = await get_db(access_token)
    try:
        if folder_id is not None:
            await _assert_owned_folder(db, folder_id, user)
        return await files_repo.list_in_folder(
            db, owner_id=user.id, folder_id=folder_id
        )
    finally:
        await db.aclose()


# -- rename -----------------------------------------------------------------
async def rename_file(
    user: UserContext, file_id: UUID, *, access_token: str, name: str
) -> FileMeta:
    """Rename a committed file the caller owns; return the updated row (SPEC §6.12)."""
    clean_name = validate_file_name(name)
    db = await get_db(access_token)
    try:
        await _load_owned_file(db, file_id, user, statuses=(FileStatus.COMMITTED,))
        updated = await files_repo.rename(db, file_id, clean_name)
        if updated is None:  # pragma: no cover - row vanished between read & write
            raise _not_found()
        return updated
    finally:
        await db.aclose()


# -- move -------------------------------------------------------------------
async def move_file(
    user: UserContext,
    file_id: UUID,
    *,
    access_token: str,
    new_folder_id: UUID | None,
) -> FileMeta:
    """Move a committed file to another of the caller's folders (``None`` = root).

    Validates that the file and (when given) the destination folder belong to the
    caller and are live (``not_found`` otherwise); returns the updated row.
    """
    db = await get_db(access_token)
    try:
        await _load_owned_file(db, file_id, user, statuses=(FileStatus.COMMITTED,))
        if new_folder_id is not None:
            await _assert_owned_folder(db, new_folder_id, user)
        updated = await files_repo.move(db, file_id, new_folder_id=new_folder_id)
        if updated is None:  # pragma: no cover - row vanished between read & write
            raise _not_found()
        return updated
    finally:
        await db.aclose()


# -- soft-delete (the FileDeleter entrypoint, reused by folders/) -----------
async def soft_delete_file(
    user: UserContext,
    file_id: UUID,
    *,
    access_token: str,
    enqueue_deletion: DeletionEnqueuer | None = None,
) -> None:
    """Soft-delete a committed file: mark ``deleting`` → decrement quota → enqueue (SPEC §6.12).

    The public deletion entrypoint. ``folders/`` calls it for each file in a
    cascading folder delete via the ``folders.set_file_deleter`` seam — its
    signature matches ``folders.ports.FileDeleter``
    (``(user, file_id, *, access_token)``), so it registers directly with no
    adapter. ``enqueue_deletion`` is an extra defaulted keyword (still matches that
    protocol's calling convention); ``None`` resolves the registered ``jobs/``
    enqueuer.

    Steps, in a deliberate order:

    1. load + ownership check (committed, live);
    2. resolve the deletion enqueuer **first**, so an unwired ``jobs/`` fails fast
       (``internal_error``) *before* any state changes — no half-deleted file;
    3. mark the row ``deleting`` + stamp ``deleted_at`` (hides it from listings);
    4. ``quota.subtract_usage`` (floored at zero) — the decrement ``files/`` owns,
       not ``storage`` or ``folders`` (SPEC §6.12);
    5. enqueue the deferred Telegram-deletion job. Telegram messages are NOT deleted
       inline — ``jobs/`` does that later (SPEC §6.12, §7.4).

    :raises TeleCloudError: ``not_found`` (missing/foreign/non-committed/deleted),
        or ``internal_error`` if no deletion enqueuer is registered.
    """
    enqueue = enqueue_deletion or get_deletion_enqueuer()

    db = await get_db(access_token)
    try:
        file = await _load_owned_file(
            db, file_id, user, statuses=(FileStatus.COMMITTED,)
        )
        await files_repo.mark_deleting(db, file_id)
    finally:
        await db.aclose()

    # Quota is decremented here (files/ owns it, SPEC §6.12); subtract_usage opens
    # its own client and floors usage at zero (SPEC §6.10).
    await quota.subtract_usage(user, file.size_bytes, access_token=access_token)

    # Defer the actual Telegram-message + row removal to jobs/ (SPEC §7.4). The
    # §7.4 find_deleting sweep is the independent backstop if this enqueue is lost.
    await enqueue(file_id)
