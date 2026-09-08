"""Module-private request/response models for the ``files/`` routes (SPEC §5.3).

The wire contract of the file endpoints, local to ``files/`` rather than shared
vocabulary (SPEC §5.3). The cross-module file read model is ``shared.FileMeta``;
:class:`FileResponse` is its response-facing view. Request bodies for rename/move
are declared here; upload parameters (name, declared size, mime, optional folder)
arrive as query params + headers and are parsed in the router, since the request
body *is* the file stream (SPEC §7.1) and can't double as a JSON body.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from telecloud.shared import FileMeta, FileStatus

#: Upper bound on a file name. Generous for a virtual name (no filesystem path
#: limit applies); the service does the meaningful validation (`validate_file_name`).
MAX_NAME_LENGTH = 255


class RenameFileRequest(BaseModel):
    """Body of ``PATCH /files/{file_id}`` — the new name."""

    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)


class MoveFileRequest(BaseModel):
    """Body of ``POST /files/{file_id}/move`` — the new folder (``null`` = root)."""

    folder_id: UUID | None = None


class FileResponse(BaseModel):
    """Response-facing view of a file row (SPEC §4.3)."""

    id: UUID
    folder_id: UUID | None
    name: str
    size_bytes: int
    mime_type: str
    chunk_count: int
    status: FileStatus
    created_at: datetime

    @classmethod
    def from_meta(cls, file: FileMeta) -> "FileResponse":
        return cls(
            id=file.id,
            folder_id=file.folder_id,
            name=file.name,
            size_bytes=file.size_bytes,
            mime_type=file.mime_type,
            chunk_count=file.chunk_count,
            status=file.status,
            created_at=file.created_at,
        )


class FileListResponse(BaseModel):
    """A flat listing of the owner's committed files in one folder.

    ``folder_id`` is the listed folder (``null`` for the owner's root). Only
    ``committed`` files are returned (SPEC §7.1).
    """

    folder_id: UUID | None
    files: list[FileResponse]
