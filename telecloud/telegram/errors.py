"""The Telegram transport's error type (SPEC.md §5.1, §6.8).

Everything that can go wrong while moving bytes to/from Telegram surfaces as a
:class:`TelegramError`. It **is a** :class:`~telecloud.shared.TeleCloudError`
carrying the reserved ``telegram_error`` code (SPEC §5.1), so ``middleware/``
renders it like any other application error and callers can ``except
TeleCloudError``. It adds two things the transport needs to make decisions:

* :attr:`transient` — whether retrying could plausibly succeed (a network blip,
  a Telegram ``429``/``5xx``, or our own per-bot/per-channel limit denying the
  call). Permanent failures (a ``400`` bad request, a ``403`` forbidden) set this
  ``False`` so we don't pointlessly re-enqueue them.
* :attr:`telegram_code` — Telegram's own ``error_code`` from the API body, when
  present, kept for logging/diagnostics only.
"""

from __future__ import annotations

from telecloud.shared import ErrorCode, TeleCloudError

#: HTTP status the API returns when a Telegram operation fails. A bad *gateway*
#: (502) is the honest framing: TeleCloud reached out to an upstream (Telegram)
#: and that call failed — it isn't the client's fault. Matches the default for
#: ``telegram_error`` in ``shared`` (SPEC §5.1).
TELEGRAM_HTTP_STATUS = 502


class TelegramError(TeleCloudError):
    """A failure in the Telegram transport, tagged transient-or-not.

    Always uses code ``telegram_error`` (SPEC §5.1). Construct it with a
    client-safe ``message``; set ``transient`` so the transport knows whether to
    enqueue a retry before re-raising (SPEC §6.8).
    """

    def __init__(
        self,
        message: str,
        *,
        transient: bool,
        http_status: int = TELEGRAM_HTTP_STATUS,
        telegram_code: int | None = None,
    ) -> None:
        super().__init__(ErrorCode.TELEGRAM_ERROR, message, http_status)
        self.transient = transient
        self.telegram_code = telegram_code

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"TelegramError(message={self.message!r}, transient={self.transient!r}, "
            f"telegram_code={self.telegram_code!r})"
        )
