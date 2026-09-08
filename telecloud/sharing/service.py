"""Share-link lifecycle + the public token-download read path (SPEC §6.13, §7.3).

Two surfaces with very different trust models live here:

* **Authed management** (``create_share`` / ``revoke_share`` / ``list_shares``) is
  owner-scoped: it goes through the RLS-honoring user client
  (``database.get_db`` with the caller's JWT, SPEC §6.3) and re-checks ``owner_id``
  against the caller as defense-in-depth (the in-memory test fake has no RLS).
* **Public download** (``open_share_download``) takes NO user. It resolves the
  share by token via the **service-role** client (``database.get_service_db``) —
  the single sanctioned RLS bypass (SPEC §4, §6.13, §7.3) — enforces
  ``revoked`` / ``expires_at`` / ``download_limit`` in application code, bumps the
  download counter atomically, then reuses ``storage.open_download`` to stream the
  bytes. It leaks nothing about the owner (SPEC §6.13).

Boundaries (SPEC §6.13): never bypasses the share checks, never uses the
service-role client for anything beyond resolving + streaming the shared file, and
never reimplements chunk streaming (that is ``storage``'s, reused wholesale).

Design decisions (SPEC §6.13 left these open — see ``sharing/README.md``):

* **Default expiry:** none. A share never expires unless the creator sets
  ``expires_at`` (which must be a future, timezone-aware instant).
* **Download limit:** none (unlimited) by default; an explicit ``download_limit``
  must be a positive integer and is enforced as ``download_count >= limit``.
* **Revocation is soft:** ``revoked=true`` keeps the row so a later download attempt
  is answered with ``share_revoked`` (not an indistinguishable ``not_found``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from telecloud import storage
from telecloud.database import (
    Database,
    files_repo,
    get_db,
    get_service_db,
    shares_repo,
)
from telecloud.shared import (
    ErrorCode,
    FileStatus,
    ShareMeta,
    TeleCloudError,
    UserContext,
    generate_token,
)
from telecloud.storage import ByteRange, DownloadResponse


def _not_found() -> TeleCloudError:
    # Always not_found (never forbidden) for shares/files the caller can't act on,
    # so the API never confirms the existence of another user's row (SPEC §6.13).
    return TeleCloudError.from_code(ErrorCode.NOT_FOUND, "Share not found.")


def _invalid(message: str) -> TeleCloudError:
    return TeleCloudError.from_code(ErrorCode.VALIDATION_ERROR, message)


def _validate_create_args(
    expires_at: datetime | None, download_limit: int | None
) -> None:
    """Validate the optional create-share parameters (SPEC §6.13 design decisions).

    ``expires_at`` (when given) must be timezone-aware and strictly in the future —
    a past or naive instant is a ``validation_error``. ``download_limit`` (when
    given) must be a positive integer.
    """
    if expires_at is not None:
        if expires_at.tzinfo is None:
            raise _invalid("expires_at must be timezone-aware (include a UTC offset).")
        if expires_at <= datetime.now(timezone.utc):
            raise _invalid("expires_at must be in the future.")
    if download_limit is not None and download_limit < 1:
        raise _invalid("download_limit must be a positive integer.")


async def _load_owned_committed_file(
    db: Database, file_id: UUID, user: UserContext
) -> None:
    """Assert ``file_id`` is the caller's live, committed file, or raise ``not_found``.

    Mirrors ``files/``'s ownership posture: a missing row, another owner's row, a
    soft-deleted row, and a non-committed row are all treated as ``not_found`` so
    the API leaks nothing about files the caller cannot share.
    """
    file = await files_repo.get(db, file_id)
    if (
        file is None
        or file.owner_id != user.id
        or file.deleted_at is not None
        or file.status != FileStatus.COMMITTED
    ):
        raise _not_found()


# -- create -----------------------------------------------------------------
async def create_share(
    user: UserContext,
    *,
    access_token: str,
    file_id: UUID,
    expires_at: datetime | None = None,
    download_limit: int | None = None,
) -> ShareMeta:
    """Create a public share link for one of the caller's committed files (SPEC §6.13).

    Validates the optional ``expires_at`` / ``download_limit`` (see
    :func:`_validate_create_args`), confirms the file is the caller's live,
    committed file, generates an unguessable URL-safe ``token`` via
    ``shared.generate_token``, and inserts the ``shares`` row. Returns the internal
    :class:`ShareMeta` (the router narrows it to a non-owner view).

    :raises TeleCloudError: ``validation_error`` (bad expiry/limit) or ``not_found``
        (missing/foreign/non-committed/deleted file).
    """
    _validate_create_args(expires_at, download_limit)

    db = await get_db(access_token)
    try:
        await _load_owned_committed_file(db, file_id, user)
        return await shares_repo.insert(
            db,
            file_id=file_id,
            owner_id=user.id,
            token=generate_token(),
            expires_at=expires_at,
            download_limit=download_limit,
        )
    finally:
        await db.aclose()


# -- revoke -----------------------------------------------------------------
async def revoke_share(
    user: UserContext, share_id: UUID, *, access_token: str
) -> ShareMeta:
    """Soft-revoke one of the caller's shares; return the updated row (SPEC §6.13).

    Revocation is soft (``revoked=true``): the row is kept so later download
    attempts are answered ``share_revoked``. Ownership is re-checked against the
    caller before mutating (defense-in-depth over RLS). Revoking an already-revoked
    share is idempotent.

    :raises TeleCloudError: ``not_found`` (missing or foreign share).
    """
    db = await get_db(access_token)
    try:
        share = await shares_repo.get(db, share_id)
        if share is None or share.owner_id != user.id:
            raise _not_found()
        revoked = await shares_repo.revoke(db, share_id)
        if revoked is None:  # pragma: no cover - row vanished between read & write
            raise _not_found()
        return revoked
    finally:
        await db.aclose()


# -- list -------------------------------------------------------------------
async def list_shares(
    user: UserContext, *, access_token: str, file_id: UUID
) -> list[ShareMeta]:
    """List the share links the caller created for one of their files (SPEC §6.13).

    ``file_id`` is required and must be the caller's live, committed file (validated
    first, ``not_found`` otherwise); the result is every share for that file,
    including revoked ones, so a management UI can show the full history.

    :raises TeleCloudError: ``not_found`` (missing/foreign/non-committed/deleted file).
    """
    db = await get_db(access_token)
    try:
        await _load_owned_committed_file(db, file_id, user)
        return await shares_repo.list_for_file(db, file_id)
    finally:
        await db.aclose()


# -- public download (no auth, service-role read) ---------------------------
def _enforce_share(share: ShareMeta) -> None:
    """Apply the §7.3 gates to a resolved share, raising on any failure.

    Order: ``revoked`` (``share_revoked``) → expired (``share_expired``) → over the
    download limit (``forbidden``). The first failing gate wins.
    """
    if share.revoked:
        raise TeleCloudError.from_code(
            ErrorCode.SHARE_REVOKED, "This share link has been revoked."
        )
    if share.expires_at is not None and share.expires_at < datetime.now(timezone.utc):
        raise TeleCloudError.from_code(
            ErrorCode.SHARE_EXPIRED, "This share link has expired."
        )
    if (
        share.download_limit is not None
        and share.download_count >= share.download_limit
    ):
        raise TeleCloudError.from_code(
            ErrorCode.FORBIDDEN, "This share link has reached its download limit."
        )


async def open_share_download(
    token: str, *, range_: ByteRange | str | None = None
) -> tuple[DownloadResponse, str]:
    """Open a public, unauthenticated streaming download for a share token (SPEC §7.3).

    The token is resolved with the **service-role** client (the sole sanctioned RLS
    bypass, SPEC §4) — there is no user JWT on this path. The share is then gated in
    application code (``revoked`` → ``share_revoked``, expired → ``share_expired``,
    over-limit → ``forbidden``). On success the ``download_count`` is incremented
    atomically *before* streaming (SPEC §7.3), and bytes are streamed by reusing
    ``storage.open_download`` (range-aware, SPEC §7.2).

    Returns ``(download, filename)``: the storage :class:`DownloadResponse` plus the
    file's name (needed by the route for ``Content-Disposition``). The file name is
    not owner identity, so returning it leaks nothing about the owner (SPEC §6.13);
    no email or user id is ever read or returned here.

    The service-role client is process-cached (``database.get_service_db``) and must
    NOT be closed per request, so it is used directly with no ``aclose``; the
    returned stream reads only from Telegram, never the DB.

    :raises TeleCloudError: ``not_found`` (unknown token / missing file),
        ``share_revoked``, ``share_expired``, ``forbidden`` (over limit),
        ``upload_incomplete`` (file still uploading), or ``validation_error``
        (HTTP 416/422) for a bad range.
    """
    db = await get_service_db()

    share = await shares_repo.resolve_by_token(db, token)
    if share is None:
        raise _not_found()
    _enforce_share(share)

    # Resolve the file name for Content-Disposition. A missing/deleted file behind a
    # live share surfaces as not_found from open_download below; we read only the
    # name here (never owner_id/email — SPEC §6.13).
    file = await files_repo.get(db, share.file_id)
    if file is None or file.deleted_at is not None:
        raise TeleCloudError.from_code(ErrorCode.NOT_FOUND, "Shared file not found.")

    # Count the download before streaming (SPEC §7.3). The bump is a single atomic
    # UPDATE; concurrent requests may momentarily over-count past the limit, which
    # is acceptable at this scale (see sharing/README.md).
    await shares_repo.increment_download_count(db, share.id)

    download = await storage.open_download(db, share.file_id, range_)
    return download, file.name
