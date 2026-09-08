"""FastAPI router for the folder endpoints (SPEC.md §6.11).

Thin routes that delegate to :mod:`telecloud.folders.service`. All are authed and
owner-scoped: each takes ``current_user`` + the bearer token (the token scopes RLS
via ``database.get_db``). Errors raised below surface as :class:`TeleCloudError`
and are rendered to the canonical JSON envelope by ``middleware/`` (SPEC §5.1).

FLAGGED (see ``folders/README.md``): the *router* depends on ``auth`` even though
SPEC §6.11 lists the dependency set as config/shared/database/auth — that is
expected; ``auth`` is module 5 and never imports ``folders/``. The cascading
delete route resolves the file-deletion entrypoint through the
:mod:`telecloud.folders.ports` seam, which ``files/`` (module 13) must register;
the dependency on ``files/`` is inverted, not imported.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from telecloud.auth import access_token, current_user
from telecloud.shared import UserContext

from telecloud.folders import service
from telecloud.folders.ports import FileDeleter, get_file_deleter
from telecloud.folders.schemas import (
    CreateFolderRequest,
    FileEntry,
    FolderContentsResponse,
    FolderResponse,
    MoveFolderRequest,
    RenameFolderRequest,
)

router = APIRouter(prefix="/folders", tags=["folders"])


def _contents_response(
    folder_id: UUID | None, contents: tuple[list, list]
) -> FolderContentsResponse:
    subfolders, files = contents
    return FolderContentsResponse(
        folder_id=folder_id,
        folders=[FolderResponse.from_meta(f) for f in subfolders],
        files=[FileEntry.from_meta(f) for f in files],
    )


@router.post("", response_model=FolderResponse, status_code=status.HTTP_201_CREATED)
async def create_folder(
    body: CreateFolderRequest,
    user: UserContext = Depends(current_user),
    token: str = Depends(access_token),
) -> FolderResponse:
    """Create a folder (root, or under ``parent_id`` if given)."""
    folder = await service.create_folder(
        user, access_token=token, name=body.name, parent_id=body.parent_id
    )
    return FolderResponse.from_meta(folder)


@router.get("", response_model=FolderContentsResponse)
async def list_root(
    user: UserContext = Depends(current_user),
    token: str = Depends(access_token),
) -> FolderContentsResponse:
    """List the caller's root contents (``parent_id IS NULL``)."""
    contents = await service.list_contents(user, access_token=token, folder_id=None)
    return _contents_response(None, contents)


@router.get("/{folder_id}", response_model=FolderContentsResponse)
async def list_folder(
    folder_id: UUID,
    user: UserContext = Depends(current_user),
    token: str = Depends(access_token),
) -> FolderContentsResponse:
    """List one folder's subfolders and files."""
    contents = await service.list_contents(
        user, access_token=token, folder_id=folder_id
    )
    return _contents_response(folder_id, contents)


@router.patch("/{folder_id}", response_model=FolderResponse)
async def rename_folder(
    folder_id: UUID,
    body: RenameFolderRequest,
    user: UserContext = Depends(current_user),
    token: str = Depends(access_token),
) -> FolderResponse:
    """Rename a folder."""
    folder = await service.rename_folder(
        user, folder_id, access_token=token, name=body.name
    )
    return FolderResponse.from_meta(folder)


@router.post("/{folder_id}/move", response_model=FolderResponse)
async def move_folder(
    folder_id: UUID,
    body: MoveFolderRequest,
    user: UserContext = Depends(current_user),
    token: str = Depends(access_token),
) -> FolderResponse:
    """Re-parent a folder (``new_parent_id: null`` moves it to the root).

    Rejects cycles (a folder cannot become its own descendant) with
    ``validation_error``.
    """
    folder = await service.move_folder(
        user, folder_id, access_token=token, new_parent_id=body.new_parent_id
    )
    return FolderResponse.from_meta(folder)


@router.delete("/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder(
    folder_id: UUID,
    user: UserContext = Depends(current_user),
    token: str = Depends(access_token),
    delete_file: FileDeleter = Depends(get_file_deleter),
) -> Response:
    """Soft-delete a folder, cascading to its descendants and their files.

    Folder rows are marked ``deleted_at``; contained files are handed to
    ``files/``'s deletion path (SPEC §6.12). Telegram messages are NOT deleted
    here — that is deferred to a ``jobs/`` deletion job (SPEC §7.4).
    """
    await service.soft_delete_folder(
        user, folder_id, access_token=token, delete_file=delete_file
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
