"""Upload-path tests: chunk count, the pending→committed transition, and the
"leave it pending on failure" contract (SPEC §6.9, §7.1)."""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from telecloud.shared import ChunkStatus, FileStatus, TeleCloudError
from telecloud.storage import store_upload
from telecloud.storage import upload as upload_mod
from telecloud.storage.tests._fakes import (
    FakeChunksRepo,
    FakeFilesRepo,
    FakeStore,
    FakeTransport,
)

CHUNK = 10  # tiny chunk size so tests are byte-exact and fast


async def _stream(data: bytes, piece: int = 4) -> AsyncIterator[bytes]:
    """Yield ``data`` in arbitrary ``piece``-sized bites (not chunk-aligned)."""
    for i in range(0, len(data), piece):
        yield data[i : i + piece]


@pytest.fixture
def wired(monkeypatch):
    """A FakeStore + FakeTransport with the repos monkeypatched into ``upload``."""
    store = FakeStore()
    monkeypatch.setattr(upload_mod, "files_repo", FakeFilesRepo(store))
    monkeypatch.setattr(upload_mod, "chunks_repo", FakeChunksRepo(store))
    return store, FakeTransport()


@pytest.mark.asyncio
async def test_chunk_count_matches_ceil_division(wired):
    store, transport = wired
    # 25 bytes / 10 → 3 chunks: 10, 10, 5.
    data = bytes(range(25))
    file = store.add_pending_file(size_bytes=len(data), chunk_count=3)

    # db is None — the repo fakes ignore it (storage just threads it through).
    await store_upload(
        None, file, _stream(data), transport=transport, chunk_size=CHUNK
    )

    assert len(transport.sent) == 3
    sizes = [len(p) for p in transport.sent]
    assert sizes == [10, 10, 5]
    # Reassembling the sent pieces reproduces the original bytes, in order.
    assert b"".join(transport.sent) == data

    chunks = sorted(store.chunks, key=lambda c: c.chunk_index)
    assert [c.chunk_index for c in chunks] == [0, 1, 2]
    assert [c.size_bytes for c in chunks] == [10, 10, 5]
    # Channel-aware identifiers were recorded from the transport (SPEC §4.4).
    assert all(c.channel_id == transport.channel_id for c in chunks)
    assert all(c.telegram_file_id and c.bot_id for c in chunks)


@pytest.mark.asyncio
async def test_commit_transitions_file_and_chunks(wired):
    store, transport = wired
    data = bytes(range(25))
    file = store.add_pending_file(size_bytes=len(data), chunk_count=3)
    assert file.status is FileStatus.PENDING

    committed = await store_upload(
        None, file, _stream(data), transport=transport, chunk_size=CHUNK
    )

    # File flipped to committed and returned (SPEC §7.1 step 5).
    assert committed.status is FileStatus.COMMITTED
    assert store.files[file.id].status is FileStatus.COMMITTED
    # Every chunk flipped to committed too.
    assert all(c.status is ChunkStatus.COMMITTED for c in store.chunks)


@pytest.mark.asyncio
async def test_exact_multiple_has_no_short_final_chunk(wired):
    store, transport = wired
    data = bytes(range(20))  # exactly 2 * CHUNK
    file = store.add_pending_file(size_bytes=len(data), chunk_count=2)

    await store_upload(None, file, _stream(data, piece=7), transport=transport,
                       chunk_size=CHUNK)

    assert [len(p) for p in transport.sent] == [10, 10]


@pytest.mark.asyncio
async def test_failed_send_leaves_file_pending(wired, monkeypatch):
    store, transport = wired
    data = bytes(range(25))
    file = store.add_pending_file(size_bytes=len(data), chunk_count=3)

    async def boom(channel_id, payload):
        raise TeleCloudError("telegram_error", "send failed", 502)

    monkeypatch.setattr(transport, "send_document", boom)

    with pytest.raises(TeleCloudError):
        await store_upload(None, file, _stream(data), transport=transport,
                           chunk_size=CHUNK)

    # The file is NOT committed — left pending for the jobs/ sweeper (SPEC §7.1 step 6).
    assert store.files[file.id].status is FileStatus.PENDING


@pytest.mark.asyncio
async def test_zero_byte_file_commits_with_no_chunks(wired):
    store, transport = wired
    file = store.add_pending_file(size_bytes=0, chunk_count=0)

    committed = await store_upload(None, file, _stream(b""), transport=transport,
                                   chunk_size=CHUNK)

    assert committed.status is FileStatus.COMMITTED
    assert transport.sent == []
    assert store.chunks == []
