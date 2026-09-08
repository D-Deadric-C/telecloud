"""``telecloud.quota`` — verification-based quota enforcement (SPEC.md §3, §6.10).

Pure **policy + usage accounting**. It decides whether an upload may proceed and
keeps ``profiles.storage_used_bytes`` accurate on commit / delete. It never moves
bytes, never talks to Telegram, and never builds HTTP responses — ``files/`` calls
it before delegating the actual transfer to ``storage/`` (SPEC §7.1).

Public surface (what other modules import):

* :func:`check_can_upload` — reject early (``file_too_large`` / ``quota_exceeded``)
  or allow, per the §3 tiers (unverified: 30 MiB/file, 500 MiB total; verified:
  unlimited).
* :func:`add_usage` — transactional increment on commit.
* :func:`subtract_usage` — transactional decrement on delete, floored at zero.

Also exported for unit tests: :func:`evaluate_upload`, the pure decision.

Dependencies are exactly the §6.10 set: ``config`` (the limits), ``shared``
(errors/models/helpers), ``database`` (the profiles repo), and ``users`` (the
verification flag). Verification is **read via** ``users`` — quota does not
reimplement the handshake (SPEC §6.10).
"""

from telecloud.quota.policy import evaluate_upload
from telecloud.quota.service import add_usage, check_can_upload, subtract_usage

__all__ = [
    "check_can_upload",
    "add_usage",
    "subtract_usage",
    "evaluate_upload",
]
