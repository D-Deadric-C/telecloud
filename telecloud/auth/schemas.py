"""Module-private request/response models for the auth routes (SPEC §5.3).

These shapes are local to ``auth/`` — they are the wire contract of the
signup/login/logout endpoints, not shared vocabulary, so they live here rather
than in ``shared/`` (SPEC §5.3). The cross-module identity model is
``shared.UserContext``; :class:`PublicUser` is its narrow, response-facing view.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from telecloud.shared import UserContext


class SignupRequest(BaseModel):
    """Credentials for creating an account. Password is forwarded to Supabase."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=256)


class LoginRequest(BaseModel):
    """Credentials for an existing account."""

    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class ResendRequest(BaseModel):
    """Email address to re-send the Supabase confirmation link to."""

    email: EmailStr


class LogoutRequest(BaseModel):
    """Optional refresh token to revoke server-side on logout.

    Access-token JWTs are stateless and expire on their own; passing the
    ``refresh_token`` lets the server revoke the renewable session. Omitting it
    performs a stateless (client-discards-token) logout.
    """

    refresh_token: str | None = None


class PublicUser(BaseModel):
    """Response-facing view of the authenticated user."""

    id: UUID
    email: str
    email_verified: bool

    @classmethod
    def from_context(cls, user: UserContext) -> "PublicUser":
        return cls(id=user.id, email=user.email, email_verified=user.email_verified)


class SessionResponse(BaseModel):
    """Issued session returned by login.

    Carries the tokens the client stores: ``access_token`` (sent as the bearer on
    later requests) and ``refresh_token`` (used to renew / passed back to logout).
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: PublicUser


class SignupResponse(BaseModel):
    """Returned by signup when Supabase email confirmation is enabled (SPEC §6.4).

    Signup issues no session — Supabase sends a confirmation email and the client
    must wait for the user to click the link, then log in. The client shows a
    "check your email" screen based on ``confirmation_required``.
    """

    email: str
    confirmation_required: bool = True
    message: str = (
        "Account created. Check your email for a verification link to finish "
        "setting up your account."
    )
