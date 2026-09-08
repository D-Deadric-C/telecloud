"""FastAPI router for the auth endpoints (SPEC.md §6.4).

Three routes — ``POST /auth/signup``, ``POST /auth/login``, ``POST /auth/logout``
— each a thin shell that validates the request body and delegates to
:mod:`telecloud.auth.service`. Password handling lives in Supabase; profile
creation on signup is the service's job. Errors raised below the route surface as
:class:`TeleCloudError` and are rendered to the canonical JSON envelope by
``middleware/`` (SPEC §5.1) — the router does not format errors itself.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from telecloud.auth import service
from telecloud.auth.dependencies import access_token
from telecloud.auth.schemas import (
    LoginRequest,
    LogoutRequest,
    ResendRequest,
    SessionResponse,
    SignupRequest,
    SignupResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=SignupResponse, status_code=status.HTTP_201_CREATED)
async def signup(body: SignupRequest) -> SignupResponse:
    """Create an account; Supabase emails a confirmation link (no session yet)."""
    return await service.signup(email=body.email, password=body.password)


@router.post("/resend-confirmation", status_code=status.HTTP_202_ACCEPTED)
async def resend_confirmation(body: ResendRequest) -> Response:
    """Re-send the Supabase email-confirmation link (unauthenticated, best-effort).

    202 regardless of whether the address exists or is already confirmed, so it
    never reveals account state. A genuine send failure (e.g. Supabase's email
    rate limit) doesn't reveal account state either, so it is NOT masked as a
    202 — it propagates as a real error (``rate_limited``/``internal_error``).
    """
    await service.resend_confirmation(email=body.email)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/login", response_model=SessionResponse)
async def login(body: LoginRequest) -> SessionResponse:
    """Authenticate against Supabase and return the issued session."""
    return await service.login(email=body.email, password=body.password)


@router.post("/logout")
async def logout(
    body: LogoutRequest | None = None,
    token: str = Depends(access_token),
) -> Response:
    """Revoke the caller's session. Requires a bearer token; body is optional."""
    refresh_token = body.refresh_token if body is not None else None
    await service.logout(access_token=token, refresh_token=refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
