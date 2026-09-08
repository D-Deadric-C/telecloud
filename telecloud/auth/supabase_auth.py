"""Async wrapper over Supabase GoTrue for signup/login/logout (SPEC.md §6.4).

Password handling is **delegated to Supabase** — this module never sees or hashes
a password beyond forwarding it to GoTrue, and never rolls its own credential
store (SPEC §6.4). It is a thin, async adapter: it turns GoTrue's response into a
small, framework-free :class:`AuthSession` (the user id/email plus the issued
tokens) and turns GoTrue's errors into the shared :class:`TeleCloudError`.

It depends only on the Supabase auth client and ``config`` (for the URL + anon
key). It does **not** touch the database or enforce quota — those belong to
``database/`` and ``quota/``. Verification email is sent by **Supabase** itself
(built-in email confirmation), not by the app (SPEC §6.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import httpx
from supabase import AsyncClient, create_async_client
from supabase.lib.client_options import AsyncClientOptions
from supabase_auth.errors import AuthApiError, AuthError

from telecloud.config import get_settings
from telecloud.shared import ErrorCode, TeleCloudError


@dataclass(frozen=True)
class AuthSession:
    """The result of a successful signup/login — identity + issued tokens.

    ``user_id``/``email`` identify the Supabase auth user (used to create the
    profile shell on signup). ``access_token`` is the JWT the client sends on
    subsequent requests; ``refresh_token`` lets the client renew it and is what
    logout revokes. ``email_verified`` here reflects *Supabase's* state and is not
    authoritative for TeleCloud's tier gate — that lives in ``profiles`` and is
    resolved fresh by ``auth.current_user`` (SPEC §3, §6.4).
    """

    user_id: UUID
    email: str
    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True)
class SignupResult:
    """The outcome of a signup when **Supabase email confirmation** is enabled.

    With "Confirm email" on, Supabase does not issue a session at signup — it
    sends a confirmation email and waits for the user to click the link
    (SPEC §6.4). So signup yields only the identity and a
    ``confirmation_required`` flag, not tokens; the client shows a "check your
    email" screen and gets a real session by logging in (or via the confirmation
    redirect) once confirmed.
    """

    user_id: UUID
    email: str
    confirmation_required: bool


class SupabaseAuth:
    """Async adapter over the Supabase GoTrue auth client.

    Build one with :meth:`from_settings` (uses the project URL + anon key from
    ``config``), or inject any object exposing a compatible ``.auth`` in tests.
    The wrapper maps GoTrue exceptions to :class:`TeleCloudError`: bad credentials
    → ``unauthorized`` (401), other auth-API problems (e.g. duplicate email, weak
    password) → ``validation_error`` (422), unreachable/unknown → ``internal_error``.
    """

    def __init__(self, client: AsyncClient, *, email_redirect_to: str | None = None) -> None:
        self._client = client
        self._email_redirect_to = email_redirect_to

    @classmethod
    async def from_settings(cls) -> "SupabaseAuth":
        """Build the adapter from ``config.get_settings()`` (SPEC §5.2, §6.4)."""
        settings = get_settings()
        client = await create_async_client(
            settings.supabase_url,
            settings.supabase_anon_key,
            options=AsyncClientOptions(
                auto_refresh_token=False, persist_session=False
            ),
        )
        # Where Supabase's confirmation link sends the user back to. Must be in the
        # project's redirect allow-list (Supabase dashboard → URL Configuration).
        # The single-file SPA is served at /index.html (SPEC §6.4); it reads the
        # session tokens Supabase appends to the URL fragment on confirmation.
        return cls(client, email_redirect_to=f"{settings.app_base_url}/index.html")

    # -- signup / login ----------------------------------------------------

    async def sign_up(self, *, email: str, password: str) -> SignupResult:
        """Create a Supabase auth user; Supabase emails the confirmation link.

        With "Confirm email" enabled (SPEC §6.4) Supabase returns no session here
        — it sends the verification email and waits for the click. We return a
        :class:`SignupResult` flagging that confirmation is required.

        Raises ``validation_error`` if Supabase rejects the credentials (e.g. the
        email is already registered or the password is too weak).
        """
        options: dict[str, object] = {}
        if self._email_redirect_to:
            options["email_redirect_to"] = self._email_redirect_to
        payload: dict[str, object] = {"email": email, "password": password}
        if options:
            payload["options"] = options
        try:
            response = await self._client.auth.sign_up(payload)
        except AuthApiError as exc:
            raise self._api_error(exc, default=ErrorCode.VALIDATION_ERROR) from exc
        except AuthError as exc:
            raise self._unknown_error(exc) from exc
        except httpx.HTTPError as exc:
            raise self._unknown_error(exc) from exc

        user = getattr(response, "user", None)
        session = getattr(response, "session", None)
        if user is None:
            raise TeleCloudError.from_code(
                ErrorCode.INTERNAL_ERROR, "Supabase returned no user on signup."
            )
        return SignupResult(
            user_id=UUID(str(user.id)),
            email=user.email or email,
            # A session means confirmation is OFF in the project; treat anything
            # without a live session as "must confirm via email".
            confirmation_required=session is None
            or not getattr(session, "access_token", None),
        )

    async def resend_confirmation(self, *, email: str) -> None:
        """Re-send the signup confirmation email for an unconfirmed address.

        Best-effort and intentionally **does not reveal** whether the address
        exists or is already confirmed — Supabase handles that — so it never
        raises on that normal "already confirmed / unknown" outcome. A genuine
        send failure (e.g. Supabase's email rate limit) doesn't reveal anything
        about the address either, so — unlike that benign case — it must not be
        swallowed: doing so previously made every resend report success even
        when Supabase never actually sent anything. Such failures surface as
        ``rate_limited``; a transport failure surfaces as ``internal_error``.
        """
        params: dict[str, object] = {"type": "signup", "email": email}
        if self._email_redirect_to:
            params["options"] = {"email_redirect_to": self._email_redirect_to}
        try:
            await self._client.auth.resend(params)
        except AuthApiError as exc:
            if exc.code in ("over_email_send_rate_limit", "over_request_rate_limit"):
                raise TeleCloudError.from_code(
                    ErrorCode.RATE_LIMITED,
                    "Too many verification emails requested. Please wait a bit"
                    " before trying again.",
                ) from exc
            # e.g. "already confirmed" — not an error worth leaking to the client.
            return
        except AuthError as exc:
            raise self._unknown_error(exc) from exc
        except httpx.HTTPError as exc:
            raise self._unknown_error(exc) from exc

    async def sign_in(self, *, email: str, password: str) -> AuthSession:
        """Verify credentials with Supabase and return the issued session.

        Raises ``unauthorized`` on bad credentials (the common case) and on any
        other GoTrue API error during sign-in.
        """
        try:
            response = await self._client.auth.sign_in_with_password(
                {"email": email, "password": password}
            )
        except AuthApiError as exc:
            raise self._api_error(exc, default=ErrorCode.UNAUTHORIZED) from exc
        except AuthError as exc:
            raise self._unknown_error(exc) from exc
        except httpx.HTTPError as exc:
            raise self._unknown_error(exc) from exc
        return self._session_from(response, on_missing="login")

    # -- logout ------------------------------------------------------------

    async def sign_out(self, *, access_token: str, refresh_token: str | None) -> None:
        """Revoke the user's session with Supabase (best-effort).

        Access-token JWTs are stateless and cannot be revoked before they expire;
        what logout *can* do is revoke the refresh token so the session can't be
        renewed. We set the session on the client (from the tokens the client
        holds) and ask GoTrue to sign out. A missing refresh token (stateless
        logout) or a GoTrue error is tolerated — the client discarding its tokens
        is the real logout — so this never raises.
        """
        if not refresh_token:
            return
        try:
            await self._client.auth.set_session(access_token, refresh_token)
            await self._client.auth.sign_out()
        except AuthError:
            # Already-expired/already-revoked sessions are fine to ignore.
            return
        except httpx.HTTPError:
            # Unreachable Supabase is not the client's problem — see docstring.
            return

    async def aclose(self) -> None:
        """Release the underlying HTTP resources (call at app shutdown)."""
        # The GoTrue client owns an httpx session; close it if available.
        close = getattr(self._client.auth, "aclose", None)
        if close is not None:
            await close()

    # -- internals ---------------------------------------------------------

    def _session_from(self, response: object, *, on_missing: str) -> AuthSession:
        session = getattr(response, "session", None)
        user = getattr(response, "user", None)
        if session is None or user is None or not getattr(session, "access_token", None):
            raise TeleCloudError.from_code(
                ErrorCode.INTERNAL_ERROR,
                f"Supabase returned no session on {on_missing}.",
            )
        return AuthSession(
            user_id=UUID(str(user.id)),
            email=user.email or "",
            access_token=session.access_token,
            refresh_token=session.refresh_token or "",
            expires_in=int(getattr(session, "expires_in", 0) or 0),
        )

    @staticmethod
    def _api_error(exc: AuthApiError, *, default: ErrorCode) -> TeleCloudError:
        """Map a GoTrue API error to a TeleCloudError, preserving its message."""
        message = str(getattr(exc, "message", None) or exc) or "Authentication failed."
        return TeleCloudError.from_code(default, message)

    @staticmethod
    def _unknown_error(exc: AuthError | httpx.HTTPError) -> TeleCloudError:
        return TeleCloudError.from_code(
            ErrorCode.INTERNAL_ERROR, "Could not reach the authentication service."
        )
