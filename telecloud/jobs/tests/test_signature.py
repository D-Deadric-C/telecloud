"""Tests for QStash signature verification (SPEC §6.14).

The gate that keeps the job routes un-runnable by arbitrary callers. Covers the
accept path (valid JWT over the exact body), current+next key rotation, and every
rejection: missing header, wrong key, tampered body, wrong issuer, expired token,
and the optional URL check.
"""

from __future__ import annotations

import base64
import hashlib
import time

import jwt
import pytest

from telecloud.shared import ErrorCode, TeleCloudError
from telecloud.jobs.signature import QSTASH_ISSUER, verify_qstash_signature

# 32+ byte keys so PyJWT's HS256 minimum-length advisory stays quiet.
CURRENT_KEY = "sig_current_" + "c" * 32
NEXT_KEY = "sig_next_" + "n" * 32
KEYS = (CURRENT_KEY, NEXT_KEY)


def _body_hash(body: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=").decode()


def _sign(
    body: bytes,
    *,
    key: str = CURRENT_KEY,
    iss: str = QSTASH_ISSUER,
    sub: str = "https://telecloud.fly.dev/jobs/sweep-orphans",
    body_claim: str | None = None,
    exp_offset: int = 300,
) -> str:
    """Mint a QStash-style signature JWT over ``body``."""
    now = int(time.time())
    claims = {
        "iss": iss,
        "sub": sub,
        "iat": now,
        "nbf": now,
        "exp": now + exp_offset,
        "jti": "nonce-123",
        "body": body_claim if body_claim is not None else _body_hash(body),
    }
    return jwt.encode(claims, key, algorithm="HS256")


def _assert_unauthorized(exc_info) -> None:
    assert isinstance(exc_info.value, TeleCloudError)
    assert exc_info.value.code == ErrorCode.UNAUTHORIZED.value
    assert exc_info.value.http_status == 401


def test_valid_signature_with_current_key_passes():
    body = b'{"job": "sweep"}'
    claims = verify_qstash_signature(_sign(body), body, KEYS)
    assert claims["iss"] == QSTASH_ISSUER


def test_valid_signature_with_next_key_passes():
    # Key rotation: QStash may sign with the next key; verify must still accept it.
    body = b"payload"
    claims = verify_qstash_signature(_sign(body, key=NEXT_KEY), body, KEYS)
    assert claims["body"] == _body_hash(body)


def test_missing_signature_is_rejected():
    with pytest.raises(TeleCloudError) as exc:
        verify_qstash_signature(None, b"body", KEYS)
    _assert_unauthorized(exc)


def test_signature_from_unknown_key_is_rejected():
    body = b"body"
    forged = _sign(body, key="sig_attacker_" + "x" * 32)
    with pytest.raises(TeleCloudError) as exc:
        verify_qstash_signature(forged, body, KEYS)
    _assert_unauthorized(exc)


def test_body_tampering_is_rejected():
    # Signature is valid, but the body delivered differs from the signed hash.
    signed = _sign(b"original body")
    with pytest.raises(TeleCloudError) as exc:
        verify_qstash_signature(signed, b"tampered body", KEYS)
    _assert_unauthorized(exc)


def test_wrong_issuer_is_rejected():
    body = b"body"
    with pytest.raises(TeleCloudError) as exc:
        verify_qstash_signature(_sign(body, iss="Attacker"), body, KEYS)
    _assert_unauthorized(exc)


def test_expired_signature_is_rejected():
    body = b"body"
    expired = _sign(body, exp_offset=-10)
    with pytest.raises(TeleCloudError) as exc:
        verify_qstash_signature(expired, body, KEYS)
    _assert_unauthorized(exc)


def test_url_check_enforced_when_requested():
    body = b"body"
    signed = _sign(body, sub="https://telecloud.fly.dev/jobs/sweep-orphans")

    # Matching URL passes.
    verify_qstash_signature(
        signed, body, KEYS, url="https://telecloud.fly.dev/jobs/sweep-orphans"
    )
    # A different URL is rejected (replay against another endpoint).
    with pytest.raises(TeleCloudError) as exc:
        verify_qstash_signature(
            signed, body, KEYS, url="https://telecloud.fly.dev/jobs/deferred-delete"
        )
    _assert_unauthorized(exc)


def test_no_signing_keys_configured_is_rejected():
    body = b"body"
    with pytest.raises(TeleCloudError) as exc:
        verify_qstash_signature(_sign(body), body, ())
    _assert_unauthorized(exc)
