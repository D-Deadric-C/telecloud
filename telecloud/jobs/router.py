"""FastAPI routes QStash calls to run the cleanup jobs (SPEC.md §6.14, §7.4).

Two endpoints, one per job (SPEC §7.4):

* ``POST /jobs/sweep-orphans``  → :func:`jobs.service.sweep_orphans`
* ``POST /jobs/deferred-delete`` → :func:`jobs.service.delete_deferred`

**Both are gated by QStash signature verification** (the :func:`require_qstash`
dependency). The gate runs *before* any handler body, so an unsigned or invalid
request gets ``401 unauthorized`` (rendered to the §5.1 envelope by ``middleware/``)
and never touches the database or Telegram — the jobs are not runnable by arbitrary
callers (SPEC §6.14 "Must NOT"). Verifying the signature requires the **raw**
request body (QStash signs a hash of it), so the dependency reads
``await request.body()`` itself; the handlers take no body.

Signing keys come from ``config`` (current + next, SPEC §6.1) via the
:func:`signing_keys` dependency, which tests override through
``app.dependency_overrides`` to inject known keys without real config or a live
QStash.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, Depends, Request

from telecloud.config import get_settings

from telecloud.jobs import service
from telecloud.jobs.signature import verify_qstash_signature

router = APIRouter(prefix="/jobs", tags=["jobs"])

#: The header QStash puts its signature JWT in.
SIGNATURE_HEADER = "Upstash-Signature"


def signing_keys() -> Sequence[str]:
    """Current + next QStash signing keys from ``config`` (SPEC §6.1).

    A dependency so tests can override the key source via
    ``app.dependency_overrides[signing_keys]`` without touching real config.
    """
    settings = get_settings()
    return (settings.qstash_current_signing_key, settings.qstash_next_signing_key)


async def require_qstash(
    request: Request, keys: Sequence[str] = Depends(signing_keys)
) -> None:
    """Admit only validly-signed QStash requests; raise ``unauthorized`` otherwise.

    Reads the ``Upstash-Signature`` header and the raw body, then delegates to
    :func:`verify_qstash_signature`. A missing/forged/expired signature, a wrong
    issuer, or a body that doesn't match the signed hash all raise
    ``TeleCloudError("unauthorized", 401)`` before the route runs (SPEC §5.1, §6.14).
    """
    signature = request.headers.get(SIGNATURE_HEADER)
    body = await request.body()
    verify_qstash_signature(signature, body, keys)


@router.post("/sweep-orphans")
async def sweep_orphans(_: None = Depends(require_qstash)) -> dict[str, int]:
    """Run the orphan sweep (QStash-triggered, SPEC §7.4).

    Reclaims abandoned ``pending`` uploads: deletes their Telegram messages and
    rows. Returns a small JSON summary of what the bounded run did.
    """
    result = await service.sweep_orphans()
    return result.as_dict()


@router.post("/deferred-delete")
async def deferred_delete(_: None = Depends(require_qstash)) -> dict[str, int]:
    """Run the deferred-delete job (QStash-triggered, SPEC §7.4).

    Finishes soft-deleted files (``deleting`` status): deletes their Telegram
    messages and rows. Quota is **not** touched (already decremented in ``files/``,
    SPEC §6.14). Returns a small JSON summary of the bounded run.
    """
    result = await service.delete_deferred()
    return result.as_dict()
