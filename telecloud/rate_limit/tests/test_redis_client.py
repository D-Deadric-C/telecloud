"""Tests for the Upstash REST transport.

No network: an :class:`httpx.MockTransport` is injected, so we assert on the
exact command bodies the client POSTs, how results are decoded, and how each
kind of failure maps to :class:`TeleCloudError`.
"""

from __future__ import annotations

import json

import httpx
import pytest

from telecloud.rate_limit.redis_client import (
    SLIDING_WINDOW_SCRIPT,
    UpstashRedis,
)
from telecloud.shared import ErrorCode, TeleCloudError

pytestmark = pytest.mark.asyncio


def _client(handler) -> UpstashRedis:
    return UpstashRedis(
        rest_url="https://example.upstash.io",
        rest_token="tok_test",
        transport=httpx.MockTransport(handler),
    )


async def test_command_posts_json_array_with_bearer_token():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"result": 2})

    client = _client(handler)
    try:
        result = await client.rpush("q", "a", "b")
    finally:
        await client.aclose()

    assert result == 2
    assert captured["method"] == "POST"
    assert captured["auth"] == "Bearer tok_test"
    assert captured["body"] == ["RPUSH", "q", "a", "b"]


async def test_sliding_window_allow_sends_eval_and_coerces_result():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"result": 1})

    client = _client(handler)
    try:
        allowed = await client.sliding_window_allow(
            "ratelimit:user:1", limit=5, window_ms=10_000, now_ms=1234, member="m1"
        )
    finally:
        await client.aclose()

    assert allowed is True
    # EVAL <script> <numkeys=1> <key> <now> <window> <limit> <member>
    assert captured["body"] == [
        "EVAL",
        SLIDING_WINDOW_SCRIPT,
        "1",
        "ratelimit:user:1",
        "1234",
        "10000",
        "5",
        "m1",
    ]


async def test_sliding_window_allow_false_on_zero():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": 0})

    client = _client(handler)
    try:
        allowed = await client.sliding_window_allow(
            "k", limit=1, window_ms=1000, now_ms=1, member="m"
        )
    finally:
        await client.aclose()
    assert allowed is False


async def test_lpop_returns_none_on_null_result():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": None})

    client = _client(handler)
    try:
        assert await client.lpop("q") is None
    finally:
        await client.aclose()


async def test_lrange_returns_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": ["x", "y"]})

    client = _client(handler)
    try:
        assert await client.lrange("q", 0, -1) == ["x", "y"]
    finally:
        await client.aclose()


async def test_redis_level_error_raises_internal_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "WRONGTYPE ..."})

    client = _client(handler)
    with pytest.raises(TeleCloudError) as excinfo:
        try:
            await client.llen("q")
        finally:
            await client.aclose()
    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR.value


async def test_non_2xx_raises_internal_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = _client(handler)
    with pytest.raises(TeleCloudError) as excinfo:
        try:
            await client.llen("q")
        finally:
            await client.aclose()
    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR.value
    assert excinfo.value.http_status == 500


async def test_network_error_raises_internal_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    client = _client(handler)
    with pytest.raises(TeleCloudError) as excinfo:
        try:
            await client.delete("q")
        finally:
            await client.aclose()
    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR.value


async def test_bool_arg_stringified_as_one_or_zero():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"result": "OK"})

    client = _client(handler)
    try:
        await client.command("SET", "flag", True)
    finally:
        await client.aclose()
    assert captured["body"] == ["SET", "flag", "1"]
