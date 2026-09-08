"""Structured, minimal request logging (SPEC.md §6.7).

One log line per request with exactly the four fields the SPEC asks for —
**method, path, status, duration** — and nothing that could leak a secret. The
request *path* is logged (``request.url.path``), never the query string or the
``Authorization`` header, so bearer tokens and share tokens (which can ride in a
query string) are never written to logs.

Implemented as a :class:`BaseHTTPMiddleware` placed at the outer edge of the
app-level stack so the duration spans the whole request, including downstream
middleware. If a downstream exception propagates (it is converted to a 500 by the
error handler further out), it is still logged here as a ``500`` before being
re-raised.
"""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

#: Structured request logger. Fields are attached via ``extra`` so a JSON log
#: formatter can pick them up; the message string stays human-readable too.
logger = logging.getLogger("telecloud.middleware.request")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log ``method path status duration_ms`` for every request (SPEC §6.7)."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        method = request.method
        # Path only — never the query string (it may carry share/verification
        # tokens) and never request headers (the bearer token lives there).
        path = request.url.path
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - start) * 1000
            # The exception becomes a 500 at the outer error handler; record it
            # with that status here, then let it propagate.
            self._log(method, path, 500, duration_ms)
            raise
        duration_ms = (time.perf_counter() - start) * 1000
        self._log(method, path, response.status_code, duration_ms)
        return response

    @staticmethod
    def _log(method: str, path: str, status: int, duration_ms: float) -> None:
        logger.info(
            "%s %s %d %.1fms",
            method,
            path,
            status,
            duration_ms,
            extra={
                "http_method": method,
                "http_path": path,
                "http_status": status,
                "duration_ms": round(duration_ms, 1),
            },
        )
