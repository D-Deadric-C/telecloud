"""Per-request rate limiting (SPEC.md §6.7).

Applies a coarse, app-level request limit using the generic limiter from
``rate_limit`` (:func:`telecloud.rate_limit.limiter.check`). Each request is
bucketed by a **key**:

* the authenticated **user id** when a valid bearer token is present
  (resolved through ``auth`` — the same verification ``current_user`` uses), or
* the **client IP** otherwise (honoring ``X-Forwarded-For`` since the app runs
  behind Fly.io's proxy).

When a bucket is over its limit the middleware emits a ``rate_limited`` 429 using
the shared error envelope (SPEC §5.1). It builds that response directly rather
than raising, because this middleware sits *outside* the routing layer where the
``TeleCloudError`` exception handler applies.

If the limiter's backend itself fails (e.g. Redis is unreachable), the middleware
**fails open** — it logs a warning and lets the request through, so a limiter
outage degrades fairness rather than taking the whole API down. Only a genuine
over-limit decision produces a 429.

Boundaries (SPEC §6.7): this is pipeline policy, not feature logic. It depends
only on ``config``/``shared``/``auth``/``rate_limit``.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from telecloud.auth import verify_supabase_token
from telecloud.auth.tokens import user_id_from_claims
from telecloud.rate_limit import limiter as default_limiter
from telecloud.shared import ErrorCode, TeleCloudError

from telecloud.middleware.errors import render_error

logger = logging.getLogger("telecloud.middleware.rate_limit")

#: Default ceiling: at most this many requests per key within
#: :data:`DEFAULT_WINDOW_SECONDS`. Deliberately generous — this is a coarse
#: abuse guard for a ~10-user system (SPEC §1), not a fine-grained quota. Tune
#: via :func:`register_middleware`.
DEFAULT_REQUEST_LIMIT = 120

#: Trailing window (seconds) the limit is measured over.
DEFAULT_WINDOW_SECONDS = 60.0

#: Type of the pluggable "who is this request" resolver. Returns the bare logical
#: key (the limiter namespaces it); never raises (auth failures fall back to IP).
KeyResolver = Callable[[Request], Awaitable[str]]

#: Type of the limiter call (``check(key, limit, window) -> allowed``), injectable
#: for tests so no real Redis is needed.
LimiterCheck = Callable[[str, int, float], Awaitable[bool]]


def client_ip(request: Request) -> str:
    """Best-effort client IP for the rate-limit key.

    Honors the first address in ``X-Forwarded-For`` (the original client when
    behind Fly.io's proxy), falling back to the direct peer, then ``"unknown"``.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first
    client = request.client
    if client is not None and client.host:
        return client.host
    return "unknown"


def _bearer_token(request: Request) -> str | None:
    """Extract a bearer token from the ``Authorization`` header, if any."""
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def resolve_key(request: Request) -> str:
    """Resolve the rate-limit key for ``request`` (SPEC §6.7).

    A valid bearer token yields ``"user:<id>"`` (verified the same way
    ``auth.current_user`` verifies it); a missing or invalid token falls back to
    ``"ip:<addr>"``. Verification failures never propagate here — an unauthorized
    request is still rate-limited, just by IP.
    """
    token = _bearer_token(request)
    if token is not None:
        try:
            claims = await verify_supabase_token(token)
            return f"user:{user_id_from_claims(claims)}"
        except TeleCloudError:
            # Invalid/expired token — fall back to IP-based limiting rather than
            # rejecting here; auth proper will reject it during routing.
            pass
    return f"ip:{client_ip(request)}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Limit requests per user/IP, emitting ``rate_limited`` 429 on block."""

    def __init__(
        self,
        app: object,
        *,
        limit: int = DEFAULT_REQUEST_LIMIT,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        key_resolver: KeyResolver = resolve_key,
        limiter_check: LimiterCheck | None = None,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]  # Starlette ASGIApp
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be a positive duration")
        self._limit = limit
        self._window = window_seconds
        self._resolve_key = key_resolver
        self._check = limiter_check or default_limiter.check

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        key = await self._resolve_key(request)
        try:
            allowed = await self._check(key, self._limit, self._window)
        except TeleCloudError:
            # Limiter backend failure (e.g. Redis down): fail open so an outage
            # degrades fairness instead of denying every request.
            logger.warning("Rate limiter unavailable; allowing request", exc_info=True)
            allowed = True
        if not allowed:
            error = TeleCloudError.from_code(
                ErrorCode.RATE_LIMITED, "Too many requests. Please slow down."
            )
            return render_error(error)
        return await call_next(request)
