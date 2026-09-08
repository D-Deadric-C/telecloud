"""Error-handling for the request pipeline (SPEC.md §5.1, §6.7).

The single place that turns an exception into the canonical API error envelope::

    { "error": { "code": <string>, "message": <string> } }

Two handlers are registered on the app:

* :func:`telecloud_error_handler` — renders an expected
  :class:`~telecloud.shared.TeleCloudError` using the ``code``/``message`` it
  carries and the ``http_status`` it asks for. The body is exactly
  :meth:`TeleCloudError.to_dict`.
* :func:`unhandled_exception_handler` — the catch-all for anything *not* a
  ``TeleCloudError``. It logs the real exception (with traceback) for operators
  but returns a generic ``internal_error`` 500 to the client, so internal
  details never leak (SPEC §5.1, §6.7).

Both render through :func:`render_error`, which is also reused by the rate-limit
middleware so a 429 built outside the routing layer gets a byte-identical body.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from telecloud.shared import ErrorCode, TeleCloudError

#: Operator-facing logger. Unexpected exceptions are logged here with full
#: detail; the client only ever sees the generic envelope.
logger = logging.getLogger("telecloud.middleware.errors")


def render_error(exc: TeleCloudError) -> JSONResponse:
    """Render a :class:`TeleCloudError` as the SPEC §5.1 JSON response.

    The body is the canonical ``{"error": {"code", "message"}}`` envelope and the
    HTTP status is the one carried on the exception. Shared by the exception
    handler and any middleware that needs to emit an error response directly
    (e.g. rate limiting, which runs outside the routing layer where the handler
    applies).
    """
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


async def telecloud_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Handle an expected :class:`TeleCloudError` (SPEC §5.1).

    Signature is the Starlette handler shape (``exc`` typed as ``Exception``);
    the handler is only registered for ``TeleCloudError`` so the cast is safe.
    """
    assert isinstance(exc, TeleCloudError)  # narrowed by the registration
    return render_error(exc)


async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Convert any non-``TeleCloudError`` into a generic ``internal_error`` 500.

    The actual exception is logged with its traceback for operators; the client
    receives only the generic envelope so internal details (stack traces, driver
    errors, secrets embedded in messages) never leak (SPEC §5.1, §6.7).
    """
    logger.exception(
        "Unhandled exception on %s %s", request.method, request.url.path
    )
    error = TeleCloudError.from_code(
        ErrorCode.INTERNAL_ERROR, "An internal error occurred."
    )
    return render_error(error)


def register_error_handlers(app: FastAPI) -> None:
    """Register both error handlers on the FastAPI ``app`` (SPEC §6.7).

    ``TeleCloudError`` gets the envelope with its own status; everything else is
    funnelled through the catch-all 500 handler.
    """
    app.add_exception_handler(TeleCloudError, telecloud_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
