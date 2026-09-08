"""``telecloud.middleware`` — the cross-cutting request pipeline (SPEC.md §6.7).

The middleware registered on the FastAPI app at startup. It owns four
cross-cutting concerns and nothing else (no feature logic):

* **Error handling** — :class:`~telecloud.shared.TeleCloudError` is rendered as
  the SPEC §5.1 envelope ``{"error": {"code", "message"}}`` with its declared
  status; any other exception becomes a generic ``internal_error`` 500 with the
  real cause logged but never leaked.
* **Rate limiting** — a per-request limit keyed by authenticated user (via
  ``auth``) or client IP, backed by ``rate_limit.limiter``; over-limit →
  ``rate_limited`` 429.
* **CORS** — frontend origin(s) derived from ``config``.
* **Request logging** — one minimal structured line (method, path, status,
  duration); never logs tokens or secrets.

Primary entry point:

* :func:`register_middleware` — wire all of the above onto a FastAPI app.

Also exported for fine-grained wiring and tests: the individual middleware
classes, the handlers, and the error renderer.

**Boundaries (SPEC §6.7):** contains no feature logic (files, quota, sharing,
…) and depends only on ``config``, ``shared``, ``auth``, ``rate_limit``.
"""

from telecloud.middleware.cors import (
    register_cors,
    resolve_cors_origins,
)
from telecloud.middleware.errors import (
    register_error_handlers,
    render_error,
    telecloud_error_handler,
    unhandled_exception_handler,
)
from telecloud.middleware.logging import RequestLoggingMiddleware
from telecloud.middleware.rate_limit import (
    DEFAULT_REQUEST_LIMIT,
    DEFAULT_WINDOW_SECONDS,
    RateLimitMiddleware,
    resolve_key,
)
from telecloud.middleware.registration import register_middleware

__all__ = [
    # primary entry point
    "register_middleware",
    # error handling
    "register_error_handlers",
    "render_error",
    "telecloud_error_handler",
    "unhandled_exception_handler",
    # rate limiting
    "RateLimitMiddleware",
    "resolve_key",
    "DEFAULT_REQUEST_LIMIT",
    "DEFAULT_WINDOW_SECONDS",
    # cors
    "register_cors",
    "resolve_cors_origins",
    # logging
    "RequestLoggingMiddleware",
]
