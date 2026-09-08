"""Tests for JWT verify/issue against the Supabase secret (SPEC §6.4).

Pure crypto/claims logic — no Supabase, no network. We sign tokens with an
explicit secret (``encode_token``) and assert ``verify_token`` accepts the good
ones and rejects the bad ones as ``TeleCloudError("unauthorized", 401)``.
"""

from __future__ import annotations

import time
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from telecloud.shared import TeleCloudError

import telecloud.auth.tokens as tokens_mod
from telecloud.auth.tokens import (
    SUPABASE_AUDIENCE,
    encode_token,
    user_id_from_claims,
    verify_supabase_token,
    verify_token,
)

SECRET = "unit-test-secret-padded-to-32-bytes-min"


def _assert_unauthorized(excinfo: pytest.ExceptionInfo[TeleCloudError]) -> None:
    assert excinfo.value.code == "unauthorized"
    assert excinfo.value.http_status == 401


# -- happy path -------------------------------------------------------------
def test_encode_then_verify_roundtrips_claims():
    uid = uuid4()
    token = encode_token({"sub": str(uid), "email": "a@b.com"}, secret=SECRET)
    claims = verify_token(token, secret=SECRET)
    assert claims["sub"] == str(uid)
    assert claims["email"] == "a@b.com"
    assert claims["aud"] == SUPABASE_AUDIENCE
    assert user_id_from_claims(claims) == uid


def test_verify_uses_config_secret_by_default(monkeypatch: pytest.MonkeyPatch):
    class _FakeSettings:
        supabase_jwt_secret = SECRET

    monkeypatch.setattr(tokens_mod, "get_settings", lambda: _FakeSettings())
    token = encode_token({"sub": str(uuid4())})  # encode also defaults to config
    claims = verify_token(token)  # no explicit secret -> pulled from config
    assert claims["aud"] == SUPABASE_AUDIENCE


# -- rejection paths --------------------------------------------------------
def test_verify_rejects_expired_token():
    token = encode_token(
        {"sub": str(uuid4()), "exp": int(time.time()) - 10}, secret=SECRET
    )
    with pytest.raises(TeleCloudError) as excinfo:
        verify_token(token, secret=SECRET)
    _assert_unauthorized(excinfo)


def test_verify_rejects_bad_signature():
    token = encode_token({"sub": str(uuid4())}, secret=SECRET)
    with pytest.raises(TeleCloudError) as excinfo:
        verify_token(token, secret="a-different-secret-also-32-bytes-long!")
    _assert_unauthorized(excinfo)


def test_verify_rejects_wrong_audience():
    token = encode_token({"sub": str(uuid4())}, secret=SECRET, audience="someone-else")
    with pytest.raises(TeleCloudError) as excinfo:
        verify_token(token, secret=SECRET)  # expects "authenticated"
    _assert_unauthorized(excinfo)


def test_verify_rejects_missing_required_claim():
    # No ``sub`` -> the require list rejects it.
    token = jwt.encode(
        {"aud": SUPABASE_AUDIENCE, "exp": int(time.time()) + 60},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(TeleCloudError) as excinfo:
        verify_token(token, secret=SECRET)
    _assert_unauthorized(excinfo)


def test_verify_rejects_garbage():
    with pytest.raises(TeleCloudError) as excinfo:
        verify_token("not.a.jwt", secret=SECRET)
    _assert_unauthorized(excinfo)


# -- user_id_from_claims ----------------------------------------------------
def test_user_id_from_claims_rejects_missing_sub():
    with pytest.raises(TeleCloudError) as excinfo:
        user_id_from_claims({"email": "a@b.com"})
    _assert_unauthorized(excinfo)


def test_user_id_from_claims_rejects_non_uuid_sub():
    with pytest.raises(TeleCloudError) as excinfo:
        user_id_from_claims({"sub": "not-a-uuid"})
    _assert_unauthorized(excinfo)


# -- verify_supabase_token (alg-aware) --------------------------------------
class _FakeJWK:
    """Minimal stand-in for ``jwt.PyJWK`` exposing the public key under ``.key``."""

    def __init__(self, key) -> None:
        self.key = key


def _es256_keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return priv_pem, priv.public_key()


@pytest.mark.asyncio
async def test_verify_supabase_token_hs256_path():
    token = encode_token({"sub": str(uuid4())}, secret=SECRET)
    claims = await verify_supabase_token(token, hs_secret=SECRET)
    assert claims["aud"] == SUPABASE_AUDIENCE


@pytest.mark.asyncio
async def test_verify_supabase_token_es256_via_jwks(monkeypatch: pytest.MonkeyPatch):
    priv_pem, public_key = _es256_keypair()
    uid = uuid4()
    token = jwt.encode(
        {"sub": str(uid), "aud": SUPABASE_AUDIENCE, "exp": int(time.time()) + 60},
        priv_pem,
        algorithm="ES256",
        headers={"kid": "test-kid"},
    )

    async def fake_get_signing_key(kid, *, jwks_url):
        assert kid == "test-kid"
        return _FakeJWK(public_key)

    monkeypatch.setattr(tokens_mod, "_get_signing_key", fake_get_signing_key)
    claims = await verify_supabase_token(token, jwks_url="https://x/jwks")
    assert user_id_from_claims(claims) == uid


@pytest.mark.asyncio
async def test_verify_supabase_token_es256_rejects_wrong_key(
    monkeypatch: pytest.MonkeyPatch,
):
    priv_pem, _ = _es256_keypair()
    _, other_public = _es256_keypair()  # a different key than the signer
    token = jwt.encode(
        {"sub": str(uuid4()), "aud": SUPABASE_AUDIENCE, "exp": int(time.time()) + 60},
        priv_pem,
        algorithm="ES256",
        headers={"kid": "test-kid"},
    )

    async def fake_get_signing_key(kid, *, jwks_url):
        return _FakeJWK(other_public)

    monkeypatch.setattr(tokens_mod, "_get_signing_key", fake_get_signing_key)
    with pytest.raises(TeleCloudError) as excinfo:
        await verify_supabase_token(token, jwks_url="https://x/jwks")
    _assert_unauthorized(excinfo)


@pytest.mark.asyncio
async def test_verify_supabase_token_rejects_unsupported_alg():
    # "none" alg token — must never be accepted.
    token = jwt.encode({"sub": str(uuid4())}, key="", algorithm="none")
    with pytest.raises(TeleCloudError) as excinfo:
        await verify_supabase_token(token)
    _assert_unauthorized(excinfo)


@pytest.mark.asyncio
async def test_verify_supabase_token_rejects_garbage():
    with pytest.raises(TeleCloudError) as excinfo:
        await verify_supabase_token("not.a.jwt", hs_secret=SECRET)
    _assert_unauthorized(excinfo)
