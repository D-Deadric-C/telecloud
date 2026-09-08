"""FastAPI router for the file endpoints (SPEC §6.12).

Thin routes that delegate to :mod:`telecloud.files.service` and build the actual
HTTP responses — ``files/`` is the only file-domain module that produces FastAPI
responses (SPEC §6.12). All routes are authed and owner-scoped: each takes
``current_user`` + the bearer token (which scopes RLS via ``database.get_db``).
Errors raised below surface as :class:`TeleCloudError` and are rendered to the
canonical JSON envelope by ``middleware/`` (SPEC §5.1).

The upload route reads the request body as a stream (SPEC §7.1, no disk buffering),
so upload parameters travel as query params + headers rather than a JSON body. The
download route is range-aware: it reflects ``storage``'s 200-vs-206 decision and
its ``Content-Length`` / ``Content-Range`` / ``Accept-Ranges`` headers onto the wire
and adds ``Content-Disposition`` (a ``files/`` concern, SPEC §6.9, §7.2).
"""

from __future__ import annotations

from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from telecloud.auth import access_token, current_user
from telecloud.shared import ErrorCode, TeleCloudError, UserContext

from telecloud.files import service
from telecloud.files.service import DEFAULT_MIME_TYPE
from telecloud.files.schemas import (
    FileListResponse,
    FileResponse,
    MoveFileRequest,
    RenameFileRequest,
)

router = APIRouter(prefix="/files", tags=["files"])


def _declared_size(request: Request) -> int:
    """Read the declared upload size from ``Content-Length`` (SPEC §7.1 step 1).

    The size must be known up front so quota can reject early (SPEC §3); a chunked
    upload with no ``Content-Length`` is rejected as a ``validation_error``.
    """
    raw = request.headers.get("content-length")
    if raw is None:
        raise TeleCloudError.from_code(
            ErrorCode.VALIDATION_ERROR,
            "A Content-Length header (declared size) is required to upload.",
        )
    try:
        size = int(raw)
    except ValueError:
        raise TeleCloudError.from_code(
            ErrorCode.VALIDATION_ERROR, "Malformed Content-Length header."
        ) from None
    if size < 0:
        raise TeleCloudError.from_code(
            ErrorCode.VALIDATION_ERROR, "Content-Length must not be negative."
        )
    return size


def _content_disposition(filename: str) -> str:
    """Build a ``Content-Disposition`` header for an attachment download.

    Provides both a sanitized ASCII ``filename`` (quotes/backslashes/control chars
    stripped) and an RFC 5987 ``filename*`` so non-ASCII names survive intact.
    """
    ascii_name = "".join(
        ch for ch in filename if 32 <= ord(ch) < 127 and ch not in '"\\'
    ).strip()
    fallback = ascii_name or "download"
    quoted = quote(filename, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quoted}"


@router.post("", response_model=FileResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(
    request: Request,
    name: str = Query(..., min_length=1),
    folder_id: UUID | None = Query(None),
    user: UserContext = Depends(current_user),
    token: str = Depends(access_token),
) -> FileResponse:
    """Upload a file (two-phase commit, SPEC §7.1).

    The request body is the raw file stream; ``name`` and optional ``folder_id``
    are query params, the declared size is the ``Content-Length`` header, and the
    MIME type is the request ``Content-Type`` (defaulting to
    ``application/octet-stream``).
    """
    size_bytes = _declared_size(request)
    mime_type = request.headers.get("content-type") or DEFAULT_MIME_TYPE
    committed = await service.upload_file(
        user,
        access_token=token,
        name=name,
        size_bytes=size_bytes,
        stream=request.stream(),
        folder_id=folder_id,
        mime_type=mime_type,
    )
    return FileResponse.from_meta(committed)


@router.get("", response_model=FileListResponse)
async def list_files(
    folder_id: UUID | None = Query(None),
    user: UserContext = Depends(current_user),
    token: str = Depends(access_token),
) -> FileListResponse:
    """List the caller's committed files in a folder (root when ``folder_id`` absent)."""
    files = await service.list_files(user, access_token=token, folder_id=folder_id)
    return FileListResponse(
        folder_id=folder_id, files=[FileResponse.from_meta(f) for f in files]
    )


@router.get("/{file_id}")
async def download_file(
    file_id: UUID,
    request: Request,
    user: UserContext = Depends(current_user),
    token: str = Depends(access_token),
) -> StreamingResponse:
    """Stream a file, honoring an optional ``Range`` header (SPEC §7.2).

    Returns ``206 Partial Content`` with ``Content-Range`` for a range request,
    else ``200`` with the full ``Content-Length``; always advertises
    ``Accept-Ranges: bytes`` and sets ``Content-Disposition``.
    """
    range_header = request.headers.get("range")
    download, filename = await service.open_file_download(
        user, file_id, access_token=token, range_=range_header
    )
    # storage hands us Content-Type/Length/(Range)/Accept-Ranges; we own
    # Content-Disposition. Content-Type is set via media_type to avoid duplication.
    headers = {k: v for k, v in download.headers.items() if k != "Content-Type"}
    headers["Content-Disposition"] = _content_disposition(filename)
    return StreamingResponse(
        download.stream,
        status_code=download.status_code,
        headers=headers,
        media_type=download.mime_type,
    )


@router.patch("/{file_id}", response_model=FileResponse)
async def rename_file(
    file_id: UUID,
    body: RenameFileRequest,
    user: UserContext = Depends(current_user),
    token: str = Depends(access_token),
) -> FileResponse:
    """Rename a file."""
    updated = await service.rename_file(
        user, file_id, access_token=token, name=body.name
    )
    return FileResponse.from_meta(updated)


@router.post("/{file_id}/move", response_model=FileResponse)
async def move_file(
    file_id: UUID,
    body: MoveFileRequest,
    user: UserContext = Depends(current_user),
    token: str = Depends(access_token),
) -> FileResponse:
    """Move a file to another folder (``folder_id: null`` moves it to the root)."""
    updated = await service.move_file(
        user, file_id, access_token=token, new_folder_id=body.folder_id
    )
    return FileResponse.from_meta(updated)


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    file_id: UUID,
    user: UserContext = Depends(current_user),
    token: str = Depends(access_token),
) -> Response:
    """Soft-delete a file (mark ``deleting`` → decrement quota → enqueue job, SPEC §6.12).

    Telegram messages are NOT deleted here — that is deferred to a ``jobs/`` deletion
    job (SPEC §7.4).
    """
    await service.soft_delete_file(user, file_id, access_token=token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
