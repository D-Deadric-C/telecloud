"""Test doubles for the storage chunking engine: a fake Telegram transport and a
fake ``database`` repository pair, both backed by simple in-memory state.

No network, no Redis, no Postgres. The fakes mirror exactly the surfaces
``storage`` depends on:

* :class:`FakeTransport` — the two ``telegram/`` ops storage uses
  (:meth:`send_document`, :meth:`get_file_stream`, SPEC §6.8). It keeps the bytes
  it was sent keyed by the synthetic ``telegram_file_id`` so a later
  ``get_file_stream`` returns the same bytes — letting an upload feed a download.
  Reads are yielded in small sub-pieces so the Range path's skip-across-pieces and
  boundary trimming get exercised.
* :class:`FakeFilesRepo` / :class:`FakeChunksRepo` — the ``files_repo`` /
  ``chunks_repo`` functions storage calls, over a shared :class:`FakeStore`. Tests
  monkeypatch the real repos in ``storage.upload`` / ``storage.download`` with
  these, which is how "mock ``database``" is realized.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from telecloud.shared import ChunkMeta, ChunkStatus, FileMeta, FileStatus
from telecloud.telegram import SendResult


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Telegram transport fake                                                      #
# --------------------------------------------------------------------------- #


class FakeTransport:
    """In-memory stand-in for the ``telegram`` module (SPEC §6.8).

    ``read_piece_size`` controls how finely :meth:`get_file_stream` slices a
    chunk's bytes on the way back, so tests can force an offset to land mid-piece.
    """

    def __init__(self, *, channel_id: int = -1001, read_piece_size: int = 3) -> None:
        self.channel_id = channel_id
        self.read_piece_size = read_piece_size
        self.blobs: dict[str, bytes] = {}
        self.sent: list[bytes] = []
        self._counter = 0

    async def send_document(self, channel_id: int | None, data: bytes) -> SendResult:
        self._counter += 1
        n = self._counter
        file_id = f"FILEID{n}"
        self.blobs[file_id] = data
        self.sent.append(data)
        chosen = channel_id if channel_id is not None else self.channel_id
        return SendResult(
            message_id=1000 + n,
            telegram_file_id=file_id,
            bot_id="bot-1",
            channel_id=chosen,
        )

    async def get_file_stream(
        self, channel_id: int, file_id: str, *, bot_id: str | None = None
    ):
        data = self.blobs[file_id]
        step = self.read_piece_size
        for i in range(0, len(data), step):
            yield data[i : i + step]


# --------------------------------------------------------------------------- #
# database repo fakes                                                          #
# --------------------------------------------------------------------------- #


@dataclass
class FakeStore:
    """Shared in-memory tables for the repo fakes."""

    files: dict[UUID, FileMeta] = field(default_factory=dict)
    chunks: list[ChunkMeta] = field(default_factory=list)

    def add_pending_file(
        self, *, size_bytes: int, chunk_count: int, mime_type: str = "text/plain"
    ) -> FileMeta:
        file = FileMeta(
            id=uuid4(),
            owner_id=uuid4(),
            folder_id=None,
            name="sample.bin",
            size_bytes=size_bytes,
            mime_type=mime_type,
            chunk_count=chunk_count,
            status=FileStatus.PENDING,
            created_at=_now(),
            deleted_at=None,
        )
        self.files[file.id] = file
        return file


class FakeFilesRepo:
    """The ``files_repo`` functions ``storage`` calls, over a :class:`FakeStore`."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def get(self, db, file_id: UUID) -> FileMeta | None:
        return self.store.files.get(file_id)

    async def mark_committed(self, db, file_id: UUID) -> FileMeta | None:
        file = self.store.files.get(file_id)
        if file is None:
            return None
        committed = file.model_copy(update={"status": FileStatus.COMMITTED})
        self.store.files[file_id] = committed
        return committed


class FakeChunksRepo:
    """The ``chunks_repo`` functions ``storage`` calls, over a :class:`FakeStore`."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def insert_pending(
        self,
        db,
        *,
        file_id: UUID,
        chunk_index: int,
        size_bytes: int,
        channel_id: int,
        message_id: int,
        telegram_file_id: str,
        bot_id: str,
    ) -> ChunkMeta:
        chunk = ChunkMeta(
            id=uuid4(),
            file_id=file_id,
            chunk_index=chunk_index,
            size_bytes=size_bytes,
            channel_id=channel_id,
            message_id=message_id,
            telegram_file_id=telegram_file_id,
            bot_id=bot_id,
            status=ChunkStatus.PENDING,
            created_at=_now(),
        )
        self.store.chunks.append(chunk)
        return chunk

    async def mark_all_committed(self, db, file_id: UUID) -> list[ChunkMeta]:
        updated: list[ChunkMeta] = []
        for i, chunk in enumerate(self.store.chunks):
            if chunk.file_id == file_id:
                committed = chunk.model_copy(update={"status": ChunkStatus.COMMITTED})
                self.store.chunks[i] = committed
                updated.append(committed)
        return updated

    async def list_for_file(self, db, file_id: UUID) -> list[ChunkMeta]:
        rows = [c for c in self.store.chunks if c.file_id == file_id]
        return sorted(rows, key=lambda c: c.chunk_index)
