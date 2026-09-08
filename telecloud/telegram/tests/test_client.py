"""Tests for the per-bot Bot API client's request building and error mapping.

These exercise :class:`~telecloud.telegram.client.TelegramBot` directly against an
:class:`httpx.MockTransport`, covering the failure shapes the transport relies on:
network errors, malformed responses, and the transient/permanent split.
"""

from __future__ import annotations

import httpx
import pytest

from telecloud.shared import ErrorCode
from telecloud.telegram.client import TelegramBot
from telecloud.telegram.errors import TelegramError

pytestmark = pytest.mark.asyncio


def _bot(handler) -> TelegramBot:
    return TelegramBot(token="111:secret", transport=httpx.MockTransport(handler))


async def test_send_document_returns_message_and_file_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bot111:secret/sendDocument"
        assert b"hello" in request.content  # the bytes ride the multipart body
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 7, "document": {"file_id": "FID"}}},
        )

    bot = _bot(handler)
    try:
        message_id, file_id = await bot.send_document(-1001, b"hello")
    finally:
        await bot.aclose()
    assert (message_id, file_id) == (7, "FID")


async def test_unexpected_send_shape_is_permanent_error():
    def handler(request: httpx.Request) -> httpx.Response:
        # ok=True but missing the document/file_id we need.
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    bot = _bot(handler)
    with pytest.raises(TelegramError) as excinfo:
        await bot.send_document(-1001, b"x")
    await bot.aclose()
    assert not excinfo.value.transient
    assert excinfo.value.code == ErrorCode.TELEGRAM_ERROR.value


async def test_network_error_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    bot = _bot(handler)
    with pytest.raises(TelegramError) as excinfo:
        await bot.send_document(-1001, b"x")
    await bot.aclose()
    assert excinfo.value.transient


async def test_get_file_path_missing_path_is_permanent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {}})

    bot = _bot(handler)
    with pytest.raises(TelegramError) as excinfo:
        await bot.get_file_path("FID")
    await bot.aclose()
    assert not excinfo.value.transient


async def test_stream_file_non_2xx_maps_transient_for_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"unavailable")

    bot = _bot(handler)
    with pytest.raises(TelegramError) as excinfo:
        async for _ in bot.stream_file("documents/x.bin"):
            pass
    await bot.aclose()
    assert excinfo.value.transient
    assert excinfo.value.telegram_code == 503


async def test_error_body_description_surfaces_and_403_is_permanent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"ok": False, "error_code": 403, "description": "bot was blocked"},
        )

    bot = _bot(handler)
    with pytest.raises(TelegramError) as excinfo:
        await bot.delete_message(-1001, 5)
    await bot.aclose()
    assert not excinfo.value.transient
    assert excinfo.value.telegram_code == 403
    assert "bot was blocked" in excinfo.value.message
