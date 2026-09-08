"""Tests for the round-robin bot pool and the per-bot client's id derivation."""

from __future__ import annotations

import httpx
import pytest

from telecloud.telegram.client import TelegramBot, derive_bot_id
from telecloud.telegram.pool import BotPool
from telecloud.telegram.tests._fakes import make_pool, telegram_handler


def test_derive_bot_id_uses_token_prefix():
    assert derive_bot_id("123456789:AAEsecretpart") == "123456789"
    # No colon (shouldn't happen in practice) falls back to the whole token.
    assert derive_bot_id("weirdtoken") == "weirdtoken"


def test_bot_id_is_not_the_secret():
    bot = TelegramBot(token="42:supersecretauth")
    assert bot.bot_id == "42"


def test_next_bot_round_robins_and_wraps():
    pool = make_pool(telegram_handler(), tokens=["1:a", "2:b", "3:c"])
    seen = [pool.next_bot().bot_id for _ in range(7)]
    # Strict rotation that wraps around the roster.
    assert seen == ["1", "2", "3", "1", "2", "3", "1"]


def test_pick_channel_round_robins_and_wraps():
    pool = make_pool(telegram_handler(), channels=[-100, -200])
    picked = [pool.pick_channel() for _ in range(5)]
    assert picked == [-100, -200, -100, -200, -100]


def test_get_bot_returns_the_named_bot_and_raises_for_unknown():
    pool = make_pool(telegram_handler(), tokens=["10:a", "20:b"])
    assert pool.get_bot("20").bot_id == "20"
    with pytest.raises(KeyError):
        pool.get_bot("nope")


def test_channels_and_bots_are_defensive_copies():
    pool = make_pool(telegram_handler(), tokens=["1:a"], channels=[-100])
    pool.channels.append(-999)
    pool.bots.clear()
    # Mutating the returned lists must not affect the pool's own state.
    assert pool.channels == [-100]
    assert len(pool.bots) == 1


def test_empty_pool_is_rejected():
    transport = httpx.MockTransport(telegram_handler())
    with pytest.raises(ValueError):
        BotPool([], [-100])
    with pytest.raises(ValueError):
        BotPool([TelegramBot(token="1:a", transport=transport)], [])
