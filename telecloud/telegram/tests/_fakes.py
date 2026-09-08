"""Test doubles for the Telegram transport: a fake limiter, queue, and Bot API.

No network and no real Redis are touched. The Bot API is faked with an
:class:`httpx.MockTransport` (the same approach the Resend/Upstash clients use),
so a :class:`~telecloud.telegram.client.TelegramBot` runs its real request/parse
code against scripted responses. The limiter and queue are tiny in-memory stand-
ins matching the ``rate_limit`` module surface the transport depends on.
"""

from __future__ import annotations

from typing import Callable

import httpx

from telecloud.telegram.client import TelegramBot
from telecloud.telegram.pool import BotPool


class FakeLimiter:
    """An in-memory limiter recording every ``check`` and answering per policy.

    ``allow`` is either a bool (every key) or a predicate ``key -> bool`` so a
    test can deny just the per-bot or per-channel key. Every call is recorded on
    :attr:`calls` as ``(key, limit, window)``.
    """

    def __init__(self, allow: bool | Callable[[str], bool] = True) -> None:
        self._allow = allow
        self.calls: list[tuple[str, int, float]] = []

    async def check(self, key: str, limit: int, window: float) -> bool:
        self.calls.append((key, limit, window))
        if callable(self._allow):
            return self._allow(key)
        return self._allow

    @property
    def keys(self) -> list[str]:
        return [key for key, _, _ in self.calls]


class FakeQueue:
    """An in-memory retry queue recording every enqueued job dict."""

    def __init__(self) -> None:
        self.jobs: list[dict] = []

    async def enqueue(self, job: dict) -> str:
        self.jobs.append(job)
        return f"job-{len(self.jobs)}"


def telegram_handler(
    *,
    send_response: Callable[[httpx.Request], httpx.Response] | None = None,
    file_path: str = "documents/file_0.bin",
    download_body: bytes = b"",
    delete_ok: bool = True,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build an :class:`httpx.MockTransport` handler for the Bot API.

    Routes by URL path: ``sendDocument`` returns a synthetic ``message_id`` /
    ``file_id`` (overridable via ``send_response``), ``getFile`` returns
    ``file_path``, the file-download GET returns ``download_body``, and
    ``deleteMessage`` returns ok. Each request is appended to ``handler.requests``.
    """
    requests: list[httpx.Request] = []
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/sendDocument"):
            if send_response is not None:
                return send_response(request)
            counter["n"] += 1
            n = counter["n"]
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "message_id": 1000 + n,
                        "document": {"file_id": f"FILEID{n}"},
                    },
                },
            )
        if path.endswith("/getFile"):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": file_path}}
            )
        if "/file/bot" in path:
            return httpx.Response(200, content=download_body)
        if path.endswith("/deleteMessage"):
            if delete_ok:
                return httpx.Response(200, json={"ok": True, "result": True})
            return httpx.Response(
                500, json={"ok": False, "error_code": 500, "description": "boom"}
            )
        return httpx.Response(
            404, json={"ok": False, "error_code": 404, "description": "no method"}
        )

    handler.requests = requests  # type: ignore[attr-defined]
    return handler


def error_response(
    status_code: int, *, description: str = "boom"
) -> Callable[[httpx.Request], httpx.Response]:
    """A send handler that always fails with a Telegram-style error body."""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"ok": False, "error_code": status_code, "description": description},
        )

    return respond


def make_pool(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    tokens: list[str] | None = None,
    channels: list[int] | None = None,
) -> BotPool:
    """Build a :class:`BotPool` whose bots all share one mock transport."""
    tokens = tokens or ["111:AAA", "222:BBB", "333:CCC"]
    channels = channels if channels is not None else [-1001, -1002]
    transport = httpx.MockTransport(handler)
    bots = [TelegramBot(token=token, transport=transport) for token in tokens]
    return BotPool(bots, channels)


def multipart_contains(request: httpx.Request, data: bytes) -> bool:
    """Whether a multipart upload request body carries ``data`` verbatim."""
    return data in request.content


def decode_job_data(job: dict) -> bytes:
    """Decode the base64 payload bytes a queued ``send_document`` retry carries."""
    import base64

    return base64.b64decode(job["data_b64"])
