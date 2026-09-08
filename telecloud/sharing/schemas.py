"""Module-private request/response models for the ``sharing/`` routes (SPEC §5.3).

The wire contract of the share endpoints, local to ``sharing/`` rather than shared
vocabulary (SPEC §5.3). The cross-module share read model is ``shared.ShareMeta``
(which carries ``owner_id``); :class:`ShareResponse` is its narrow, response-facing
view that **drops ``owner_id``** so no owner identity travels on the wire (SPEC
§6.13) — even on the authed management routes, where it would only ever be the
caller's own id, the field is simply not part of the contract.

The unauthenticated public download route returns raw bytes (a stream), not one of
these models, so there is no response schema for it here.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from telecloud.shared import ShareMeta


class CreateShareRequest(BaseModel):
    """Body of ``POST /shares`` — create a public link for one of the caller's files.

    ``expires_at`` and ``download_limit`` are optional; omitting them yields the
    module defaults (never-expire, unlimited — see ``sharing/README.md``). When
    given they are validated in the service: ``expires_at`` must be timezone-aware
    and in the future, ``download_limit`` must be a positive integer.
    """

    file_id: UUID
    expires_at: datetime | None = None
    download_limit: int | None = Field(default=None, ge=1)


class ShareResponse(BaseModel):
    """Response-facing view of a share row — owner identity intentionally omitted.

    Mirrors the meaningful columns of ``public.shares`` (SPEC §4.5) minus
    ``owner_id``. The ``token`` is returned so the caller can construct the public
    ``/s/{token}`` URL (the host is the caller's concern; ``sharing/`` does not know
    the deployment origin).
    """

    id: UUID
    file_id: UUID
    token: str
    expires_at: datetime | None
    download_limit: int | None
    download_count: int
    revoked: bool
    created_at: datetime

    @classmethod
    def from_meta(cls, share: ShareMeta) -> "ShareResponse":
        return cls(
            id=share.id,
            file_id=share.file_id,
            token=share.token,
            expires_at=share.expires_at,
            download_limit=share.download_limit,
            download_count=share.download_count,
            revoked=share.revoked,
            created_at=share.created_at,
        )


class ShareListResponse(BaseModel):
    """The share links created for one of the caller's files (management view)."""

    file_id: UUID
    shares: list[ShareResponse]
