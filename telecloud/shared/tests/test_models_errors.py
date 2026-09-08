"""Light tests for the error type and shared read models (SPEC §5.1, §4)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from telecloud.shared import (
    ChunkStatus,
    ErrorCode,
    FileMeta,
    FileStatus,
    TeleCloudError,
    UserContext,
)


def test_error_to_dict_matches_spec_envelope():
    err = TeleCloudError(ErrorCode.NOT_FOUND, "No such file", 404)
    assert err.code == "not_found"
    assert err.http_status == 404
    assert err.to_dict() == {"error": {"code": "not_found", "message": "No such file"}}


def test_error_from_code_uses_default_status():
    err = TeleCloudError.from_code(ErrorCode.RATE_LIMITED)
    assert err.http_status == 429
    assert err.code == "rate_limited"
    assert err.message == "rate_limited"


def test_error_accepts_plain_string_code():
    err = TeleCloudError("custom_code", "boom", 400)
    assert err.code == "custom_code"


def test_error_is_raisable():
    with pytest.raises(TeleCloudError) as exc_info:
        raise TeleCloudError.from_code(ErrorCode.FORBIDDEN, "nope")
    assert exc_info.value.http_status == 403


def test_filemeta_from_attributes_and_frozen():
    now = datetime.now(timezone.utc)

    class Row:  # stand-in for a DB record (attribute access)
        id = uuid4()
        owner_id = uuid4()
        folder_id = None
        name = "report.pdf"
        size_bytes = 123
        mime_type = "application/pdf"
        chunk_count = 1
        status = "committed"
        created_at = now
        deleted_at = None

    meta = FileMeta.model_validate(Row())
    assert meta.name == "report.pdf"
    assert meta.status is FileStatus.COMMITTED

    with pytest.raises(Exception):  # frozen → assignment forbidden
        meta.name = "other.pdf"  # type: ignore[misc]


def test_usercontext_minimal():
    user = UserContext(id=uuid4(), email="a@b.com", email_verified=True)
    assert user.email_verified is True


def test_status_enums_are_strings():
    assert FileStatus.PENDING == "pending"
    assert ChunkStatus.COMMITTED == "committed"
