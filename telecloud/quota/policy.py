"""The pure quota decision (SPEC.md §3, §6.10).

This is the *rule*, with no I/O: given a user's verification state and current
usage, decide whether a new upload of ``size_bytes`` is allowed. The numbers are
**not** hardcoded here — they are referenced from ``config`` (SPEC §3 keeps the
limits in one place), so this module never drifts from the canonical values.

The two tiers (SPEC §3):

============  ==============  ==============
State         Total quota     Max file size
============  ==============  ==============
Unverified    500 MiB         30 MiB
Verified      Unlimited       No cap
============  ==============  ==============

Keeping the decision pure means it is trivially unit-testable without a database;
``service.py`` wires it to the live profile (verification flag + usage).
"""

from __future__ import annotations

from telecloud.config import MAX_FILE_SIZE_UNVERIFIED, QUOTA_UNVERIFIED_BYTES
from telecloud.shared import ErrorCode, TeleCloudError, format_size


def evaluate_upload(
    *,
    verified: bool,
    current_usage: int,
    size_bytes: int,
) -> None:
    """Raise if an upload of ``size_bytes`` is not permitted; return otherwise.

    * **Verified** users have no per-file cap and no total cap — always allowed
      (the ``*_VERIFIED`` config sentinels are ``None`` for "unlimited", SPEC §3),
      so usage is not even consulted.
    * **Unverified** users are rejected with ``file_too_large`` when the single
      file exceeds :data:`config.MAX_FILE_SIZE_UNVERIFIED` (30 MiB), and with
      ``quota_exceeded`` when the upload would push total usage past
      :data:`config.QUOTA_UNVERIFIED_BYTES` (500 MiB).

    The per-file cap is checked before the total cap so an oversized file is
    reported as ``file_too_large`` regardless of how full the account is.
    """
    if verified:
        return

    if size_bytes > MAX_FILE_SIZE_UNVERIFIED:
        raise TeleCloudError.from_code(
            ErrorCode.FILE_TOO_LARGE,
            f"File is {format_size(size_bytes)}; unverified accounts are limited "
            f"to {format_size(MAX_FILE_SIZE_UNVERIFIED)} per file. "
            "Verify your email to lift this limit.",
        )

    if current_usage + size_bytes > QUOTA_UNVERIFIED_BYTES:
        raise TeleCloudError.from_code(
            ErrorCode.QUOTA_EXCEEDED,
            f"Upload would exceed the {format_size(QUOTA_UNVERIFIED_BYTES)} "
            "storage limit for unverified accounts. "
            "Verify your email for unlimited storage.",
        )
