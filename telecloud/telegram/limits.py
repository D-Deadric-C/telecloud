"""Telegram's rate limits and the keys we limit on (SPEC.md §6.8).

The limiter in ``rate_limit`` is deliberately generic — it knows nothing about
Telegram (SPEC §6.6). The *numbers* and the *key shapes* are Telegram-specific,
so they live here, the one module that owns Telegram knowledge.

Telegram's Bot API enforces roughly:

* **~30 messages per second per bot** across everything a bot sends.
* **~20 messages per minute into a single channel/group.**

These are the documented soft limits; staying just under them avoids the ``429
Too Many Requests`` responses (and ``retry_after`` backoffs) Telegram hands out
when a bot is too chatty. Sends (``sendDocument``) and deletes (``deleteMessage``)
are *messages* and count against both limits; a download (``getFile``) does not
post into the channel, so only the per-bot limit guards reads.
"""

from __future__ import annotations

#: Max messages a single bot may send per :data:`PER_BOT_WINDOW_SECONDS`.
PER_BOT_RATE = 30
PER_BOT_WINDOW_SECONDS = 1.0

#: Max messages that may land in one channel per :data:`PER_CHANNEL_WINDOW_SECONDS`.
PER_CHANNEL_RATE = 20
PER_CHANNEL_WINDOW_SECONDS = 60.0


def bot_key(bot_id: str) -> str:
    """Limiter key for one bot's per-second send budget."""
    return f"telegram:bot:{bot_id}"


def channel_key(channel_id: int) -> str:
    """Limiter key for one channel's per-minute message budget."""
    return f"telegram:channel:{channel_id}"
