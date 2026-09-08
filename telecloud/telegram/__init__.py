"""``telecloud.telegram`` — bot pool + transport (SPEC.md §6.8).

The Telegram transport: a **round-robin bot pool** that moves bytes to and from a
private channel. It knows about bots, channels, messages, and bytes — and nothing
about files, chunks-as-a-concept, quota, or DB rows (SPEC §6.8). It returns
identifiers; ``storage`` persists them.

Public surface:

* the three transport functions, each ``async`` and rate-limited per bot
  (~30 msg/s) and per channel (~20 msg/min) via ``rate_limit`` —
  :func:`send_document`, :func:`get_file_stream`, :func:`delete_message`;
* the pool accessor :func:`get_pool` and the :class:`TelegramTransport` /
  :class:`BotPool` classes for wiring and tests;
* :class:`SendResult` (what a send reports back) and :class:`TelegramError`.

Call :func:`close` at shutdown to release the pool's HTTP clients.

**Boundaries (SPEC §6.8):** depends only on ``config``, ``shared``, and
``rate_limit``. No files, chunks, quota, DB, or disk buffering on download.

----

**Flagged deviations from the SPEC §6.8 signatures** (not silently changed — see
the build notes): ``get_file_stream``'s second argument is the Telegram
``file_id`` (stored as ``telegram_file_id``, SPEC §4.4), not the ``message_id``,
because ``getFile`` requires the file id; reads/deletes also take an optional
``bot_id`` to pin the bot that uploaded the chunk (file ids are bot-specific);
and :class:`SendResult` appends the chosen ``channel_id`` to the SPEC tuple so
channel-aware chunk rows can record it.
"""

from telecloud.telegram.errors import TelegramError
from telecloud.telegram.pool import BotPool, close_pool, get_pool
from telecloud.telegram.transport import (
    SendResult,
    TelegramTransport,
    close,
    delete_message,
    get_file_stream,
    get_transport,
    send_document,
)

__all__ = [
    # public transport functions (SPEC §6.8)
    "send_document",
    "get_file_stream",
    "delete_message",
    # pool + transport accessors
    "get_pool",
    "get_transport",
    "BotPool",
    "TelegramTransport",
    # results / errors
    "SendResult",
    "TelegramError",
    # lifecycle
    "close",
    "close_pool",
]
