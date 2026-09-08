"""``telecloud.jobs`` — async cleanup via QStash (SPEC.md §6.14, §7.4).

QStash-triggered cleanup that keeps storage consistent without blocking requests:

* **Orphan sweep** — reclaims abandoned ``pending`` uploads (a two-phase commit
  that never finished): deletes their Telegram messages, then the chunk + file
  rows (SPEC §7.1 step 6, §7.4).
* **Deferred delete** — finishes soft-deletes: deletes the Telegram messages for
  files in ``deleting`` (quota was already decremented in ``files/``), then removes
  the rows (SPEC §6.12, §7.4). Quota is **not** re-touched here.

Both jobs are idempotent and process a **bounded** batch per run; transient
Telegram failures are retried via ``rate_limit.queue`` and dead-lettered after
repeated failure instead of looping forever (SPEC §6.6).

Public surface:

* :data:`router` — the two QStash job endpoints, each gated by signature
  verification (a request without a valid QStash signature is rejected before any
  work happens — SPEC §6.14 "Must NOT").
* :func:`register` — register the deferred-deletion enqueuer with ``files/`` at app
  composition (the public enqueue API ``files/`` depends on, SPEC §6.12).
* :func:`verify_qstash_signature` — the signature gate, exposed for wiring/tests.
* :func:`sweep_orphans` / :func:`delete_deferred` / :func:`process_retries` — the
  job functions, exposed for scheduling and tests.

Dependencies are the §6.14 set: ``config``, ``shared``, ``database``, ``telegram``,
``rate_limit`` (and ``files.ports`` for the registration seam, mirroring how
``folders/`` inverts its dependency on ``files/``). It never reaches into bot or
Redis internals.
"""

from telecloud.jobs.enqueue import make_enqueuer, register
from telecloud.jobs.router import require_qstash, router, signing_keys
from telecloud.jobs.service import (
    CleanupResult,
    delete_deferred,
    process_retries,
    sweep_orphans,
)
from telecloud.jobs.signature import verify_qstash_signature

__all__ = [
    # HTTP (SPEC §6.14 public: the job endpoints)
    "router",
    "require_qstash",
    "signing_keys",
    # enqueue helpers (SPEC §6.14 public: what files/ uses)
    "register",
    "make_enqueuer",
    # jobs + retry drain
    "sweep_orphans",
    "delete_deferred",
    "process_retries",
    "CleanupResult",
    # signature gate
    "verify_qstash_signature",
]
