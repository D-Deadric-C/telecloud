"""Tests for the cleanup jobs: orphan sweep, deferred delete, retry/dead-letter.

The ``database`` repos are monkeypatched with in-memory fakes (the ``storage/``
pattern); the retry queue is the **real** ``RetryQueue`` over an in-memory
``FakeRedis`` so the dead-letter transition under test is the production path.
Telegram deletes go through a scriptable :class:`FakeDelete`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from telecloud.rate_limit.queue import RetryQueue
from telecloud.rate_limit.tests._fake_redis import FakeRedis
from telecloud.shared import FileStatus

import telecloud.jobs.service as service
from telecloud.jobs.service import delete_deferred, process_retries, sweep_orphans
from telecloud.jobs.tests._fakes import (
    FakeChunksRepo,
    FakeDelete,
    FakeFilesRepo,
    FakeStore,
    permanent_error,
    transient_error,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    """A FakeStore with the repos monkeypatched into ``jobs.service``."""
    s = FakeStore()
    monkeypatch.setattr(service, "files_repo", FakeFilesRepo(s))
    monkeypatch.setattr(service, "chunks_repo", FakeChunksRepo(s))
    return s


def _queue(**kwargs) -> RetryQueue:
    return RetryQueue(FakeRedis(), **kwargs)


def _old() -> datetime:
    """A timestamp comfortably older than the default orphan threshold."""
    return datetime.now(timezone.utc) - timedelta(days=2)


# -- orphan sweep -----------------------------------------------------------


async def test_orphan_sweep_deletes_pending_messages_and_rows(store):
    file = store.add_file(status=FileStatus.PENDING, chunk_count=3, created_at=_old())
    delete = FakeDelete()
    queue = _queue()

    result = await sweep_orphans(db=None, delete_message=delete, queue=queue)

    # All three chunk messages deleted, with the chunks' Telegram coordinates.
    assert len(delete.calls) == 3
    assert {c[1] for c in delete.calls} == {5000, 5001, 5002}
    # File + chunk rows are gone (delete_row cascades the chunks).
    assert file.id not in store.files
    assert store.chunks == []
    assert result.files_removed == 1
    assert result.messages_deleted == 3
    assert result.messages_queued == 0


async def test_orphan_sweep_ignores_recent_pending(store):
    # A pending upload still within the threshold is a live upload, not an orphan.
    recent = store.add_file(status=FileStatus.PENDING, created_at=datetime.now(timezone.utc))
    delete = FakeDelete()

    result = await sweep_orphans(db=None, delete_message=delete, queue=_queue())

    assert delete.calls == []
    assert recent.id in store.files
    assert result.files_removed == 0


async def test_orphan_sweep_skips_committed_files(store):
    # Only pending files are swept; committed files are untouched.
    committed = store.add_file(status=FileStatus.COMMITTED, created_at=_old())

    result = await sweep_orphans(db=None, delete_message=FakeDelete(), queue=_queue())

    assert committed.id in store.files
    assert result.files_removed == 0


async def test_orphan_sweep_is_bounded_by_batch_size(store):
    for _ in range(5):
        store.add_file(status=FileStatus.PENDING, created_at=_old())

    result = await sweep_orphans(
        db=None, delete_message=FakeDelete(), queue=_queue(), batch_size=2
    )

    assert result.files_removed == 2
    assert len(store.files) == 3  # the remaining orphans wait for the next run


# -- deferred delete --------------------------------------------------------


async def test_deferred_delete_removes_deleting_files(store):
    file = store.add_file(status=FileStatus.DELETING, chunk_count=2)
    delete = FakeDelete()

    result = await delete_deferred(db=None, delete_message=delete, queue=_queue())

    assert len(delete.calls) == 2
    assert file.id not in store.files
    assert store.chunks == []
    assert result.files_removed == 1
    assert result.messages_deleted == 2


async def test_deferred_delete_ignores_committed_and_pending(store):
    store.add_file(status=FileStatus.COMMITTED)
    store.add_file(status=FileStatus.PENDING, created_at=_old())
    delete = FakeDelete()

    result = await delete_deferred(db=None, delete_message=delete, queue=_queue())

    assert delete.calls == []  # neither is in `deleting`
    assert result.files_removed == 0
    assert len(store.files) == 2


async def test_deferred_delete_does_not_touch_quota():
    # Structural guard for SPEC §6.14 "Must NOT re-decrement quota": the jobs module
    # has no quota dependency at all, so a deferred delete cannot double-count.
    assert not hasattr(service, "quota")


async def test_deferred_delete_is_idempotent_on_rerun(store):
    store.add_file(status=FileStatus.DELETING, chunk_count=2)
    delete = FakeDelete()
    queue = _queue()

    first = await delete_deferred(db=None, delete_message=delete, queue=queue)
    second = await delete_deferred(db=None, delete_message=delete, queue=queue)

    assert first.files_removed == 1
    assert second.files_removed == 0  # nothing left to do
    assert len(delete.calls) == 2  # the second run deletes no messages


# -- transient / permanent message-delete handling --------------------------


async def test_transient_failure_queues_retry_but_still_removes_rows(store):
    file = store.add_file(status=FileStatus.DELETING, chunk_count=1)
    # The single chunk's delete always fails transiently.
    delete = FakeDelete(fail=lambda n: transient_error())
    queue = _queue()

    result = await delete_deferred(db=None, delete_message=delete, queue=queue)

    # Rows are removed in one pass; the straggler message is queued for retry.
    assert file.id not in store.files
    assert result.files_removed == 1
    assert result.messages_deleted == 0
    assert result.messages_queued == 1
    assert await queue.depth() == 1  # one retry descriptor enqueued


async def test_permanent_failure_is_tolerated_and_rows_removed(store):
    # A permanent error (e.g. message already gone) must not block cleanup or enqueue.
    file = store.add_file(status=FileStatus.DELETING, chunk_count=1)
    delete = FakeDelete(fail=lambda n: permanent_error())
    queue = _queue()

    result = await delete_deferred(db=None, delete_message=delete, queue=queue)

    assert file.id not in store.files
    assert result.files_removed == 1
    assert result.messages_queued == 0
    assert await queue.depth() == 0  # permanent failures are not retried


# -- retry drain + dead-letter ----------------------------------------------


async def test_dead_letter_after_repeated_telegram_failures(store):
    # Drive the full path: a deleting file whose chunk delete keeps failing
    # transiently. The first run queues a retry; subsequent runs replay it until the
    # attempt budget is exhausted and it is dead-lettered.
    store.add_file(status=FileStatus.DELETING, chunk_count=1)
    delete = FakeDelete(fail=lambda n: transient_error())
    queue = _queue(max_attempts=3)

    # Run 1: deletes rows, enqueues the straggler (attempts=0 in the queue).
    first = await delete_deferred(db=None, delete_message=delete, queue=queue)
    assert first.messages_queued == 1
    assert await queue.depth() == 1

    # Subsequent runs find no files (rows gone) but drain the retry each time. With
    # max_attempts=3 the op is re-enqueued twice, then dead-lettered on the 3rd.
    dead_total = 0
    for _ in range(3):
        result = await delete_deferred(db=None, delete_message=delete, queue=queue)
        assert result.files_removed == 0  # nothing new to clean
        dead_total += result.retries_dead_lettered

    assert dead_total == 1
    assert await queue.depth() == 0  # no longer cycling in the ready queue
    dead = await queue.dead_letter()
    assert len(dead) == 1
    assert dead[0].payload["op"] == service.OP_DELETE_MESSAGE
    assert dead[0].attempts == 3


async def test_process_retries_replays_success_and_clears_queue(store):
    queue = _queue()
    await queue.enqueue(
        {"op": service.OP_DELETE_MESSAGE, "channel_id": -1001, "message_id": 7, "bot_id": "bot-1"}
    )
    delete = FakeDelete()  # succeeds

    processed, dead = await process_retries(queue=queue, delete_message=delete)

    assert (processed, dead) == (1, 0)
    assert delete.calls == [(-1001, 7, "bot-1")]
    assert await queue.depth() == 0


async def test_process_retries_is_bounded_by_batch_size(store):
    queue = _queue()
    for i in range(5):
        await queue.enqueue(
            {"op": service.OP_DELETE_MESSAGE, "channel_id": -1, "message_id": i, "bot_id": "b"}
        )

    processed, _ = await process_retries(
        queue=queue, delete_message=FakeDelete(), batch_size=2
    )

    assert processed == 2
    assert await queue.depth() == 3  # the rest wait for the next run
