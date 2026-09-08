"""HTTP-level tests for the job routes (SPEC §6.14).

The routes must be un-runnable without a valid QStash signature: an unsigned or
forged request gets ``401`` and never reaches the job (the stubbed service is never
called). A validly-signed request runs the job and returns its JSON summary. The
verifier dependency is overridden with **known test keys** (no real config / live
QStash), but it is the *real* :class:`QStashVerifier` doing the verification.
"""

from __future__ import annotations

import base64
import hashlib
import time

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from telecloud.middleware.errors import register_error_handlers

import telecloud.jobs.service as service
from telecloud.jobs.router import router, signing_keys
from telecloud.jobs.signature import QSTASH_ISSUER

TEST_KEY = "sig_test_current_" + "k" * 32  # 32+ bytes: quiet PyJWT's HS256 advisory


def _sign(body: bytes, *, key: str = TEST_KEY) -> str:
    now = int(time.time())
    body_hash = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=").decode()
    claims = {
        "iss": QSTASH_ISSUER,
        "sub": "https://telecloud.fly.dev/jobs/sweep-orphans",
        "iat": now,
        "nbf": now,
        "exp": now + 300,
        "body": body_hash,
    }
    return jwt.encode(claims, key, algorithm="HS256")


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """An app wired with the real router but test signing keys + stubbed jobs."""
    captured: dict[str, int] = {}

    async def fake_sweep(**_):
        captured["sweep"] = captured.get("sweep", 0) + 1
        return service.CleanupResult(files_removed=2, messages_deleted=5)

    async def fake_deferred(**_):
        captured["deferred"] = captured.get("deferred", 0) + 1
        return service.CleanupResult(files_removed=1)

    monkeypatch.setattr(service, "sweep_orphans", fake_sweep)
    monkeypatch.setattr(service, "delete_deferred", fake_deferred)

    app = FastAPI()
    app.include_router(router)
    # middleware/ owns rendering TeleCloudError → the §5.1 envelope; register it so a
    # rejected signature surfaces as a 401 envelope rather than a bare 500.
    register_error_handlers(app)
    # Inject known signing keys (no real config / live QStash); the real verifier runs.
    app.dependency_overrides[signing_keys] = lambda: (TEST_KEY,)
    test_client = TestClient(app, raise_server_exceptions=False)
    test_client.captured = captured  # type: ignore[attr-defined]
    return test_client


def test_unsigned_request_is_rejected(client):
    resp = client.post("/jobs/sweep-orphans", content=b"")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
    # The job never ran.
    assert client.captured == {}  # type: ignore[attr-defined]


def test_forged_signature_is_rejected(client):
    body = b""
    forged = _sign(body, key="sig_attacker_" + "x" * 32)
    resp = client.post(
        "/jobs/sweep-orphans", content=body, headers={"Upstash-Signature": forged}
    )
    assert resp.status_code == 401
    assert client.captured == {}  # type: ignore[attr-defined]


def test_body_tampering_is_rejected(client):
    # Signature is valid for an empty body, but a different body is delivered.
    signed = _sign(b"")
    resp = client.post(
        "/jobs/sweep-orphans",
        content=b"tampered",
        headers={"Upstash-Signature": signed},
    )
    assert resp.status_code == 401
    assert client.captured == {}  # type: ignore[attr-defined]


def test_valid_signature_runs_orphan_sweep(client):
    body = b""
    resp = client.post(
        "/jobs/sweep-orphans", content=body, headers={"Upstash-Signature": _sign(body)}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "files_removed": 2,
        "messages_deleted": 5,
        "messages_queued": 0,
        "retries_processed": 0,
        "retries_dead_lettered": 0,
    }
    assert client.captured == {"sweep": 1}  # type: ignore[attr-defined]


def test_valid_signature_runs_deferred_delete(client):
    body = b""
    resp = client.post(
        "/jobs/deferred-delete", content=body, headers={"Upstash-Signature": _sign(body)}
    )
    assert resp.status_code == 200
    assert resp.json()["files_removed"] == 1
    assert client.captured == {"deferred": 1}  # type: ignore[attr-defined]
