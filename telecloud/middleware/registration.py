"""The single entry point that wires the pipeline onto the app (SPEC.md §6.7).

``register_middleware(app)`` installs everything ``middleware/`` owns:

1. **Error handlers** — ``TeleCloudError`` → §5.1 envelope, plus the catch-all
   ``internal_error`` 500.
2. **Request logging** — minimal structured line per request.
3. **Rate limiting** — per-user / per-IP request limit, ``rate_limited`` 429.
4. **CORS** — allowed frontend origin(s) from ``config``.

**Ordering matters.** Starlette wraps each ``add_middleware`` call *outside* the
previous one, so the last added is the outermost. We want, from outermost to
innermost: **CORS → logging → rate limit → (exception handlers → router)**. That
way CORS headers are applied to every response (including errors), the log line
times the whole request and sees the final status (including a 429), and the rate
limiter runs before any route work. The order of the three ``add_middleware``
calls below is therefore rate-limit, then logging, then CORS.

Exception handlers are not part of the middleware stack ordering — Starlette runs
them in its inner ``ExceptionMiddleware`` (for ``TeleCloudError``) and outer
``ServerErrorMiddleware`` (for the 500 catch-all), so route-raised errors are
rendered correctly regardless of the calls above.
"""

from __future__ import annotations

from fastapi import FastAPI

from telecloud.config import Settings

from telecloud.middleware.cors import register_cors
from telecloud.middleware.errors import register_error_handlers
from telecloud.middleware.logging import RequestLoggingMiddleware
from telecloud.middleware.rate_limit import (
    DEFAULT_REQUEST_LIMIT,
    DEFAULT_WINDOW_SECONDS,
    LimiterCheck,
    RateLimitMiddleware,
)


def register_middleware(
    app: FastAPI,
    *,
    rate_limit: int = DEFAULT_REQUEST_LIMIT,
    rate_window_seconds: float = DEFAULT_WINDOW_SECONDS,
    limiter_check: LimiterCheck | None = None,
    cors_origins: list[str] | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Register the full request pipeline on ``app`` and return it (SPEC §6.7).

    Idempotent per app instance is *not* guaranteed — call once at startup.
    Keyword arguments let a deploy tune the request limit/window or override the
    CORS origins without touching this module. ``limiter_check`` overrides the
    shared ``rate_limit.limiter`` backend (mainly for tests/wiring); by default
    the process-wide limiter is used.
    """
    # Exception handlers first (independent of stack order).
    register_error_handlers(app)

    # Stack: added inner→outer. Final outermost→innermost is CORS, logging,
    # rate limit, then the app's routing/handlers.
    app.add_middleware(
        RateLimitMiddleware,
        limit=rate_limit,
        window_seconds=rate_window_seconds,
        limiter_check=limiter_check,
    )
    app.add_middleware(RequestLoggingMiddleware)
    register_cors(app, origins=cors_origins, settings=settings)

    return app
