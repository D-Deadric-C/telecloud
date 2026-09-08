"""Async Upstash Redis transport over the REST API (SPEC.md §5.4, §6.6).

Upstash exposes Redis over a small **HTTP REST API** rather than the binary
RESP protocol, and ``config`` hands us a REST URL + bearer token
(``upstash_redis_rest_url`` / ``upstash_redis_rest_token``). We talk to it
directly over an async :class:`httpx.AsyncClient` so nothing blocks the event loop
(SPEC §5.4) and no extra Redis dependency is needed.

A single Redis command is one ``POST`` whose JSON body is the command as an
array of strings, e.g. ``["RPUSH", "q", "{...}"]``; the response is
``{"result": ...}`` on success or ``{"error": "..."}`` on a Redis-level error.

Two layers live here:

* :class:`UpstashRedis` — the thin REST transport plus the handful of typed
  command wrappers the limiter and queue need, including the one *atomic*
  operation the limiter relies on (:meth:`UpstashRedis.sliding_window_allow`,
  implemented server-side as a Lua script so the read-then-write decision can't
  race).
* :class:`RedisBackend` — a structural ``Protocol`` describing exactly those
  operations, so the limiter and queue depend on the capability, not the
  concrete client. Tests substitute an in-memory backend implementing the same
  protocol (``tests/_fake_redis.py``).

This module knows nothing about Telegram, HTTP routing, or DB rows (SPEC §6.6);
it only moves values in and out of Redis.
"""

from __future__ import annotations

import functools
from typing import Any, Protocol, runtime_checkable

import httpx

from telecloud.shared import ErrorCode, TeleCloudError

#: Network timeout for a single Redis REST call. Rate-limit and queue checks sit
#: on the hot path of other requests, so a slow Redis is treated as a failure
#: rather than left to hang the caller.
DEFAULT_TIMEOUT_SECONDS = 5.0

#: Atomic sliding-window-log limiter, run server-side via ``EVAL`` so the
#: read-modify-write (count → maybe add) cannot interleave between callers.
#:
#: ``KEYS[1]``  the sorted set holding one member per request in the window.
#: ``ARGV[1]``  now, in milliseconds.
#: ``ARGV[2]``  window length, in milliseconds.
#: ``ARGV[3]``  the limit (max requests allowed within the window).
#: ``ARGV[4]``  a unique member id for this request.
#:
#: Returns ``1`` if the request is allowed (and records it) or ``0`` if denied.
SLIDING_WINDOW_SCRIPT = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
-- Drop entries that have aged out of the window.
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)
local count = redis.call('ZCARD', KEYS[1])
if count < limit then
    redis.call('ZADD', KEYS[1], now, member)
    redis.call('PEXPIRE', KEYS[1], window)
    return 1
end
return 0
"""


@runtime_checkable
class RedisBackend(Protocol):
    """The Redis operations the limiter and queue depend on.

    A structural protocol so callers (and tests) can swap the real
    :class:`UpstashRedis` for an in-memory fake without inheritance. Every method
    is ``async`` because all I/O in TeleCloud is async (SPEC §5.4).
    """

    async def sliding_window_allow(
        self, key: str, limit: int, window_ms: int, now_ms: int, member: str
    ) -> bool:
        """Atomically record this request and report whether it is allowed."""
        ...

    async def rpush(self, key: str, *values: str) -> int:
        """Append ``values`` to the tail of the list at ``key``; return length."""
        ...

    async def lpop(self, key: str) -> str | None:
        """Pop and return the head of the list at ``key``, or ``None`` if empty."""
        ...

    async def llen(self, key: str) -> int:
        """Return the length of the list at ``key`` (``0`` if it doesn't exist)."""
        ...

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        """Return the elements of the list at ``key`` in ``[start, stop]``."""
        ...

    async def delete(self, *keys: str) -> int:
        """Delete ``keys``; return how many existed."""
        ...

    async def aclose(self) -> None:
        """Release any underlying resources."""
        ...


def _stringify(arg: object) -> str:
    """Render one command argument as the string Redis/Upstash expects."""
    if isinstance(arg, str):
        return arg
    if isinstance(arg, bool):  # before int: bool is an int subclass
        return "1" if arg else "0"
    return str(arg)


class UpstashRedis:
    """A minimal async client for the Upstash Redis REST API.

    Build it from settings via :meth:`from_settings`, or directly with a
    ``rest_url`` and ``rest_token``. A custom ``transport`` may be injected for
    tests (e.g. :class:`httpx.MockTransport`) so no network is touched, exactly
    like the Resend client. The underlying :class:`httpx.AsyncClient` is created
    lazily on first use and reused; close it with :meth:`aclose` at shutdown.
    """

    def __init__(
        self,
        *,
        rest_url: str,
        rest_token: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._rest_url = rest_url.rstrip("/")
        self._rest_token = rest_token
        self._timeout = timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def from_settings(cls) -> "UpstashRedis":
        """Build a client from ``config.get_settings()`` (SPEC §5.2, §6.6)."""
        # Imported here so the module stays importable (and unit-testable with an
        # injected transport) without a fully-populated environment.
        from telecloud.config import get_settings

        settings = get_settings()
        return cls(
            rest_url=settings.upstash_redis_rest_url,
            rest_token=settings.upstash_redis_rest_token,
        )

    def _get_client(self) -> httpx.AsyncClient:
        """Return the lazily-created, reused async HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._rest_url,
                headers={"Authorization": f"Bearer {self._rest_token}"},
                timeout=self._timeout,
                transport=self._transport,
            )
        return self._client

    async def command(self, *args: object) -> Any:
        """Execute one Redis command and return its decoded ``result``.

        The command is sent as a JSON array of string arguments. Raises
        :class:`TeleCloudError` (``internal_error``) on a transport failure, a
        non-2xx response, or a Redis-level ``error`` in the body, so callers can
        decide retry policy (SPEC §6.6) without parsing HTTP themselves.
        """
        payload = [_stringify(arg) for arg in args]
        client = self._get_client()
        try:
            response = await client.post("/", json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TeleCloudError.from_code(
                ErrorCode.INTERNAL_ERROR,
                "Redis request failed: Upstash returned "
                f"HTTP {exc.response.status_code}.",
            ) from exc
        except httpx.HTTPError as exc:
            raise TeleCloudError.from_code(
                ErrorCode.INTERNAL_ERROR,
                "Redis request failed: could not reach Upstash.",
            ) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise TeleCloudError.from_code(
                ErrorCode.INTERNAL_ERROR,
                "Redis request failed: malformed response from Upstash.",
            ) from exc

        if isinstance(body, dict) and body.get("error"):
            raise TeleCloudError.from_code(
                ErrorCode.INTERNAL_ERROR,
                f"Redis command error: {body['error']}",
            )
        return body.get("result") if isinstance(body, dict) else body

    # -- Typed command wrappers used by the limiter and queue --------------

    async def sliding_window_allow(
        self, key: str, limit: int, window_ms: int, now_ms: int, member: str
    ) -> bool:
        """Run :data:`SLIDING_WINDOW_SCRIPT` atomically; return ``True`` if allowed."""
        result = await self.command(
            "EVAL", SLIDING_WINDOW_SCRIPT, 1, key, now_ms, window_ms, limit, member
        )
        return bool(result)

    async def rpush(self, key: str, *values: str) -> int:
        return int(await self.command("RPUSH", key, *values) or 0)

    async def lpop(self, key: str) -> str | None:
        result = await self.command("LPOP", key)
        return None if result is None else str(result)

    async def llen(self, key: str) -> int:
        return int(await self.command("LLEN", key) or 0)

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        result = await self.command("LRANGE", key, start, stop)
        return [str(item) for item in (result or [])]

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return int(await self.command("DEL", *keys) or 0)

    async def aclose(self) -> None:
        """Close the underlying HTTP client if it was created."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


@functools.lru_cache(maxsize=1)
def get_redis() -> UpstashRedis:
    """Return the process-wide shared :class:`UpstashRedis` (SPEC §5.2).

    Built from settings on first call and reused thereafter so the limiter and
    queue share one pooled HTTP client. Cleared by :func:`close_redis`.
    """
    return UpstashRedis.from_settings()


async def close_redis() -> None:
    """Close and forget the shared client (call at app shutdown)."""
    if get_redis.cache_info().currsize:
        await get_redis().aclose()
        get_redis.cache_clear()
