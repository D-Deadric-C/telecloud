"""Row (de)serialization helpers for the PostgREST boundary.

PostgREST speaks JSON. Outbound write payloads must contain only JSON-native
values, so :func:`to_jsonable` coerces the Python types we hand it (``UUID``,
``datetime``, ``Enum``) into strings. Reads come back as plain ``dict`` rows that
the shared pydantic read models parse directly (``FileMeta.model_validate(row)``
handles the string→UUID/datetime conversion), so there is no inbound decoder
here — that is the shared models' job.

This module is pure: no I/O, no DB knowledge beyond value shapes.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID


def _encode_value(value: Any) -> Any:
    """Coerce a single value to its JSON-native form for a write payload."""
    # Enum first: FileStatus/ChunkStatus are *str* subclasses, so this must run
    # before the primitive shortcut below or the enum member (whose str() is
    # "FileStatus.PENDING", not "pending") would pass through unconverted.
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        # timestamptz round-trips as ISO-8601; keep the tzinfo the caller set.
        return value.isoformat()
    return value


def to_jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``payload`` with every value JSON-encoded for PostgREST.

    Keys are preserved; ``None`` values are kept (they map to SQL ``NULL``),
    which is how nullable columns like ``folder_id`` / ``parent_id`` are set.
    """
    return {key: _encode_value(val) for key, val in payload.items()}
