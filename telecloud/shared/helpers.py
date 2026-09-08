"""Small, pure helper functions shared across modules (SPEC.md §6.2).

Three concerns, all pure (no I/O, no globals beyond the imported constant):

* **Token generation** — URL-safe, cryptographically unguessable strings used
  for email-verification tokens (``users/``) and share tokens (``sharing/``).
* **Human-readable size formatting** — for UI/log strings.
* **Chunk math** — the file-size ⇄ chunk arithmetic that ``storage/`` relies on
  for the two-phase upload (count) and Range downloads (locating a byte). The
  default chunk size is :data:`telecloud.config.CHUNK_SIZE` (18 MiB, SPEC §1).
"""

from __future__ import annotations

import secrets

from telecloud.config import CHUNK_SIZE

# Default byte-entropy for generated tokens. 32 bytes → a 43-char URL-safe
# string (~256 bits), comfortably unguessable for verification + share links.
_DEFAULT_TOKEN_BYTES = 32

# IEC binary unit ladder (1024-based) — TeleCloud talks in MiB everywhere
# (18 MiB chunks, 500 MiB quota), so size formatting uses binary units too.
_BINARY_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")


def generate_token(num_bytes: int = _DEFAULT_TOKEN_BYTES) -> str:
    """Return a URL-safe, unguessable random token.

    Backed by :func:`secrets.token_urlsafe`, so the result is suitable for
    security-sensitive use (verification + share tokens). ``num_bytes`` is the
    entropy in bytes; the returned string is longer due to base64url encoding.

    Raises :class:`ValueError` if ``num_bytes`` is not positive.
    """
    if num_bytes <= 0:
        raise ValueError("num_bytes must be a positive integer")
    return secrets.token_urlsafe(num_bytes)


def format_size(num_bytes: int) -> str:
    """Format a byte count as a human-readable binary-unit string.

    Uses IEC units (``B``, ``KiB``, ``MiB`` …). Whole bytes are shown without a
    decimal point; larger units use one decimal place
    (e.g. ``1536`` → ``"1.5 KiB"``, ``18 * 1024 * 1024`` → ``"18.0 MiB"``).

    Raises :class:`ValueError` if ``num_bytes`` is negative.
    """
    if num_bytes < 0:
        raise ValueError("num_bytes must not be negative")
    if num_bytes < 1024:
        return f"{num_bytes} B"

    value = float(num_bytes)
    for unit in _BINARY_UNITS[1:]:
        value /= 1024.0
        if value < 1024.0:
            return f"{value:.1f} {unit}"
    # Past the largest named unit: keep reporting in the last one (EiB).
    return f"{value:.1f} {_BINARY_UNITS[-1]}"


def compute_chunk_count(total_size: int, chunk_size: int = CHUNK_SIZE) -> int:
    """Return how many chunks ``total_size`` bytes split into.

    Ceiling division: a partial final chunk counts as one. A zero-byte file has
    zero chunks. (Used when creating the ``files`` row in the two-phase upload,
    SPEC §7.1.)

    Raises :class:`ValueError` for a negative ``total_size`` or a non-positive
    ``chunk_size``.
    """
    if total_size < 0:
        raise ValueError("total_size must not be negative")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    # -(-a // b) is integer ceiling division without floating point.
    return -(-total_size // chunk_size)


def locate_byte(offset: int, chunk_size: int = CHUNK_SIZE) -> tuple[int, int]:
    """Map an absolute byte ``offset`` to ``(chunk_index, intra_chunk_offset)``.

    Given a Range request's start byte, returns which 0-based chunk it falls in
    and the offset within that chunk. Streaming then begins at ``chunk_index``
    and skips ``intra_chunk_offset`` bytes (SPEC §7.2).

    Raises :class:`ValueError` for a negative ``offset`` or a non-positive
    ``chunk_size``.
    """
    if offset < 0:
        raise ValueError("offset must not be negative")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    return divmod(offset, chunk_size)
