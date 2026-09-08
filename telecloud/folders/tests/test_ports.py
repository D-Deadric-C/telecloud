"""Tests for the file-deletion seam (SPEC §6.11, §6.12).

The cascade depends on ``files/`` (module 13), which is not built yet. These
checks pin the flagged behavior: until a deleter is registered, resolving one
raises ``internal_error`` (rather than silently orphaning files); once ``files/``
registers one, it is returned.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from telecloud.shared import TeleCloudError, UserContext

import telecloud.folders.ports as ports


@pytest.fixture(autouse=True)
def _reset_registry():
    saved = ports._file_deleter
    ports._file_deleter = None
    try:
        yield
    finally:
        ports._file_deleter = saved


def test_get_file_deleter_raises_when_unregistered():
    with pytest.raises(TeleCloudError) as excinfo:
        ports.get_file_deleter()
    assert excinfo.value.code == "internal_error"


def test_set_then_get_returns_registered_deleter():
    async def deleter(user: UserContext, file_id: UUID, *, access_token: str) -> None:
        pass

    ports.set_file_deleter(deleter)

    assert ports.get_file_deleter() is deleter
