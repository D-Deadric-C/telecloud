"""Pydantic models shared across modules (SPEC.md §4, §5.3).

These are **read models** — immutable views that mirror the meaningful fields of
the frozen Postgres tables in SPEC §4. They are the common currency between
layers: ``database/`` repositories return them, and every other module consumes
them. Module-private request/response shapes do **not** live here (SPEC §5.3).

Conventions:

* All app row ids are ``uuid`` → :class:`uuid.UUID` here (SPEC §5.5).
* All timestamps are ``timestamptz`` (UTC) → :class:`datetime.datetime`.
* Models are ``frozen`` (a read model should not be mutated in place) and accept
  attribute access objects via ``from_attributes`` so a repo can do
  ``FileMeta.model_validate(row)`` directly from a DB record.

This module is pure: no I/O, only data definitions.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class FileStatus(str, Enum):
    """Lifecycle status of a file row (SPEC §4.3 ``file_status`` enum).

    * ``pending`` — created, chunks being uploaded; not yet usable.
    * ``committed`` — all chunks confirmed; the file is visible/usable (§7.1).
    * ``deleting`` — soft-deleted; awaiting the ``jobs/`` deferred delete (§7.4).
    """

    PENDING = "pending"
    COMMITTED = "committed"
    DELETING = "deleting"


class ChunkStatus(str, Enum):
    """Lifecycle status of a chunk row (SPEC §4.4 ``chunk_status`` enum)."""

    PENDING = "pending"
    COMMITTED = "committed"


class _ReadModel(BaseModel):
    """Base for the shared read models: immutable, buildable from DB rows."""

    model_config = ConfigDict(frozen=True, from_attributes=True)


class UserContext(_ReadModel):
    """The authenticated identity resolved from a request's JWT (SPEC §6.4).

    Produced by ``auth.current_user`` and threaded through the request. Mirrors
    the identity-bearing columns of ``profiles`` (SPEC §4.1); quota/usage figures
    are intentionally excluded — those are read fresh by ``quota/`` when needed.
    """

    id: UUID
    email: str
    email_verified: bool


class FileMeta(_ReadModel):
    """A file row (SPEC §4.3 ``public.files``)."""

    id: UUID
    owner_id: UUID
    folder_id: UUID | None = None
    name: str
    size_bytes: int
    mime_type: str = "application/octet-stream"
    chunk_count: int
    status: FileStatus
    created_at: datetime
    deleted_at: datetime | None = None


class ChunkMeta(_ReadModel):
    """A chunk row (SPEC §4.4 ``public.chunks``) — channel-aware (SPEC §1).

    Carries the Telegram coordinates (``channel_id``, ``message_id``,
    ``telegram_file_id``, ``bot_id``) that ``storage`` / ``telegram`` need to
    fetch or delete the underlying bytes. ``chunk_index`` is 0-based and ordered.
    """

    id: UUID
    file_id: UUID
    chunk_index: int
    size_bytes: int
    channel_id: int
    message_id: int
    telegram_file_id: str
    bot_id: str
    status: ChunkStatus
    created_at: datetime


class FolderMeta(_ReadModel):
    """A folder row (SPEC §4.2 ``public.folders``) — adjacency-list node.

    ``parent_id is None`` denotes a root-level folder owned by ``owner_id``.
    """

    id: UUID
    owner_id: UUID
    parent_id: UUID | None = None
    name: str
    created_at: datetime
    deleted_at: datetime | None = None


class ShareMeta(_ReadModel):
    """A share row (SPEC §4.5 ``public.shares``) — the *internal* read model.

    This holds ``owner_id`` and is for module-internal use (``sharing``/``files``
    management paths). The public, unauthenticated download route must NOT leak
    owner identity (SPEC §6.13); that endpoint returns its own narrow shape.
    """

    id: UUID
    file_id: UUID
    owner_id: UUID
    token: str
    expires_at: datetime | None = None
    download_limit: int | None = None
    download_count: int = 0
    revoked: bool = False
    created_at: datetime
