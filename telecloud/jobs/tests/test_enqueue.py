"""Tests for the deferred-deletion enqueue helper (SPEC §6.14, §6.12).

``jobs.register`` must wire a concrete enqueuer into ``files.ports`` so ``files/``'s
soft-delete can hand off without importing ``jobs/``. The default (sweep-backed)
enqueuer must satisfy the ``files.ports.DeletionEnqueuer`` contract and never raise.
An optional publisher, when supplied, is used to trigger on demand.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from telecloud.files import ports as files_ports

from telecloud.jobs.enqueue import make_enqueuer, register

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_registration():
    """Clear the files/ registration around each test (module-global state)."""
    files_ports._deletion_enqueuer = None
    yield
    files_ports._deletion_enqueuer = None


async def test_register_wires_files_ports():
    register()
    enqueuer = files_ports.get_deletion_enqueuer()
    # The registered enqueuer satisfies the port: async (file_id) -> None.
    assert await enqueuer(uuid4()) is None


async def test_default_enqueuer_never_raises():
    # The sweep is the backstop, so the default must not fail a soft-delete.
    enqueuer = make_enqueuer()
    assert await enqueuer(uuid4()) is None


async def test_publisher_is_used_when_supplied():
    published: list = []

    class FakePublisher:
        async def publish_deferred_delete(self, file_id):
            published.append(file_id)

    enqueuer = make_enqueuer(FakePublisher())
    file_id = uuid4()
    await enqueuer(file_id)

    assert published == [file_id]


async def test_publisher_failure_falls_back_to_sweep():
    # A failed on-demand publish must not strand the delete (sweep reclaims it).
    class ExplodingPublisher:
        async def publish_deferred_delete(self, file_id):
            raise RuntimeError("qstash down")

    enqueuer = make_enqueuer(ExplodingPublisher())
    assert await enqueuer(uuid4()) is None  # swallowed
