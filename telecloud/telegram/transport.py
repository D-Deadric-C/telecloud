"""The three transport functions: send, stream, delete (SPEC.md §6.8).

This is where the pool, the rate limiter, and the retry queue come together. Each
call:

1. picks a bot (round-robin) and a channel (pinned, or round-robin for sends),
2. spends the Telegram rate budget for that bot/channel via ``rate_limit.limiter``
   (the numbers live in :mod:`telecloud.telegram.limits`),
3. moves the bytes via the per-bot :class:`~telecloud.telegram.client.TelegramBot`,
4. on a **transient** failure (limit denied, Telegram ``429``/``5xx``, network),
   enqueues a retry on ``rate_limit.queue`` and raises a ``telegram_error`` so the
   caller can react now (SPEC §6.8). Permanent failures just raise.

Every successful op reports back which ``channel_id`` and ``bot_id`` it used
(:class:`SendResult`) so the caller — ``storage`` — can persist that on the chunk
row (SPEC §4.4). This module returns identifiers; it never touches files, chunks,
quota, or the DB (SPEC §6.8).

The retry queue is used for **sends and deletes** — operations ``jobs/`` can
replay later. A *download* is a live stream to a waiting caller; there's no
meaningful "retry this read in the background", so a failed read raises but does
not enqueue.
"""

from __future__ import annotations

import base64
from typing import Any, AsyncIterator, NamedTuple, Protocol

from telecloud.rate_limit import limiter as default_limiter
from telecloud.rate_limit import queue as default_queue
from telecloud.telegram import limits
from telecloud.telegram.client import TelegramBot
from telecloud.telegram.errors import TelegramError
from telecloud.telegram.pool import BotPool, get_pool


class SendResult(NamedTuple):
    """What :meth:`TelegramTransport.send_document` reports back.

    The first three fields are the tuple SPEC §6.8 specifies
    (``message_id, telegram_file_id, bot_id``); :attr:`channel_id` is appended
    because chunk rows are channel-aware (SPEC §4.4) and the transport — not the
    caller — chooses the channel when it isn't pinned, so it must report which one
    it used.
    """

    message_id: int
    telegram_file_id: str
    bot_id: str
    channel_id: int


class _Limiter(Protocol):
    async def check(self, key: str, limit: int, window: float) -> bool: ...


class _Queue(Protocol):
    async def enqueue(self, job: dict) -> Any: ...


class TelegramTransport:
    """Bot pool + rate limiter + retry queue, exposing the three transport ops.

    ``limiter`` and ``queue`` default to the shared ``rate_limit`` modules and are
    injectable for tests. The limits default to Telegram's documented values
    (:mod:`telecloud.telegram.limits`).
    """

    def __init__(
        self,
        pool: BotPool,
        *,
        limiter: _Limiter = default_limiter,
        queue: _Queue = default_queue,
        per_bot_rate: int = limits.PER_BOT_RATE,
        per_bot_window: float = limits.PER_BOT_WINDOW_SECONDS,
        per_channel_rate: int = limits.PER_CHANNEL_RATE,
        per_channel_window: float = limits.PER_CHANNEL_WINDOW_SECONDS,
    ) -> None:
        self._pool = pool
        self._limiter = limiter
        self._queue = queue
        self._per_bot_rate = per_bot_rate
        self._per_bot_window = per_bot_window
        self._per_channel_rate = per_channel_rate
        self._per_channel_window = per_channel_window

    @property
    def pool(self) -> BotPool:
        """The underlying bot pool (handy for wiring and tests)."""
        return self._pool

    # -- send ---------------------------------------------------------------

    async def send_document(
        self, channel_id: int | None, data: bytes
    ) -> SendResult:
        """Upload ``data`` to ``channel_id`` (or a pool-picked channel) via the next bot.

        Returns the ``(message_id, telegram_file_id, bot_id, channel_id)`` the
        caller persists. On a transient failure enqueues a retry and raises
        :class:`TelegramError`; on a permanent failure just raises.
        """
        channel = channel_id if channel_id is not None else self._pool.pick_channel()
        bot = self._pool.next_bot()

        def retry_job() -> dict:
            # Self-contained so jobs/ can replay the send without our context. The
            # bytes ride along base64-encoded since the queue stores JSON dicts
            # (SPEC §6.6); a chunk is bounded at 18 MiB (SPEC §1).
            return {
                "op": "send_document",
                "channel_id": channel,
                "bot_id": bot.bot_id,
                "data_b64": base64.b64encode(data).decode("ascii"),
            }

        await self._spend_budget(bot.bot_id, channel, on_denied=retry_job)
        try:
            message_id, file_id = await bot.send_document(channel, data)
        except TelegramError as exc:
            await self._maybe_enqueue(exc, retry_job)
            raise
        return SendResult(message_id, file_id, bot.bot_id, channel)

    # -- stream -------------------------------------------------------------

    async def get_file_stream(
        self, channel_id: int, file_id: str, *, bot_id: str | None = None
    ) -> AsyncIterator[bytes]:
        """Stream a chunk's bytes back via ``getFile`` — no disk buffering (SPEC §1).

        ``file_id`` is the Telegram ``file_id`` from the original ``sendDocument``
        (stored as ``telegram_file_id`` on the chunk, SPEC §4.4) — ``getFile``
        needs it, not the message id. ``bot_id`` pins the download to the bot that
        uploaded the chunk, which is required because file ids are bot-specific;
        when omitted (single-bot deploys) the next round-robin bot is used.
        ``channel_id`` is accepted for the channel-aware contract but ``getFile``
        doesn't post into a channel, so only the per-bot budget guards reads.

        A failed read raises :class:`TelegramError` but does **not** enqueue a
        retry — there's no background consumer for a re-download.
        """
        bot = self._pool.get_bot(bot_id) if bot_id is not None else self._pool.next_bot()
        if not await self._limiter.check(
            limits.bot_key(bot.bot_id), self._per_bot_rate, self._per_bot_window
        ):
            raise TelegramError(
                f"Per-bot rate limit reached for bot {bot.bot_id}.", transient=True
            )
        file_path = await bot.get_file_path(file_id)
        async for piece in bot.stream_file(file_path):
            yield piece

    # -- delete -------------------------------------------------------------

    async def delete_message(
        self, channel_id: int, message_id: int, *, bot_id: str | None = None
    ) -> None:
        """Delete ``message_id`` from ``channel_id`` via the pinned (or next) bot.

        On a transient failure enqueues a retry and raises; on a permanent failure
        just raises. ``bot_id`` pins the bot that owns the message when known.
        """
        bot = self._pool.get_bot(bot_id) if bot_id is not None else self._pool.next_bot()

        def retry_job() -> dict:
            return {
                "op": "delete_message",
                "channel_id": channel_id,
                "message_id": message_id,
                "bot_id": bot.bot_id,
            }

        await self._spend_budget(bot.bot_id, channel_id, on_denied=retry_job)
        try:
            await bot.delete_message(channel_id, message_id)
        except TelegramError as exc:
            await self._maybe_enqueue(exc, retry_job)
            raise

    # -- shared plumbing ----------------------------------------------------

    async def _spend_budget(self, bot_id: str, channel_id: int, *, on_denied) -> None:
        """Spend one per-bot and one per-channel token, or enqueue + raise.

        A denial is a transient back-pressure signal: the job is enqueued for a
        later retry and a transient :class:`TelegramError` is raised so the caller
        backs off now (SPEC §6.8).
        """
        bot_ok = await self._limiter.check(
            limits.bot_key(bot_id), self._per_bot_rate, self._per_bot_window
        )
        channel_ok = await self._limiter.check(
            limits.channel_key(channel_id),
            self._per_channel_rate,
            self._per_channel_window,
        )
        if bot_ok and channel_ok:
            return
        await self._queue.enqueue(on_denied())
        which = "bot" if not bot_ok else "channel"
        raise TelegramError(
            f"Telegram {which} rate limit reached; send was queued for retry.",
            transient=True,
        )

    async def _maybe_enqueue(self, exc: TelegramError, job_factory) -> None:
        """Enqueue a retry for ``job_factory`` iff ``exc`` is transient."""
        if exc.transient:
            await self._queue.enqueue(job_factory())


# ---------------------------------------------------------------------------
# Process-wide transport + module-level convenience API (SPEC §6.8 public API)
# ---------------------------------------------------------------------------

_transport: TelegramTransport | None = None


def get_transport() -> TelegramTransport:
    """Return the process-wide :class:`TelegramTransport`, built on first use.

    Bound to the shared bot pool and the shared ``rate_limit`` limiter/queue.
    Reset by :func:`close`.
    """
    global _transport
    if _transport is None:
        _transport = TelegramTransport(get_pool())
    return _transport


async def send_document(channel_id: int | None, data: bytes) -> SendResult:
    """Module-level convenience for the shared transport (SPEC §6.8)."""
    return await get_transport().send_document(channel_id, data)


def get_file_stream(
    channel_id: int, file_id: str, *, bot_id: str | None = None
) -> AsyncIterator[bytes]:
    """Module-level convenience for the shared transport (SPEC §6.8).

    Returns the async byte iterator; ``async for`` over it to stream the chunk.
    """
    return get_transport().get_file_stream(channel_id, file_id, bot_id=bot_id)


async def delete_message(
    channel_id: int, message_id: int, *, bot_id: str | None = None
) -> None:
    """Module-level convenience for the shared transport (SPEC §6.8)."""
    await get_transport().delete_message(channel_id, message_id, bot_id=bot_id)


async def close() -> None:
    """Release the shared transport's pool and forget it (call at shutdown)."""
    global _transport
    from telecloud.telegram.pool import close_pool

    await close_pool()
    _transport = None
