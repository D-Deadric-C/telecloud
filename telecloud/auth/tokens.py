"""JWT verify/issue for Supabase-issued access tokens (SPEC.md §2, §3, §6.4).

TeleCloud is JWT-based and **backed by Supabase**: access tokens are minted by
Supabase GoTrue at sign-in. This module is the one place that knows how to
**verify** such a token (and, for symmetry/tests, **encode** an HS256 one).

Supabase supports two signing models, and this module handles both:

* **Asymmetric (ES256/RS256)** — the modern default. Tokens carry a ``kid`` and
  are verified with the project's public key from the JWKS endpoint
  (``{supabase_url}/auth/v1/.well-known/jwks.json``). Keys are fetched once and
  cached by ``kid``. This is the async path the request dependency uses.
* **Legacy HS256** — the shared ``SUPABASE_JWT_SECRET`` from ``config``. Still
  used by older projects (and by the API keys themselves).

SPEC §2/§6.1 describe only the HS256 "verify against the JWT secret" model; real
projects now default to asymmetric keys, so verification follows the token's own
``alg`` header. See README "Contract notes".

Config is read only through ``config`` (never the environment, SPEC §5.2). The
JWKS fetch is the one bit of I/O here and is async (SPEC §5.4); claims logic is
otherwise pure. Every failure surfaces as ``TeleCloudError("unauthorized", 401)``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import UUID

import httpx
import jwt

from telecloud.config import get_settings
from telecloud.shared import ErrorCode, TeleCloudError

#: Symmetric algorithm for the legacy shared-secret model (and for ``encode_token``).
ALGORITHM = "HS256"

#: Asymmetric algorithms Supabase may use with JWKS-published public keys.
ASYMMETRIC_ALGORITHMS = ("ES256", "RS256")

#: The ``aud`` claim Supabase stamps on a logged-in user's access token.
SUPABASE_AUDIENCE = "authenticated"

#: How long a single JWKS fetch may take before it's treated as a failure.
_JWKS_TIMEOUT_SECONDS = 10.0


def _unauthorized(message: str) -> TeleCloudError:
    """A 401 ``unauthorized`` error (SPEC §5.1) — the only failure tokens raise."""
    return TeleCloudError.from_code(ErrorCode.UNAUTHORIZED, message)


def _secret(secret: str | None) -> str:
    """Resolve the HS256 signing secret: explicit arg wins, else from ``config``."""
    if secret is not None:
        return secret
    return get_settings().supabase_jwt_secret


def _decode(
    token: str,
    key: Any,
    algorithms: list[str],
    audience: str | None,
    leeway: float,
) -> dict[str, Any]:
    """Decode + validate a token, mapping PyJWT's errors to ``unauthorized``.

    Requires ``sub`` and ``exp``; checks ``aud`` when ``audience`` is set. Shared
    by both the HS256 and the asymmetric verification paths.
    """
    try:
        return jwt.decode(
            token,
            key,
            algorithms=algorithms,
            audience=audience,
            leeway=leeway,
            options={
                "require": ["exp", "sub"],
                "verify_aud": audience is not None,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise _unauthorized("Authentication token has expired.") from exc
    except jwt.InvalidAudienceError as exc:
        raise _unauthorized("Authentication token has an invalid audience.") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise _unauthorized("Authentication token is missing required claims.") from exc
    except jwt.PyJWTError as exc:
        raise _unauthorized("Invalid authentication token.") from exc


# ---------------------------------------------------------------------------
# JWKS (asymmetric) verification
# ---------------------------------------------------------------------------

#: Process-wide cache of verified signing keys, keyed by ``kid``. Supabase
#: rotates keys rarely; an unknown ``kid`` triggers a single refetch.
_jwks_cache: dict[str, jwt.PyJWK] = {}
_jwks_lock = asyncio.Lock()

#: Long-lived HTTP client for JWKS fetches. Reused across fetches so the (rare)
#: key refetch keeps a warm keep-alive connection instead of paying a fresh
#: TCP+TLS handshake each time. Created lazily; closed by :func:`close_jwks_client`.
_jwks_client: httpx.AsyncClient | None = None
_jwks_client_lock = asyncio.Lock()


def _jwks_url() -> str:
    """The project's JWKS endpoint, derived from ``config.supabase_url``."""
    return f"{get_settings().supabase_url}/auth/v1/.well-known/jwks.json"


def reset_jwks_cache() -> None:
    """Forget cached signing keys (tests / key rotation)."""
    _jwks_cache.clear()


async def _get_jwks_client() -> httpx.AsyncClient:
    """Return the lazily-created, reused JWKS HTTP client."""
    global _jwks_client
    if _jwks_client is None:
        async with _jwks_client_lock:
            if _jwks_client is None:  # re-check inside the lock
                _jwks_client = httpx.AsyncClient(timeout=_JWKS_TIMEOUT_SECONDS)
    return _jwks_client


async def close_jwks_client() -> None:
    """Close and forget the shared JWKS HTTP client (shutdown/tests)."""
    global _jwks_client
    if _jwks_client is not None:
        await _jwks_client.aclose()
        _jwks_client = None


async def _fetch_jwks(jwks_url: str) -> None:
    """Fetch the JWKS and replace the key cache. Raises ``unauthorized`` on failure."""
    try:
        client = await _get_jwks_client()
        # The endpoint is public; the apikey header satisfies the Supabase
        # gateway without granting any privileges.
        headers = {"apikey": get_settings().supabase_anon_key}
        response = await client.get(jwks_url, headers=headers)
        response.raise_for_status()
        keys = response.json().get("keys", [])
    except (httpx.HTTPError, ValueError) as exc:
        raise _unauthorized("Could not retrieve token signing keys.") from exc

    refreshed: dict[str, jwt.PyJWK] = {}
    for key_dict in keys:
        kid = key_dict.get("kid")
        if not kid:
            continue
        try:
            refreshed[kid] = jwt.PyJWK.from_dict(key_dict)
        except jwt.PyJWTError:
            continue  # skip a malformed/unsupported key rather than fail the lot
    _jwks_cache.clear()
    _jwks_cache.update(refreshed)


async def _get_signing_key(kid: str, *, jwks_url: str) -> jwt.PyJWK:
    """Return the cached :class:`jwt.PyJWK` for ``kid``, refetching once on a miss."""
    key = _jwks_cache.get(kid)
    if key is not None:
        return key
    async with _jwks_lock:
        # Re-check inside the lock: a concurrent caller may have just fetched it.
        key = _jwks_cache.get(kid)
        if key is None:
            await _fetch_jwks(jwks_url)
            key = _jwks_cache.get(kid)
    if key is None:
        raise _unauthorized("Authentication token was signed by an unknown key.")
    return key


# ---------------------------------------------------------------------------
# Public verify / issue
# ---------------------------------------------------------------------------


async def verify_supabase_token(
    token: str,
    *,
    hs_secret: str | None = None,
    jwks_url: str | None = None,
    audience: str | None = SUPABASE_AUDIENCE,
    leeway: float = 0,
) -> dict[str, Any]:
    """Verify a Supabase access token (any signing model) and return its claims.

    Routes on the token's ``alg`` header: ``HS256`` is verified with the shared
    secret; ``ES256``/``RS256`` are verified with the JWKS public key for the
    token's ``kid`` (fetched + cached). This is the entry point the request
    dependency uses. Any failure → ``TeleCloudError("unauthorized", 401)``.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise _unauthorized("Invalid authentication token.") from exc

    alg = header.get("alg")
    if alg == ALGORITHM:
        return _decode(token, _secret(hs_secret), [ALGORITHM], audience, leeway)
    if alg in ASYMMETRIC_ALGORITHMS:
        kid = header.get("kid")
        if not kid:
            raise _unauthorized("Authentication token is missing a key id.")
        signing_key = await _get_signing_key(kid, jwks_url=jwks_url or _jwks_url())
        return _decode(token, signing_key.key, [alg], audience, leeway)
    raise _unauthorized("Authentication token uses an unsupported algorithm.")


def verify_token(
    token: str,
    *,
    secret: str | None = None,
    audience: str | None = SUPABASE_AUDIENCE,
    leeway: float = 0,
) -> dict[str, Any]:
    """Verify an **HS256** token synchronously against the shared secret.

    The legacy / shared-secret path: no network, no JWKS. Used for HS256 tokens
    and as the verify counterpart to :func:`encode_token` (tests, symmetry). For
    request auth, prefer :func:`verify_supabase_token`, which also handles the
    asymmetric tokens modern Supabase projects issue.
    """
    return _decode(token, _secret(secret), [ALGORITHM], audience, leeway)


def user_id_from_claims(claims: dict[str, Any]) -> UUID:
    """Extract the user id (``sub``) from verified claims as a :class:`UUID`.

    Raises ``unauthorized`` if ``sub`` is absent or not a valid UUID — Supabase
    user ids are UUIDs (they reference ``auth.users.id``, SPEC §4.1).
    """
    sub = claims.get("sub")
    if not sub:
        raise _unauthorized("Authentication token is missing a subject.")
    try:
        return UUID(str(sub))
    except ValueError as exc:
        raise _unauthorized("Authentication token subject is not a valid user id.") from exc


def encode_token(
    claims: dict[str, Any],
    *,
    secret: str | None = None,
    expires_in: int = 3600,
    audience: str = SUPABASE_AUDIENCE,
) -> str:
    """Sign an HS256 JWT with the shared secret — the verify counterpart.

    In production, access tokens are issued by **Supabase** at sign-in (this app
    delegates password handling and token minting to Supabase, SPEC §6.4); this
    helper exists so tests can mint HS256 tokens the verifier accepts, without a
    live Supabase project. ``exp`` and ``aud`` are filled in if absent.
    """
    payload = dict(claims)
    payload.setdefault("aud", audience)
    payload.setdefault("exp", int(time.time()) + expires_in)
    return jwt.encode(payload, _secret(secret), algorithm=ALGORITHM)
