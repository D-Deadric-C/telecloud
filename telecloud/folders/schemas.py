"""Module-private request/response models for the ``folders/`` routes (SPEC §5.3).

The wire contract of the folder endpoints, local to ``folders/`` rather than
shared vocabulary (SPEC §5.3). The cross-module folder read model is
``shared.FolderMeta``; :class:`FolderResponse` is its narrow, response-facing view
(it drops ``deleted_at`` — listings never return soft-deleted rows). File entries
in a listing are summarized by :class:`FileEntry`, a read-only projection of
``shared.FileMeta`` (folder listings show files but ``folders/`` does not own the
file contract).

Name length bounds are declared here for early 422s, but the authoritative,
content-aware validation (path separators, control chars, ``.``/``..``) lives in
:func:`telecloud.folders.service.validate_name` so direct service callers are
guarded too.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from telecloud.shared import FileMeta, FolderMeta

#: Upper bound on a folder name. Generous for a virtual name (no filesystem path
#: limit applies); the service does the meaningful validation.
MAX_NAME_LENGTH = 255


class CreateFolderRequest(BaseModel):
    """Body of ``POST /folders`` — create a folder, optionally under a parent."""

    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    parent_id: UUID | None = None


class RenameFolderRequest(BaseModel):
    """Body of ``PATCH /folders/{folder_id}`` — the new name."""

    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)


class MoveFolderRequest(BaseModel):
    """Body of ``POST /folders/{folder_id}/move`` — the new parent (``null`` = root)."""

    new_parent_id: UUID | None = None


class FolderResponse(BaseModel):
    """Response-facing view of a folder row (no ``deleted_at``)."""

    id: UUID
    parent_id: UUID | None
    name: str
    created_at: datetime

    @classmethod
    def from_meta(cls, folder: FolderMeta) -> "FolderResponse":
        return cls(
            id=folder.id,
            parent_id=folder.parent_id,
            name=folder.name,
            created_at=folder.created_at,
        )


class FileEntry(BaseModel):
    """A file as it appears in a folder listing (a narrow ``FileMeta`` view)."""

    id: UUID
    folder_id: UUID | None
    name: str
    size_bytes: int
    mime_type: str
    created_at: datetime

    @classmethod
    def from_meta(cls, file: FileMeta) -> "FileEntry":
        return cls(
            id=file.id,
            folder_id=file.folder_id,
            name=file.name,
            size_bytes=file.size_bytes,
            mime_type=file.mime_type,
            created_at=file.created_at,
        )


class FolderContentsResponse(BaseModel):
    """Contents of one folder: its child folders and its files.

    ``folder_id`` is the listed folder (``null`` for the owner's root). Only
    non-deleted subfolders and ``committed`` files are returned.
    """

    folder_id: UUID | None
    folders: list[FolderResponse]
    files: list[FileEntry]
