"""The round-robin bot pool (SPEC.md §6.8).

The pool is the channel-aware roster of bots the transport draws from. It owns
two cheap decisions and nothing else:

* **which bot sends next** — strict round-robin across the configured tokens, so
  load (and Telegram's per-bot rate budget) spreads evenly.
* **which channel a fresh upload lands in** — round-robin across the configured
  channels when the caller doesn't pin one. Reads and deletes *do* pin a channel
  (a chunk remembers where it lives, SPEC §4.4), and may pin the originating bot
  too, since ``getFile`` file ids are bot-specific.

The pool holds bots and channel ids only — no files, no chunks, no DB (SPEC §6.8).
"""

from __future__ import annotations

import functools

import httpx

from telecloud.telegram.client import TelegramBot


class BotPool:
    """A fixed roster of :class:`TelegramBot`\\ s plus the channels they write to.

    Built from ``config`` via :meth:`from_settings`, or directly from explicit
    bots and channel ids (used by tests). Selection is round-robin and advances a
    simple cursor; under asyncio there's a single thread, so the only interleave
    point is ``await`` — selection itself is synchronous and never splits a step.
    """

    def __init__(self, bots: list[TelegramBot], channels: list[int]) -> None:
        if not bots:
            raise ValueError("BotPool requires at least one bot")
        if not channels:
            raise ValueError("BotPool requires at least one channel")
        self._bots = list(bots)
        self._by_id = {bot.bot_id: bot for bot in self._bots}
        self._channels = list(channels)
        self._bot_cursor = 0
        self._channel_cursor = 0

    @classmethod
    def from_settings(
        cls, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> "BotPool":
        """Build the pool from ``config.get_settings()`` (SPEC §5.2, §6.8).

        Imported lazily so the module stays importable (and unit-testable with
        explicit bots) without a fully-populated environment.
        """
        from telecloud.config import get_settings

        settings = get_settings()
        bots = [
            TelegramBot(token=token, transport=transport)
            for token in settings.telegram_bot_tokens
        ]
        return cls(bots, list(settings.telegram_channel_ids))

    # -- Selection ----------------------------------------------------------

    def next_bot(self) -> TelegramBot:
        """Return the next bot in round-robin order."""
        bot = self._bots[self._bot_cursor % len(self._bots)]
        self._bot_cursor += 1
        return bot

    def pick_channel(self) -> int:
        """Return the next channel id in round-robin order (for unpinned sends)."""
        channel = self._channels[self._channel_cursor % len(self._channels)]
        self._channel_cursor += 1
        return channel

    def get_bot(self, bot_id: str) -> TelegramBot:
        """Return the bot with ``bot_id``.

        Used to pin downloads/deletes to the bot that uploaded a chunk. Raises
        :class:`KeyError` if no bot with that id is in the pool (e.g. its token was
        removed from config) — the caller decides how to react.
        """
        return self._by_id[bot_id]

    # -- Introspection / lifecycle -----------------------------------------

    @property
    def bots(self) -> list[TelegramBot]:
        """The bots in the pool (a copy; mutating it doesn't affect selection)."""
        return list(self._bots)

    @property
    def channels(self) -> list[int]:
        """The channel ids uploads may target (a copy)."""
        return list(self._channels)

    async def aclose(self) -> None:
        """Close every bot's HTTP client (call at app shutdown)."""
        for bot in self._bots:
            await bot.aclose()


@functools.lru_cache(maxsize=1)
def get_pool() -> BotPool:
    """Return the process-wide shared :class:`BotPool` (SPEC §5.2).

    Built from settings on first call and reused thereafter so every send shares
    one pooled set of HTTP clients. Cleared by :func:`close_pool`.
    """
    return BotPool.from_settings()


async def close_pool() -> None:
    """Close and forget the shared pool (call at app shutdown)."""
    if get_pool.cache_info().currsize:
        await get_pool().aclose()
        get_pool.cache_clear()
