"""Unit tests for the pure helpers in ``telecloud.shared.helpers`` (SPEC §6.2).

Focus per the build brief: the chunk-math and token helpers. ``format_size`` is
covered lightly too since it's a one-liner of behavior.
"""

from __future__ import annotations

import pytest

from telecloud.config import CHUNK_SIZE
from telecloud.shared import (
    compute_chunk_count,
    format_size,
    generate_token,
    locate_byte,
)

# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------

_URL_SAFE = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def test_token_is_url_safe():
    token = generate_token()
    assert token  # non-empty
    assert set(token) <= _URL_SAFE  # only base64url alphabet, no padding "="


def test_tokens_are_unguessable_and_unique():
    # A large batch should be entirely distinct (collision would be astronomical).
    tokens = {generate_token() for _ in range(1000)}
    assert len(tokens) == 1000


def test_more_entropy_yields_longer_token():
    short = generate_token(8)
    long = generate_token(64)
    assert len(long) > len(short)


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_token_rejects_non_positive_entropy(bad):
    with pytest.raises(ValueError):
        generate_token(bad)


# ---------------------------------------------------------------------------
# compute_chunk_count — ceiling division
# ---------------------------------------------------------------------------


def test_chunk_count_zero_bytes_is_zero():
    assert compute_chunk_count(0) == 0


@pytest.mark.parametrize(
    "size, expected",
    [
        (1, 1),
        (CHUNK_SIZE - 1, 1),
        (CHUNK_SIZE, 1),
        (CHUNK_SIZE + 1, 2),
        (2 * CHUNK_SIZE, 2),
        (2 * CHUNK_SIZE + 1, 3),
    ],
)
def test_chunk_count_default_chunk_size(size, expected):
    assert compute_chunk_count(size) == expected


def test_chunk_count_custom_chunk_size():
    assert compute_chunk_count(10, chunk_size=4) == 3  # 4 + 4 + 2


def test_chunk_count_rejects_negative_size():
    with pytest.raises(ValueError):
        compute_chunk_count(-1)


@pytest.mark.parametrize("bad", [0, -1])
def test_chunk_count_rejects_bad_chunk_size(bad):
    with pytest.raises(ValueError):
        compute_chunk_count(100, chunk_size=bad)


# ---------------------------------------------------------------------------
# locate_byte — (chunk_index, intra_chunk_offset) for Range downloads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "offset, expected",
    [
        (0, (0, 0)),
        (1, (0, 1)),
        (CHUNK_SIZE - 1, (0, CHUNK_SIZE - 1)),
        (CHUNK_SIZE, (1, 0)),
        (CHUNK_SIZE + 5, (1, 5)),
        (3 * CHUNK_SIZE + 7, (3, 7)),
    ],
)
def test_locate_byte_default_chunk_size(offset, expected):
    assert locate_byte(offset) == expected


def test_locate_byte_custom_chunk_size():
    assert locate_byte(10, chunk_size=4) == (2, 2)  # byte 10 → chunk 2, offset 2


def test_locate_byte_round_trips_with_chunk_count():
    # The last byte of a file lives in the last chunk (index = count - 1).
    size = 2 * CHUNK_SIZE + 123
    last_index, _ = locate_byte(size - 1)
    assert last_index == compute_chunk_count(size) - 1


def test_locate_byte_rejects_negative_offset():
    with pytest.raises(ValueError):
        locate_byte(-1)


@pytest.mark.parametrize("bad", [0, -1])
def test_locate_byte_rejects_bad_chunk_size(bad):
    with pytest.raises(ValueError):
        locate_byte(100, chunk_size=bad)


# ---------------------------------------------------------------------------
# format_size — light coverage (binary units)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "num_bytes, expected",
    [
        (0, "0 B"),
        (512, "512 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (18 * 1024 * 1024, "18.0 MiB"),
        (500 * 1024 * 1024, "500.0 MiB"),
        (1024**3, "1.0 GiB"),
    ],
)
def test_format_size(num_bytes, expected):
    assert format_size(num_bytes) == expected


def test_format_size_rejects_negative():
    with pytest.raises(ValueError):
        format_size(-1)
