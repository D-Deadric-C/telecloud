"""QStash request-signature verification (SPEC.md §6.14).

Every ``jobs/`` route is callable **only** by QStash. QStash signs each delivery
with a short-lived JWT placed in the ``Upstash-Signature`` header, signed ``HS256``
with the project's signing key. Two keys are kept in ``config`` so a key rotation
never drops a request — verify against the *current* key, then the *next* one
(SPEC §6.1). This module is the single gate: an absent, malformed, wrongly-signed,
expired, or body-mismatched signature raises ``unauthorized`` (SPEC §5.1), so an
arbitrary caller can never run a cleanup job (SPEC §6.14 "Must NOT").

The JWT carries these claims (the ones we check):

* ``iss`` — always the literal ``"Upstash"``.
* ``exp`` / ``nbf`` — validity window (PyJWT enforces these).
* ``body`` — ``base64url(sha256(raw_request_body))`` (no ``=`` padding). Binding
  the signature to a hash of the body is what stops a captured signature from
  being replayed against a *different* payload, so we recompute and compare it.
* ``sub`` — the destination URL. Optional to check here: reverse proxies (Fly.io)
  rewrite host/scheme, so a strict match causes false rejections; callers that can
  reconstruct the exact public URL may pass ``url=`` to enforce it.

This module is pure crypto/claims logic — no FastAPI, no I/O. The ``jobs/`` router
reads the header + raw body and calls :func:`verify_qstash_signature`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Sequence
from typing import Any

import jwt

from telecloud.shared import ErrorCode, TeleCloudError

#: The only algorithm QStash uses to sign delivery JWTs.
ALGORITHM = "HS256"

#: The fixed issuer QStash stamps on every signed request.
QSTASH_ISSUER = "Upstash"


def _unauthorized(message: str) -> TeleCloudError:
    """A 401 ``unauthorized`` error (SPEC §5.1) — the only failure this gate raises."""
    return TeleCloudError.from_code(ErrorCode.UNAUTHORIZED, message)


def _body_hash(body: bytes) -> str:
    """Return ``base64url(sha256(body))`` without padding — QStash's ``body`` claim form."""
    digest = hashlib.sha256(body).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _decode_with_any_key(
    signature: str, signing_keys: Sequence[str], *, leeway: float
) -> dict[str, Any]:
    """Decode ``signature`` against the first key that verifies it; else raise.

    Only a *signature/structure* failure falls through to the next key (that is the
    whole point of keeping current+next keys across a rotation). An expired token is
    a definitive rejection — trying another key cannot make it un-expired — so it is
    re-raised immediately rather than masked as "unknown key".
    """
    last_error: jwt.PyJWTError | None = None
    for key in signing_keys:
        try:
            return jwt.decode(
                signature,
                key,
                algorithms=[ALGORITHM],
                leeway=leeway,
                options={"require": ["exp"], "verify_aud": False},
            )
        except jwt.ExpiredSignatureError as exc:
            raise _unauthorized("QStash signature has expired.") from exc
        except jwt.PyJWTError as exc:
            last_error = exc
            continue
    raise _unauthorized("QStash signature is invalid.") from last_error


def verify_qstash_signature(
    signature: str | None,
    body: bytes,
    signing_keys: Sequence[str],
    *,
    url: str | None = None,
    leeway: float = 0,
) -> dict[str, Any]:
    """Verify a QStash ``Upstash-Signature`` over ``body``; return its claims.

    ``signing_keys`` is the current + next signing key from ``config`` (in that
    order). Raises ``TeleCloudError("unauthorized", 401)`` if the header is missing,
    the JWT fails to verify against either key, it is expired/not-yet-valid, the
    issuer is not ``"Upstash"``, the ``body`` hash does not match ``body``, or (when
    ``url`` is given) the ``sub`` claim does not match it.

    A successful return means the request genuinely came from QStash and carries the
    exact body we received — the caller may proceed to run the job.
    """
    if not signature:
        raise _unauthorized("Missing QStash signature.")
    if not signing_keys:
        # A wiring/config bug, not a caller condition: never silently accept.
        raise _unauthorized("QStash signing keys are not configured.")

    claims = _decode_with_any_key(signature, signing_keys, leeway=leeway)

    if claims.get("iss") != QSTASH_ISSUER:
        raise _unauthorized("QStash signature has an unexpected issuer.")

    expected = _body_hash(body)
    presented = str(claims.get("body", "")).rstrip("=")
    if not hmac.compare_digest(presented, expected):
        raise _unauthorized("QStash signature does not match the request body.")

    if url is not None and claims.get("sub") != url:
        raise _unauthorized("QStash signature was issued for a different URL.")

    return claims
