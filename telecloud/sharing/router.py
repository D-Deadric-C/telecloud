"""FastAPI router for the **authed** share-management endpoints (SPEC §6.13).

Thin, owner-scoped routes that delegate to :mod:`telecloud.sharing.service`. Each
takes ``current_user`` + the bearer token (the token scopes RLS via
``database.get_db``). Responses use :class:`ShareResponse`, which omits owner
identity (SPEC §6.13). Errors raised below surface as :class:`TeleCloudError` and
are rendered to the canonical JSON envelope by ``middleware/`` (SPEC §5.1).

The unauthenticated public download route lives separately in
:mod:`telecloud.sharing.public` (different prefix, no auth dependency).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from telecloud.auth import access_token, current_user
from telecloud.shared import UserContext

from telecloud.sharing import service
from telecloud.sharing.schemas import (
    CreateShareRequest,
    ShareListResponse,
    ShareResponse,
)

router = APIRouter(prefix="/shares", tags=["sharing"])


@router.post("", response_model=ShareResponse, status_code=status.HTTP_201_CREATED)
async def create_share(
    body: CreateShareRequest,
    user: UserContext = Depends(current_user),
    token: str = Depends(access_token),
) -> ShareResponse:
    """Create a public share link for one of the caller's committed files.

    Optional ``expires_at`` (future, timezone-aware) and ``download_limit``
    (positive int) narrow the link; omitting them yields the defaults
    (never-expire, unlimited).
    """
    share = await service.create_share(
        user,
        access_token=token,
        file_id=body.file_id,
        expires_at=body.expires_at,
        download_limit=body.download_limit,
    )
    return ShareResponse.from_meta(share)


@router.get("", response_model=ShareListResponse)
async def list_shares(
    file_id: UUID = Query(...),
    user: UserContext = Depends(current_user),
    token: str = Depends(access_token),
) -> ShareListResponse:
    """List every share link the caller created for one of their files (``?file_id=``).

    Includes revoked links so a management UI can show the full history.
    """
    shares = await service.list_shares(user, access_token=token, file_id=file_id)
    return ShareListResponse(
        file_id=file_id, shares=[ShareResponse.from_meta(s) for s in shares]
    )


@router.post("/{share_id}/revoke", response_model=ShareResponse)
async def revoke_share(
    share_id: UUID,
    user: UserContext = Depends(current_user),
    token: str = Depends(access_token),
) -> ShareResponse:
    """Revoke one of the caller's share links (soft: ``revoked=true``).

    Returns the updated row; revoking an already-revoked link is idempotent.
    Subsequent public downloads of the token are rejected with ``share_revoked``.
    """
    share = await service.revoke_share(user, share_id, access_token=token)
    return ShareResponse.from_meta(share)
