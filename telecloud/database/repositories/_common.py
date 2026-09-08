"""Internal helpers shared by the per-table repositories.

Keep these tiny and pure: extracting the row list from a PostgREST response and
selecting the single expected row. Repositories use them so the
``execute() -> APIResponse(data=[...])`` shape lives in one place.
"""

from __future__ import annotations

from typing import Any


def rows(response: Any) -> list[dict[str, Any]]:
    """Return the list of row dicts from a PostgREST response (``[]`` if none)."""
    data = getattr(response, "data", None)
    return list(data) if data else []


def first(response: Any) -> dict[str, Any] | None:
    """Return the first row dict from a response, or ``None`` when empty."""
    data = rows(response)
    return data[0] if data else None
