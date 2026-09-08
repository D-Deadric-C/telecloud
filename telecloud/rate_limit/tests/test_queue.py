"""Tests for the retry queue: enqueue/dequeue FIFO and dead-letter transitions.

Backed by the in-memory :class:`FakeRedis`, so list semantics match production
without a real Redis.
"""

from __future__ import annotations

import pytest

from telecloud.rate_limit.queue import QueuedJob, RetryQueue
from telecloud.rate_limit.tests._fake_redis import FakeRedis

pytestmark = pytest.mark.asyncio


def _queue(**kwargs) -> RetryQueue:
    return RetryQueue(FakeRedis(), **kwargs)


async def test_enqueue_then_dequeue_returns_payload():
    queue = _queue()
    job = {"action": "delete", "channel_id": -100, "message_id": 7}

    job_id = await queue.enqueue(job)
    assert job_id  # a non-empty id is assigned

    dequeued = await queue.dequeue()
    assert isinstance(dequeued, QueuedJob)
    assert dequeued.id == job_id
    assert dequeued.payload == job
    assert dequeued.attempts == 0
    assert dequeued.enqueued_at  # timestamped


async def test_dequeue_empty_returns_none():
    queue = _queue()
    assert await queue.dequeue() is None


async def test_fifo_order():
    queue = _queue()
    await queue.enqueue({"n": 1})
    await queue.enqueue({"n": 2})
    await queue.enqueue({"n": 3})

    assert (await queue.dequeue()).payload == {"n": 1}
    assert (await queue.dequeue()).payload == {"n": 2}
    assert (await queue.dequeue()).payload == {"n": 3}
    assert await queue.dequeue() is None


async def test_mark_failed_requeues_until_dead_letter():
    queue = _queue(max_attempts=3)
    await queue.enqueue({"task": "x"})

    # 1st failure -> re-enqueued (attempts now 1).
    job = await queue.dequeue()
    assert await queue.mark_failed(job) is False
    assert await queue.depth() == 1

    # 2nd failure -> re-enqueued (attempts now 2).
    job = await queue.dequeue()
    assert job.attempts == 1
    assert await queue.mark_failed(job) is False

    # 3rd failure -> reaches max_attempts -> dead-lettered.
    job = await queue.dequeue()
    assert job.attempts == 2
    assert await queue.mark_failed(job) is True

    # Ready queue drained; the job sits in the dead-letter list.
    assert await queue.depth() == 0
    assert await queue.dequeue() is None
    dead = await queue.dead_letter()
    assert len(dead) == 1
    assert dead[0].payload == {"task": "x"}
    assert dead[0].attempts == 3


async def test_max_attempts_one_dead_letters_on_first_failure():
    queue = _queue(max_attempts=1)
    await queue.enqueue({"task": "y"})

    job = await queue.dequeue()
    assert await queue.mark_failed(job) is True
    assert await queue.depth() == 0
    assert [d.payload for d in await queue.dead_letter()] == [{"task": "y"}]


async def test_id_and_timestamp_preserved_across_retries():
    queue = _queue(max_attempts=5)
    job_id = await queue.enqueue({"task": "z"})

    first = await queue.dequeue()
    await queue.mark_failed(first)
    second = await queue.dequeue()

    assert second.id == job_id
    assert second.enqueued_at == first.enqueued_at
    assert second.attempts == first.attempts + 1


async def test_purge_clears_both_lists():
    queue = _queue(max_attempts=1)
    await queue.enqueue({"a": 1})
    job = await queue.dequeue()
    await queue.mark_failed(job)  # now in dead-letter
    await queue.enqueue({"b": 2})  # one waiting in ready

    await queue.purge()
    assert await queue.depth() == 0
    assert await queue.dead_letter() == []


async def test_envelope_round_trips_through_json():
    job = QueuedJob(id="abc", payload={"k": [1, 2, 3]}, attempts=2, enqueued_at="t")
    assert QueuedJob.from_json(job.to_json()) == job


async def test_enqueue_rejects_non_dict():
    queue = _queue()
    with pytest.raises(ValueError):
        await queue.enqueue(["not", "a", "dict"])  # type: ignore[arg-type]


async def test_enqueue_rejects_non_serializable():
    queue = _queue()
    with pytest.raises(ValueError):
        await queue.enqueue({"bad": object()})


async def test_zero_max_attempts_rejected():
    with pytest.raises(ValueError):
        RetryQueue(FakeRedis(), max_attempts=0)
