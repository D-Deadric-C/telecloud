"""The shared application error type and its reserved codes (SPEC.md §5.1).

Every expected failure in TeleCloud is raised as a :class:`TeleCloudError`. The
``middleware/`` package is the single place that catches it and renders the
canonical JSON envelope::

    { "error": { "code": <string>, "message": <string> } }

This module is **pure** — it raises and serializes; it performs no I/O and knows
nothing about FastAPI. The HTTP framing (status code on the wire, JSON encoding)
is applied by ``middleware/`` using the data carried on the exception.
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """The reserved, stable error codes from SPEC §5.1.

    A ``str`` enum so a member compares equal to its wire string
    (``ErrorCode.NOT_FOUND == "not_found"``) and serializes directly to JSON.
    The set is intentionally small and stable; extend it here (never invent
    ad-hoc code strings elsewhere) so the API's error vocabulary stays in one
    place.
    """

    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    QUOTA_EXCEEDED = "quota_exceeded"
    FILE_TOO_LARGE = "file_too_large"
    RATE_LIMITED = "rate_limited"
    UPLOAD_INCOMPLETE = "upload_incomplete"
    SHARE_EXPIRED = "share_expired"
    SHARE_REVOKED = "share_revoked"
    TELEGRAM_ERROR = "telegram_error"
    VALIDATION_ERROR = "validation_error"
    INTERNAL_ERROR = "internal_error"


#: Sensible default HTTP status for each reserved code. Used by
#: :meth:`TeleCloudError.from_code` so callers don't have to repeat the obvious
#: mapping; an explicit ``http_status`` always wins over this table.
DEFAULT_STATUS: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.QUOTA_EXCEEDED: 413,
    ErrorCode.FILE_TOO_LARGE: 413,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.UPLOAD_INCOMPLETE: 409,
    ErrorCode.SHARE_EXPIRED: 410,
    ErrorCode.SHARE_REVOKED: 410,
    ErrorCode.TELEGRAM_ERROR: 502,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.INTERNAL_ERROR: 500,
}


class TeleCloudError(Exception):
    """The one application exception type (SPEC §5.1).

    Carries everything ``middleware/`` needs to render the error response:

    * :attr:`code` — a stable machine-readable string (an :class:`ErrorCode`
      value). Stored as a plain ``str`` so it serializes cleanly.
    * :attr:`message` — a human-readable, client-safe description.
    * :attr:`http_status` — the HTTP status the middleware should send.

    Construct it directly when you want full control::

        raise TeleCloudError(ErrorCode.NOT_FOUND, "No such file", 404)

    or via :meth:`from_code`, which fills in the conventional status::

        raise TeleCloudError.from_code(ErrorCode.NOT_FOUND, "No such file")
    """

    def __init__(self, code: ErrorCode | str, message: str, http_status: int) -> None:
        # Normalize an ErrorCode member to its string value; pass strings
        # through unchanged so this never rejects a caller-supplied code.
        self.code: str = code.value if isinstance(code, ErrorCode) else str(code)
        self.message: str = message
        self.http_status: int = http_status
        super().__init__(message)

    @classmethod
    def from_code(
        cls,
        code: ErrorCode,
        message: str | None = None,
        http_status: int | None = None,
    ) -> "TeleCloudError":
        """Build an error using the default status (and message) for ``code``.

        ``http_status`` defaults to :data:`DEFAULT_STATUS` for the code and
        ``message`` defaults to the code string itself. Either may be overridden.
        """
        status = http_status if http_status is not None else DEFAULT_STATUS[code]
        return cls(code, message if message is not None else code.value, status)

    def to_dict(self) -> dict[str, dict[str, str]]:
        """Return the canonical error envelope from SPEC §5.1.

        ``{"error": {"code": ..., "message": ...}}`` — the exact JSON body the
        API returns. ``middleware/`` pairs this with :attr:`http_status`.
        """
        return {"error": {"code": self.code, "message": self.message}}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"TeleCloudError(code={self.code!r}, message={self.message!r}, "
            f"http_status={self.http_status!r})"
        )
