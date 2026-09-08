"""Download-path tests: full stream, a single-chunk range, a range spanning two
chunks (the tricky case), status guards, and the framing metadata (SPEC §6.9, §7.2)."""

from __future__ import annotations

from typing import AsyncIterator

import pytest
import pytest_asyncio

from telecloud.shared import TeleCloudError
from telecloud.storage import ByteRange, open_download, parse_range, store_upload
from telecloud.storage import download as download_mod
from telecloud.storage import upload as upload_mod
from telecloud.storage.tests._fakes import (
    FakeChunksRepo,
    FakeFilesRepo,
    FakeStore,
    FakeTransport,
)

CHUNK = 10
DATA = bytes(range(25))  # 25 bytes → chunks [0:10], [10:20], [20:25]


async def _stream(data: bytes, piece: int = 4) -> AsyncIterator[bytes]:
    for i in range(0, len(data), piece):
        yield data[i : i + piece]


async def _collect(stream: AsyncIterator[bytes]) -> bytes:
    return b"".join([piece async for piece in stream])


@pytest_asyncio.fixture
async def uploaded(monkeypatch):
    """Upload DATA through storage, then wire the repos for the download path too."""
    store = FakeStore()
    transport = FakeTransport(read_piece_size=3)
    monkeypatch.setattr(upload_mod, "files_repo", FakeFilesRepo(store))
    monkeypatch.setattr(upload_mod, "chunks_repo", FakeChunksRepo(store))
    monkeypatch.setattr(download_mod, "files_repo", FakeFilesRepo(store))
    monkeypatch.setattr(download_mod, "chunks_repo", FakeChunksRepo(store))

    file = store.add_pending_file(size_bytes=len(DATA), chunk_count=3)
    committed = await store_upload(None, file, _stream(DATA), transport=transport,
                                   chunk_size=CHUNK)
    return store, transport, committed


@pytest.mark.asyncio
async def test_full_download_streams_all_bytes(uploaded):
    store, transport, file = uploaded

    resp = await open_download(None, file.id, transport=transport, chunk_size=CHUNK)

    assert resp.is_partial is False
    assert resp.status_code == 200
    assert resp.content_length == len(DATA)
    assert resp.content_range is None
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert "Content-Range" not in resp.headers
    assert resp.headers["Content-Type"] == "text/plain"
    assert await _collect(resp.stream) == DATA


@pytest.mark.asyncio
async def test_single_chunk_range(uploaded):
    store, transport, file = uploaded

    # bytes 2-5 live entirely inside chunk 0 (bytes 0-9).
    resp = await open_download(None, file.id, "bytes=2-5", transport=transport,
                               chunk_size=CHUNK)

    assert resp.is_partial is True
    assert resp.status_code == 206
    assert resp.content_length == 4
    assert resp.content_range == f"bytes 2-5/{len(DATA)}"
    assert await _collect(resp.stream) == DATA[2:6]


@pytest.mark.asyncio
async def test_range_spanning_two_chunks(uploaded):
    store, transport, file = uploaded

    # bytes 8-13: starts in chunk 0 (offset 8), crosses the 10-byte boundary, and
    # ends in chunk 1 — the case the engine must stitch together (SPEC §7.2).
    resp = await open_download(None, file.id, "bytes=8-13", transport=transport,
                               chunk_size=CHUNK)

    assert resp.is_partial is True
    assert resp.content_length == 6
    assert resp.content_range == f"bytes 8-13/{len(DATA)}"
    assert await _collect(resp.stream) == DATA[8:14]


@pytest.mark.asyncio
async def test_range_spanning_three_chunks_to_eof(uploaded):
    store, transport, file = uploaded

    # bytes 5- (open-ended) → from mid-chunk-0 all the way to the last byte,
    # crossing both boundaries and ending in the short final chunk.
    resp = await open_download(None, file.id, "bytes=5-", transport=transport,
                               chunk_size=CHUNK)

    assert resp.content_length == len(DATA) - 5
    assert resp.content_range == f"bytes 5-{len(DATA) - 1}/{len(DATA)}"
    assert await _collect(resp.stream) == DATA[5:]


@pytest.mark.asyncio
async def test_suffix_range(uploaded):
    store, transport, file = uploaded

    resp = await open_download(None, file.id, "bytes=-4", transport=transport,
                               chunk_size=CHUNK)

    assert resp.content_length == 4
    assert await _collect(resp.stream) == DATA[-4:]


@pytest.mark.asyncio
async def test_byte_range_object_is_accepted(uploaded):
    store, transport, file = uploaded

    resp = await open_download(None, file.id, ByteRange(8, 13), transport=transport,
                               chunk_size=CHUNK)

    assert await _collect(resp.stream) == DATA[8:14]


@pytest.mark.asyncio
async def test_range_end_is_clamped_to_size(uploaded):
    store, transport, file = uploaded

    resp = await open_download(None, file.id, "bytes=20-999", transport=transport,
                               chunk_size=CHUNK)

    assert resp.content_range == f"bytes 20-{len(DATA) - 1}/{len(DATA)}"
    assert await _collect(resp.stream) == DATA[20:]


@pytest.mark.asyncio
async def test_unsatisfiable_range_raises_416(uploaded):
    store, transport, file = uploaded

    with pytest.raises(TeleCloudError) as exc:
        await open_download(None, file.id, "bytes=100-200", transport=transport,
                            chunk_size=CHUNK)
    assert exc.value.http_status == 416


@pytest.mark.asyncio
async def test_malformed_range_raises_422(uploaded):
    store, transport, file = uploaded

    with pytest.raises(TeleCloudError) as exc:
        await open_download(None, file.id, "chunks=0-5", transport=transport,
                            chunk_size=CHUNK)
    assert exc.value.http_status == 422


@pytest.mark.asyncio
async def test_missing_file_raises_not_found(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(download_mod, "files_repo", FakeFilesRepo(store))
    monkeypatch.setattr(download_mod, "chunks_repo", FakeChunksRepo(store))
    from uuid import uuid4

    with pytest.raises(TeleCloudError) as exc:
        await open_download(None, uuid4())
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_pending_file_raises_upload_incomplete(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(download_mod, "files_repo", FakeFilesRepo(store))
    monkeypatch.setattr(download_mod, "chunks_repo", FakeChunksRepo(store))
    file = store.add_pending_file(size_bytes=25, chunk_count=3)  # never committed

    with pytest.raises(TeleCloudError) as exc:
        await open_download(None, file.id)
    assert exc.value.code == "upload_incomplete"


def test_parse_range_helper_is_reusable():
    # files/ may reuse the parser directly off a Range header.
    assert parse_range("bytes=0-9", 25) == ByteRange(0, 9)
    assert parse_range("bytes=5-", 25) == ByteRange(5, 24)
    assert parse_range("bytes=-4", 25) == ByteRange(21, 24)
