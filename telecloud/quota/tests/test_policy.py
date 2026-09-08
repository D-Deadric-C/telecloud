"""Tests for the pure quota decision (SPEC §3, §6.10).

``evaluate_upload`` is I/O-free, so these exercise the rule directly against the
config limits: the unverified per-file cap (``file_too_large``), the unverified
total cap (``quota_exceeded``), the under-both allow case, and the verified
unlimited case. The limits are imported from ``config`` (never hardcoded here) so
the tests track the canonical values.
"""

from __future__ import annotations

import pytest

from telecloud.config import MAX_FILE_SIZE_UNVERIFIED, QUOTA_UNVERIFIED_BYTES
from telecloud.quota import evaluate_upload
from telecloud.shared import TeleCloudError


# -- unverified: over the per-file cap --------------------------------------
def test_unverified_over_per_file_cap_is_file_too_large():
    with pytest.raises(TeleCloudError) as excinfo:
        evaluate_upload(
            verified=False,
            current_usage=0,
            size_bytes=MAX_FILE_SIZE_UNVERIFIED + 1,
        )

    assert excinfo.value.code == "file_too_large"
    assert excinfo.value.http_status == 413


def test_unverified_per_file_cap_takes_priority_over_total_cap():
    # Both caps would be violated; the per-file check must win.
    with pytest.raises(TeleCloudError) as excinfo:
        evaluate_upload(
            verified=False,
            current_usage=QUOTA_UNVERIFIED_BYTES,
            size_bytes=MAX_FILE_SIZE_UNVERIFIED + 1,
        )

    assert excinfo.value.code == "file_too_large"


# -- unverified: over the total cap -----------------------------------------
def test_unverified_over_total_cap_is_quota_exceeded():
    # A file within the per-file cap, but no room left in the 500 MiB total.
    with pytest.raises(TeleCloudError) as excinfo:
        evaluate_upload(
            verified=False,
            current_usage=QUOTA_UNVERIFIED_BYTES,
            size_bytes=1,
        )

    assert excinfo.value.code == "quota_exceeded"
    assert excinfo.value.http_status == 413


def test_unverified_exactly_at_total_cap_is_allowed():
    # Filling the quota to the brim (==) is allowed; only strictly over rejects.
    remaining = MAX_FILE_SIZE_UNVERIFIED  # a legal per-file size
    evaluate_upload(
        verified=False,
        current_usage=QUOTA_UNVERIFIED_BYTES - remaining,
        size_bytes=remaining,
    )  # no raise


# -- unverified: under both caps --------------------------------------------
def test_unverified_under_both_caps_is_allowed():
    evaluate_upload(verified=False, current_usage=0, size_bytes=1024)  # no raise


def test_unverified_at_per_file_cap_exactly_is_allowed():
    evaluate_upload(
        verified=False,
        current_usage=0,
        size_bytes=MAX_FILE_SIZE_UNVERIFIED,
    )  # no raise


# -- verified: unlimited -----------------------------------------------------
def test_verified_is_unlimited_ignoring_size_and_usage():
    # Far past both unverified caps — a verified user is always allowed.
    evaluate_upload(
        verified=True,
        current_usage=QUOTA_UNVERIFIED_BYTES * 100,
        size_bytes=MAX_FILE_SIZE_UNVERIFIED * 100,
    )  # no raise
