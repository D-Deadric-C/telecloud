"""Quota enforcement + usage accounting (SPEC.md §3, §6.10).

This module is **pure policy + bookkeeping**: it decides whether an upload is
allowed and keeps ``profiles.storage_used_bytes`` in step on commit / delete. It
never moves bytes, never touches Telegram, and never builds HTTP responses — the
``files/`` orchestrator calls in here before handing off to ``storage/`` (SPEC
§7.1).

Three functions form the public surface:

* :func:`check_can_upload` — the pre-upload gate (reject early per SPEC §3).
* :func:`add_usage` — increment usage transactionally on commit.
* :func:`subtract_usage` — decrement usage on delete, never going negative.

Reads. The verification flag is read **via** ``users.get_profile`` (SPEC lists
``users`` as a dependency precisely so quota doesn't re-derive verification), and
current usage is read **via** the ``database`` profiles repo. Both are read fresh
off the profile rather than trusted from the caller's (possibly stale) token —
SPEC §3 says enforce against the profile.

Writes. Usage mutations go through ``profiles_repo.adjust_storage_used``, which is
backed by an atomic SQL ``UPDATE`` (migration 0003). That primitive performs the
*mutation* only; the **business rules** layered on top — the no-negative floor —
live here, as the migration's own comment and SPEC §6.3 require.

NOTE (contract): SPEC §6.10 writes these as ``check_can_upload(user, size)`` etc.
RLS scopes every profile read/write to ``auth.uid()``, which needs the caller's
JWT, so — exactly like ``database.get_db`` and ``users.get_profile`` — each
function also takes the request's ``access_token``. See ``quota/README.md``
"Contract notes".
"""

from __future__ import annotations

from telecloud.database import get_db, profiles_repo
from telecloud.quota.policy import evaluate_upload
from telecloud.shared import ErrorCode, TeleCloudError, UserContext
from telecloud.users import get_profile


async def check_can_upload(
    user: UserContext,
    size_bytes: int,
    *,
    access_token: str,
) -> None:
    """Reject (raise) or allow (return) an upload of ``size_bytes`` (SPEC §3, §7.1).

    Verified users are unconditionally allowed (no per-file or total cap), so only
    their verification flag is read. Unverified users are checked against the
    per-file cap first (``file_too_large``) and then the total-quota cap
    (``quota_exceeded``); the latter needs their current usage, read fresh from the
    profile.

    Reads the **fresh** verification flag via ``users.get_profile`` rather than
    trusting ``user.email_verified`` from the token, so a just-verified user isn't
    held to the unverified limits (SPEC §3 enforces against the profile).

    :raises TeleCloudError: ``file_too_large`` or ``quota_exceeded`` when blocked;
        ``not_found`` (from ``get_profile``) if the profile row is absent.
    """
    profile = await get_profile(user, access_token=access_token)

    # Verified ⇒ unlimited; skip the usage read entirely (SPEC §3).
    if profile.email_verified:
        evaluate_upload(verified=True, current_usage=0, size_bytes=size_bytes)
        return

    current_usage = await _read_usage(user, access_token=access_token)
    evaluate_upload(
        verified=False,
        current_usage=current_usage,
        size_bytes=size_bytes,
    )


async def add_usage(
    user: UserContext,
    delta: int,
    *,
    access_token: str,
) -> int:
    """Add ``delta`` bytes to the user's usage; return the new total (SPEC §7.1).

    Called on a committed upload. The mutation is a single atomic SQL ``UPDATE``
    (``adjust_storage_used``). ``delta`` is a magnitude and must be non-negative —
    use :func:`subtract_usage` to decrease usage.

    :raises ValueError: if ``delta`` is negative.
    :raises TeleCloudError: ``not_found`` if the profile row is absent.
    """
    _require_non_negative(delta)
    if delta == 0:
        return await _read_usage(user, access_token=access_token)

    db = await get_db(access_token)
    try:
        new_value = await profiles_repo.adjust_storage_used(db, user.id, delta)
    finally:
        await db.aclose()
    return _require_profile(new_value)


async def subtract_usage(
    user: UserContext,
    delta: int,
    *,
    access_token: str,
) -> int:
    """Subtract ``delta`` bytes from usage; return the new total (SPEC §7.4).

    Called on a file deletion. ``delta`` is a magnitude and must be non-negative.
    Usage is **never driven below zero** (SPEC §6.10): the atomic decrement is
    applied, and if accounting drift would have produced a negative balance it is
    snapped back up to ``0`` with a compensating atomic update. (The clamp lives
    here, not in SQL: the ``adjust_storage_used`` primitive only mutates; the
    no-negative rule is quota's, per migration 0003 / SPEC §6.3.)

    :raises ValueError: if ``delta`` is negative.
    :raises TeleCloudError: ``not_found`` if the profile row is absent.
    """
    _require_non_negative(delta)
    if delta == 0:
        return await _read_usage(user, access_token=access_token)

    db = await get_db(access_token)
    try:
        new_value = await profiles_repo.adjust_storage_used(db, user.id, -delta)
        new_value = _require_profile(new_value)
        if new_value < 0:
            # Floor at zero: add back exactly the observed deficit. Atomic, so it
            # corrects the underflow we just saw without a separate read.
            new_value = _require_profile(
                await profiles_repo.adjust_storage_used(db, user.id, -new_value)
            )
    finally:
        await db.aclose()
    return new_value


# -- internals --------------------------------------------------------------


async def _read_usage(user: UserContext, *, access_token: str) -> int:
    """Return the user's current ``storage_used_bytes`` (SPEC §4.1).

    Read via the RLS-scoped client so a user only ever reads their own row. A
    missing profile is treated as ``not_found`` — quota is never asked about a
    user the system doesn't know.
    """
    db = await get_db(access_token)
    try:
        usage = await profiles_repo.get_storage_used(db, user.id)
    finally:
        await db.aclose()
    return _require_profile(usage)


def _require_non_negative(delta: int) -> None:
    if delta < 0:
        raise ValueError(
            "delta must be a non-negative magnitude; "
            "use subtract_usage to decrease usage"
        )


def _require_profile(value: int | None) -> int:
    """Turn a missing-profile ``None`` from the repo into a clean ``not_found``."""
    if value is None:
        raise TeleCloudError.from_code(ErrorCode.NOT_FOUND, "Profile not found.")
    return value
