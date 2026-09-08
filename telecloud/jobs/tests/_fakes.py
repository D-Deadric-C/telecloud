"""Test doubles for the ``jobs/`` cleanup jobs — all in-memory, no I/O.

The fakes mirror exactly the surfaces ``jobs.service`` depends on:

* :class:`FakeStore` + :class:`FakeFilesRepo` / :class:`FakeChunksRepo` — the
  ``files_repo`` / ``chunks_repo`` functions the jobs call. Tests monkeypatch the
  real repos in ``jobs.service`` with these (the same pattern ``storage/`` uses to
  "mock ``database``").
* :class:`FakeDelete` — a stand-in for ``telegram.delete_message`` that records
  calls and can be scripted to raise transient / permanent :class:`TelegramError`s.

The retry queue is **not** faked: tests use the real
:class:`telecloud.rate_limit.queue.RetryQueue` over the in-memory ``FakeRedis``, so
the dead-letter transition under test is the production code path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from telecloud.shared import ChunkMeta, ChunkStatus, FileMeta, FileStatus
from telecloud.telegram import TelegramError


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FakeStore:
    """Shared in-memory ``files`` + ``chunks`` tables for the repo fakes."""

    files: dict[UUID, FileMeta] = field(default_factory=dict)
    chunks: list[ChunkMeta] = field(default_factory=list)

    def add_file(
        self,
        *,
        status: FileStatus,
        chunk_count: int = 1,
        created_at: datetime | None = None,
        owner_id: UUID | None = None,
    ) -> FileMeta:
        """Add a file with ``chunk_count`` chunks carrying distinct Telegram coords."""
        file = FileMeta(
            id=uuid4(),
            owner_id=owner_id or uuid4(),
            folder_id=None,
            name="sample.bin",
            size_bytes=chunk_count * 100,
            mime_type="application/octet-stream",
            chunk_count=chunk_count,
            status=status,
            created_at=created_at or _now(),
            deleted_at=_now() if status is FileStatus.DELETING else None,
        )
        self.files[file.id] = file
        for index in range(chunk_count):
            self.chunks.append(
                ChunkMeta(
                    id=uuid4(),
                    file_id=file.id,
                    chunk_index=index,
                    size_bytes=100,
                    channel_id=-1001,
                    message_id=5000 + len(self.chunks),
                    telegram_file_id=f"FILEID{len(self.chunks)}",
                    bot_id="bot-1",
                    status=(
                        ChunkStatus.PENDING
                        if status is FileStatus.PENDING
                        else ChunkStatus.COMMITTED
                    ),
                    created_at=_now(),
                )
            )
        return file


class FakeFilesRepo:
    """The ``files_repo`` functions ``jobs.service`` calls, over a :class:`FakeStore`."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def find_pending_older_than(self, db, cutoff: datetime) -> list[FileMeta]:
        rows = [
            f
            for f in self.store.files.values()
            if f.status is FileStatus.PENDING and f.created_at < cutoff
        ]
        return sorted(rows, key=lambda f: f.created_at)

    async def find_deleting(self, db) -> list[FileMeta]:
        rows = [
            f for f in self.store.files.values() if f.status is FileStatus.DELETING
        ]
        return sorted(rows, key=lambda f: f.deleted_at or f.created_at)

    async def delete_row(self, db, file_id: UUID) -> None:
        # Mirror the real ``on delete cascade``: removing the file removes its chunks.
        self.store.files.pop(file_id, None)
        self.store.chunks = [c for c in self.store.chunks if c.file_id != file_id]


class FakeChunksRepo:
    """The ``chunks_repo`` functions ``jobs.service`` calls, over a :class:`FakeStore`."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def list_for_file(self, db, file_id: UUID) -> list[ChunkMeta]:
        rows = [c for c in self.store.chunks if c.file_id == file_id]
        return sorted(rows, key=lambda c: c.chunk_index)


class FakeDelete:
    """Scriptable stand-in for ``telegram.delete_message``.

    Records every ``(channel_id, message_id, bot_id)`` it was called with. ``fail``
    decides the outcome per call: return a :class:`TelegramError` (transient or not)
    to raise it, or ``None`` to succeed. ``fail`` receives the 1-based call count so
    a test can, say, fail the first N attempts then succeed.
    """

    def __init__(self, fail=None) -> None:
        self.calls: list[tuple[int, int, str | None]] = []
        self._fail = fail or (lambda n: None)

    async def __call__(
        self, channel_id: int, message_id: int, *, bot_id: str | None = None
    ) -> None:
        self.calls.append((channel_id, message_id, bot_id))
        outcome = self._fail(len(self.calls))
        if outcome is not None:
            raise outcome


def transient_error(message: str = "rate limited") -> TelegramError:
    """A transient Telegram failure (the kind that should be retried)."""
    return TelegramError(message, transient=True)


def permanent_error(message: str = "message not found") -> TelegramError:
    """A permanent Telegram failure (the kind that should not be retried)."""
    return TelegramError(message, transient=False)
