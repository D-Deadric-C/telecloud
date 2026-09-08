"""Module-private request/response models for the ``users/`` routes (SPEC §5.3).

These shapes are the wire contract of the profile endpoint, local to ``users/``
rather than shared vocabulary (SPEC §5.3). The cross-module identity model is
``shared.UserContext``; :class:`ProfileResponse` is its narrow, response-facing
view (the same fields, validated from a ``UserContext``).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from telecloud.shared import UserContext


class ProfileResponse(BaseModel):
    """Response-facing view of a profile (identity + verification status).

    Mirrors the fields of ``shared.UserContext``. Quota/usage figures are
    intentionally excluded — those belong to ``quota/`` (SPEC §6.5, §6.10).
    """

    id: UUID
    email: str
    email_verified: bool

    @classmethod
    def from_context(cls, user: UserContext) -> "ProfileResponse":
        return cls(id=user.id, email=user.email, email_verified=user.email_verified)
