"""Async Telegram Bot API client for a single bot (SPEC.md §5.4, §6.8).

One :class:`TelegramBot` wraps one bot token and speaks the Bot API over an async
:class:`httpx.AsyncClient` — mirroring the Resend/Upstash transports so nothing
blocks the event loop (SPEC §5.4). It knows only how to move bytes for *this* bot
and how to turn any failure into a :class:`TelegramError`; it has no notion of
pools, files, chunks, or retries (those live in ``pool``/``transport``).

The Bot API is a set of HTTPS methods under ``/bot<token>/<method>`` returning
``{"ok": true, "result": …}`` on success or ``{"ok": false, "error_code": …,
"description": …}`` on failure. File **downloads** are a separate GET under
``/file/bot<token>/<file_path>`` and are streamed straight through with **no disk
buffering** (SPEC §1).
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import httpx

from telecloud.telegram.errors import TelegramError

#: Telegram Bot API host. Bot methods live at ``/bot<token>/<method>`` and file
#: downloads at ``/file/bot<token>/<file_path>`` under the same host.
API_BASE = "https://api.telegram.org"

#: Network timeout for a single Bot API call (``sendDocument`` etc.). The file
#: *download* stream uses a separate, more generous timeout below since an
#: 18 MiB chunk legitimately takes longer than a control call.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Timeout for the streaming file download. Generous because it covers pulling a
#: whole 18 MiB chunk; the per-read still can't hang forever.
DOWNLOAD_TIMEOUT_SECONDS = 120.0

#: Filename attached to each uploaded chunk. Telegram requires *a* name on a
#: document; the real name lives in our DB, so a neutral placeholder is fine —
#: this module never sees the user's filename (SPEC §6.8 "no files").
UPLOAD_FILENAME = "chunk.bin"


def derive_bot_id(token: str) -> str:
    """Return the public, non-secret bot id embedded in a bot token.

    A Telegram token is ``<bot_id>:<auth>`` (e.g. ``123456789:AA…``). The numeric
    prefix is the bot's public id; the part after the colon is the secret. We use
    the prefix as the round-robin pool key and the value stored as ``bot_id`` on a
    chunk (SPEC §4.4) so a secret never lands in the database. A token with no
    colon (shouldn't happen) falls back to the whole string.
    """
    return token.split(":", 1)[0]


class TelegramBot:
    """The Bot API surface for one bot: send, locate, stream, delete.

    Construct directly with a ``token`` (a custom ``transport`` may be injected
    for tests, exactly like the Resend/Upstash clients). The underlying
    :class:`httpx.AsyncClient` is created lazily and reused; close it with
    :meth:`aclose` at shutdown.
    """

    def __init__(
        self,
        *,
        token: str,
        base_url: str = API_BASE,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        download_timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self.bot_id = derive_bot_id(token)
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._download_timeout = download_timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Return the lazily-created, reused async HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            )
        return self._client

    # -- Bot API methods ----------------------------------------------------

    async def send_document(self, chat_id: int, data: bytes) -> tuple[int, str]:
        """Upload ``data`` as a document to ``chat_id``; return ``(message_id, file_id)``.

        ``message_id`` locates the message later (delete); ``file_id`` is what
        ``getFile`` needs to download the bytes back. Raises :class:`TelegramError`
        on any failure.
        """
        result = await self._post(
            "sendDocument",
            data={"chat_id": str(chat_id)},
            files={"document": (UPLOAD_FILENAME, data, "application/octet-stream")},
        )
        try:
            message_id = int(result["message_id"])
            file_id = str(result["document"]["file_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TelegramError(
                "Telegram sendDocument returned an unexpected response shape.",
                transient=False,
            ) from exc
        return message_id, file_id

    async def get_file_path(self, file_id: str) -> str:
        """Resolve ``file_id`` to a downloadable ``file_path`` via ``getFile``.

        ``file_id`` values are **bot-specific**: a file id minted by one bot is
        only valid for that same bot, which is why downloads must run on the bot
        that uploaded the chunk (its id is stored on the chunk row, SPEC §4.4).
        """
        result = await self._post("getFile", data={"file_id": file_id})
        try:
            return str(result["file_path"])
        except (KeyError, TypeError) as exc:
            raise TelegramError(
                "Telegram getFile returned no file_path.",
                transient=False,
            ) from exc

    async def stream_file(self, file_path: str) -> AsyncIterator[bytes]:
        """Stream the bytes at ``file_path`` straight through — no disk buffering.

        Yields chunks as httpx reads them off the socket (SPEC §1). Raises
        :class:`TelegramError` if the download can't be reached or returns non-2xx.
        """
        client = self._get_client()
        url = f"/file/bot{self._token}/{file_path}"
        try:
            async with client.stream(
                "GET", url, timeout=self._download_timeout
            ) as response:
                if response.status_code // 100 != 2:
                    # Drain so the error body doesn't leak the connection, then map.
                    await response.aread()
                    raise TelegramError(
                        "Telegram file download failed: HTTP "
                        f"{response.status_code}.",
                        transient=_status_is_transient(response.status_code),
                        telegram_code=response.status_code,
                    )
                async for piece in response.aiter_bytes():
                    yield piece
        except httpx.HTTPError as exc:
            raise TelegramError(
                "Telegram file download failed: could not reach Telegram.",
                transient=True,
            ) from exc

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        """Delete ``message_id`` from ``chat_id``. Raises :class:`TelegramError` on failure."""
        await self._post(
            "deleteMessage",
            data={"chat_id": str(chat_id), "message_id": str(message_id)},
        )

    # -- Plumbing -----------------------------------------------------------

    async def _post(
        self,
        method: str,
        *,
        data: dict[str, str],
        files: dict[str, tuple] | None = None,
    ) -> Any:
        """POST one Bot API ``method`` and return its decoded ``result``.

        Raises :class:`TelegramError` (transient flagged) on a transport failure
        or any non-``ok`` Telegram response.
        """
        client = self._get_client()
        path = f"/bot{self._token}/{method}"
        try:
            response = await client.post(path, data=data, files=files)
        except httpx.HTTPError as exc:
            raise TelegramError(
                f"Telegram {method} failed: could not reach Telegram.",
                transient=True,
            ) from exc
        return self._decode(method, response)

    def _decode(self, method: str, response: httpx.Response) -> Any:
        """Parse a Bot API response, raising :class:`TelegramError` on failure."""
        try:
            body = response.json()
        except ValueError:
            body = None

        if (
            response.status_code // 100 == 2
            and isinstance(body, dict)
            and body.get("ok")
        ):
            return body.get("result")

        description = (
            body.get("description") if isinstance(body, dict) else None
        ) or f"HTTP {response.status_code}"
        telegram_code = (
            body.get("error_code") if isinstance(body, dict) else None
        ) or response.status_code
        raise TelegramError(
            f"Telegram {method} failed: {description}.",
            transient=_status_is_transient(response.status_code),
            telegram_code=telegram_code,
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client if it was created."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _status_is_transient(status_code: int) -> bool:
    """Whether retrying a call that returned ``status_code`` could help.

    ``429 Too Many Requests`` and any ``5xx`` are temporary — Telegram is busy or
    we were too chatty, and a later retry may succeed. Other ``4xx`` (bad request,
    forbidden, not found) are the caller's fault and won't fix themselves.
    """
    return status_code == 429 or status_code >= 500
