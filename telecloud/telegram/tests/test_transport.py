"""Tests for the three transport functions: send, stream, delete.

Everything runs against an :class:`httpx.MockTransport` Bot API plus an in-memory
fake limiter and queue, so no network or Redis is touched. Coverage focuses on the
behaviours the SPEC calls out: round-robin rotation, the rate-limit / transient
retry-enqueue path, and the streaming read with no disk buffering.
"""

from __future__ import annotations

import pytest

from telecloud.shared import ErrorCode, TeleCloudError
from telecloud.telegram import limits
from telecloud.telegram.errors import TelegramError
from telecloud.telegram.transport import SendResult, TelegramTransport
from telecloud.telegram.tests._fakes import (
    FakeLimiter,
    FakeQueue,
    decode_job_data,
    error_response,
    make_pool,
    multipart_contains,
    telegram_handler,
)

pytestmark = pytest.mark.asyncio


def _transport(handler, *, limiter=None, queue=None, **pool_kwargs) -> TelegramTransport:
    pool = make_pool(handler, **pool_kwargs)
    return TelegramTransport(
        pool,
        limiter=limiter or FakeLimiter(allow=True),
        queue=queue or FakeQueue(),
    )


# -- send_document ----------------------------------------------------------


async def test_send_document_rotates_bots_and_reports_identifiers():
    handler = telegram_handler()
    limiter = FakeLimiter(allow=True)
    queue = FakeQueue()
    transport = _transport(
        handler, limiter=limiter, queue=queue,
        tokens=["111:a", "222:b", "333:c"], channels=[-1001],
    )

    results = [await transport.send_document(None, b"chunk") for _ in range(4)]

    # Round-robin across the three bots, wrapping back to the first.
    assert [r.bot_id for r in results] == ["111", "222", "333", "111"]
    # Each result is the SPEC tuple plus the channel the pool picked.
    first = results[0]
    assert isinstance(first, SendResult)
    assert first.message_id == 1001
    assert first.telegram_file_id == "FILEID1"
    assert first.channel_id == -1001
    # Nothing failed, so nothing was queued.
    assert queue.jobs == []


async def test_send_document_pins_channel_when_given():
    handler = telegram_handler()
    transport = _transport(handler, channels=[-1001, -1002])

    result = await transport.send_document(-99999, b"data")

    assert result.channel_id == -99999
    # The uploaded bytes actually rode the request body.
    assert multipart_contains(handler.requests[0], b"data")


async def test_send_document_spends_bot_and_channel_budget():
    handler = telegram_handler()
    limiter = FakeLimiter(allow=True)
    transport = _transport(handler, limiter=limiter, tokens=["111:a"], channels=[-1001])

    await transport.send_document(None, b"x")

    assert limits.bot_key("111") in limiter.keys
    assert limits.channel_key(-1001) in limiter.keys
    # The Telegram numbers are supplied by this module, not the generic limiter.
    bot_call = next(c for c in limiter.calls if c[0] == limits.bot_key("111"))
    assert bot_call[1:] == (limits.PER_BOT_RATE, limits.PER_BOT_WINDOW_SECONDS)
    channel_call = next(c for c in limiter.calls if c[0] == limits.channel_key(-1001))
    assert channel_call[1:] == (
        limits.PER_CHANNEL_RATE,
        limits.PER_CHANNEL_WINDOW_SECONDS,
    )


async def test_send_document_transient_failure_enqueues_retry_and_raises():
    handler = telegram_handler(send_response=error_response(429))
    queue = FakeQueue()
    transport = _transport(handler, queue=queue, tokens=["111:a"], channels=[-1001])

    with pytest.raises(TeleCloudError) as excinfo:
        await transport.send_document(None, b"payload")

    err = excinfo.value
    assert err.code == ErrorCode.TELEGRAM_ERROR.value
    assert isinstance(err, TelegramError) and err.transient
    # The send was queued for retry, self-contained, carrying the bytes verbatim.
    assert len(queue.jobs) == 1
    job = queue.jobs[0]
    assert job["op"] == "send_document"
    assert job["channel_id"] == -1001
    assert job["bot_id"] == "111"
    assert decode_job_data(job) == b"payload"


async def test_send_document_rate_limit_denied_enqueues_without_calling_telegram():
    handler = telegram_handler()
    # Deny only the per-bot key; the send must never reach Telegram.
    limiter = FakeLimiter(allow=lambda key: not key.startswith("telegram:bot:"))
    queue = FakeQueue()
    transport = _transport(
        handler, limiter=limiter, queue=queue, tokens=["111:a"], channels=[-1001]
    )

    with pytest.raises(TelegramError) as excinfo:
        await transport.send_document(None, b"payload")

    assert excinfo.value.transient
    assert len(queue.jobs) == 1
    assert queue.jobs[0]["op"] == "send_document"
    # No sendDocument request was made — we backed off before the network.
    assert handler.requests == []


async def test_send_document_permanent_failure_does_not_enqueue():
    handler = telegram_handler(send_response=error_response(400, description="bad"))
    queue = FakeQueue()
    transport = _transport(handler, queue=queue, tokens=["111:a"], channels=[-1001])

    with pytest.raises(TelegramError) as excinfo:
        await transport.send_document(None, b"payload")

    assert not excinfo.value.transient
    # A 400 won't fix itself, so it is not re-enqueued.
    assert queue.jobs == []


# -- get_file_stream --------------------------------------------------------


async def test_get_file_stream_yields_bytes_and_pins_bot():
    body = b"the quick brown fox" * 100
    handler = telegram_handler(file_path="documents/file_7.bin", download_body=body)
    transport = _transport(handler, tokens=["111:a", "222:b"])

    chunks = [
        piece
        async for piece in transport.get_file_stream(-1001, "FILEID1", bot_id="222")
    ]

    assert b"".join(chunks) == body
    # getFile + the download both ran on the pinned bot (222), not round-robin.
    assert all("/bot222:" in r.url.path for r in handler.requests)
    # The download GET hit the /file/ path (streamed, not buffered to disk).
    assert any("/file/bot222:" in r.url.path for r in handler.requests)


async def test_get_file_stream_only_spends_bot_budget():
    handler = telegram_handler(download_body=b"abc")
    limiter = FakeLimiter(allow=True)
    transport = _transport(handler, limiter=limiter, tokens=["111:a"])

    _ = [p async for p in transport.get_file_stream(-1001, "FID", bot_id="111")]

    # Reads don't post into the channel, so only the per-bot key is checked.
    assert limiter.keys == [limits.bot_key("111")]


async def test_get_file_stream_rate_limited_raises_without_enqueue():
    handler = telegram_handler(download_body=b"abc")
    limiter = FakeLimiter(allow=False)
    queue = FakeQueue()
    transport = _transport(handler, limiter=limiter, queue=queue, tokens=["111:a"])

    with pytest.raises(TelegramError) as excinfo:
        async for _ in transport.get_file_stream(-1001, "FID", bot_id="111"):
            pass

    assert excinfo.value.transient
    # A read has no background retry consumer, so nothing is queued and no
    # download was attempted.
    assert queue.jobs == []
    assert handler.requests == []


# -- delete_message ---------------------------------------------------------


async def test_delete_message_calls_api_on_pinned_bot():
    handler = telegram_handler()
    transport = _transport(handler, tokens=["111:a", "222:b"])

    await transport.delete_message(-1001, 555, bot_id="222")

    assert len(handler.requests) == 1
    request = handler.requests[0]
    assert request.url.path.endswith("/deleteMessage")
    assert "/bot222:" in request.url.path


async def test_delete_message_transient_failure_enqueues_and_raises():
    handler = telegram_handler(delete_ok=False)  # deleteMessage returns 500
    queue = FakeQueue()
    transport = _transport(handler, queue=queue, tokens=["111:a"])

    with pytest.raises(TelegramError) as excinfo:
        await transport.delete_message(-1001, 555, bot_id="111")

    assert excinfo.value.transient
    assert len(queue.jobs) == 1
    job = queue.jobs[0]
    assert job == {
        "op": "delete_message",
        "channel_id": -1001,
        "message_id": 555,
        "bot_id": "111",
    }
