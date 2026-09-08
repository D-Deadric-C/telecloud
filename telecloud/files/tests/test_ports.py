"""Tests for the deletion-enqueue seam (SPEC §6.12, §7.4).

The soft-delete depends on ``jobs/`` (module 14), which is not built yet. These
checks pin the flagged behavior: until an enqueuer is registered, resolving one
raises ``internal_error`` (rather than silently dropping the Telegram-deletion job);
once ``jobs/`` registers one, it is returned.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from telecloud.shared import TeleCloudError

import telecloud.files.ports as ports


@pytest.fixture(autouse=True)
def _reset_registry():
    saved = ports._deletion_enqueuer
    ports._deletion_enqueuer = None
    try:
        yield
    finally:
        ports._deletion_enqueuer = saved


def test_get_deletion_enqueuer_raises_when_unregistered():
    with pytest.raises(TeleCloudError) as excinfo:
        ports.get_deletion_enqueuer()
    assert excinfo.value.code == "internal_error"


def test_set_then_get_returns_registered_enqueuer():
    async def enqueuer(file_id: UUID) -> None:
        pass

    ports.set_deletion_enqueuer(enqueuer)

    assert ports.get_deletion_enqueuer() is enqueuer
