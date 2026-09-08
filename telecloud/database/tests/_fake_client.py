"""An in-memory fake of the PostgREST query surface for repository tests.

It mimics just the slice of the Supabase async builder the repositories use —
``table(name).insert/select/update/delete`` with ``eq``/``lt``/``is_``/``order``/
``limit`` filters and an awaitable ``execute()`` returning an object with
``.data`` — plus ``rpc(fn, params).execute()`` for the two atomic counters. It is
NOT a Postgres engine: no RLS, no constraints. It exists so the repos' query
construction and model mapping can be unit-tested without a live database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from telecloud.database.client import Database


@dataclass
class _Response:
    data: Any
    count: int | None = None


# Per-table default columns filled in on insert (the DB defaults from SPEC §4
# that PostgREST would return in the inserted representation).
def _defaults(table: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    base: dict[str, Any] = {"id": str(uuid4()), "created_at": now}
    if table == "profiles":
        # profiles' PK is the caller-supplied id, not generated.
        base.pop("id")
        base.update(
            storage_used_bytes=0,
            email_verified=False,
            verification_token=None,
            verification_token_expires_at=None,
        )
    elif table == "folders":
        base.update(deleted_at=None, parent_id=None)
    elif table == "files":
        base.update(
            deleted_at=None,
            folder_id=None,
            mime_type="application/octet-stream",
            status="pending",
        )
    elif table == "chunks":
        base.update(status="pending")
    elif table == "shares":
        base.update(
            download_count=0,
            revoked=False,
            expires_at=None,
            download_limit=None,
        )
    return base


class _Query:
    """A chainable, awaitable query over one table's in-memory row list."""

    def __init__(self, table: str, store: dict[str, list[dict[str, Any]]]):
        self._table = table
        self._store = store
        self._op: str = "select"
        self._payload: Any = None
        self._filters: list[tuple[str, str, Any]] = []
        self._order: str | None = None
        self._limit: int | None = None

    # -- operation builders -------------------------------------------------
    def insert(self, payload: dict[str, Any]) -> "_Query":
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload: dict[str, Any]) -> "_Query":
        self._op, self._payload = "update", payload
        return self

    def delete(self) -> "_Query":
        self._op = "delete"
        return self

    def select(self, *_cols: str) -> "_Query":
        self._op = "select"
        return self

    # -- filters / modifiers ------------------------------------------------
    def eq(self, col: str, val: Any) -> "_Query":
        self._filters.append(("eq", col, val))
        return self

    def lt(self, col: str, val: Any) -> "_Query":
        self._filters.append(("lt", col, val))
        return self

    def is_(self, col: str, val: Any) -> "_Query":
        self._filters.append(("is", col, val))
        return self

    def order(self, col: str) -> "_Query":
        self._order = col
        return self

    def limit(self, n: int) -> "_Query":
        self._limit = n
        return self

    # -- evaluation ---------------------------------------------------------
    def _match(self, row: dict[str, Any]) -> bool:
        for kind, col, val in self._filters:
            cell = row.get(col)
            if kind == "eq" and str(cell) != str(val):
                return False
            if kind == "is" and val == "null" and cell is not None:
                return False
            if kind == "lt" and not (cell is not None and cell < val):
                return False
        return True

    def _rows(self) -> list[dict[str, Any]]:
        return self._store.setdefault(self._table, [])

    async def execute(self) -> _Response:
        rows = self._rows()
        if self._op == "insert":
            new = {**_defaults(self._table), **self._payload}
            rows.append(new)
            return _Response([dict(new)])

        matched = [r for r in rows if self._match(r)]

        if self._op == "update":
            for r in matched:
                r.update(self._payload)
        elif self._op == "delete":
            for r in matched:
                rows.remove(r)
            return _Response([])

        if self._order:
            # Sort None-last without coercing 0/"" to a falsy default (which
            # would break ordering by chunk_index where 0 is valid).
            col = self._order
            matched.sort(key=lambda r: (r.get(col) is None, r.get(col)))
        if self._limit is not None:
            matched = matched[: self._limit]
        return _Response([dict(r) for r in matched])


class _Rpc:
    def __init__(self, fn: str, params: dict[str, Any],
                 store: dict[str, list[dict[str, Any]]]):
        self._fn, self._params, self._store = fn, params, store

    async def execute(self) -> _Response:
        if self._fn == "adjust_storage_used":
            owner, delta = self._params["p_owner"], self._params["p_delta"]
            for r in self._store.get("profiles", []):
                if str(r["id"]) == str(owner):
                    r["storage_used_bytes"] += delta
                    return _Response(r["storage_used_bytes"])
            return _Response(None)
        if self._fn == "increment_share_download":
            sid = self._params["p_share_id"]
            for r in self._store.get("shares", []):
                if str(r["id"]) == str(sid):
                    r["download_count"] += 1
                    return _Response(r["download_count"])
            return _Response(None)
        raise AssertionError(f"unknown rpc {self._fn}")


class FakeDatabase(Database):
    """A :class:`Database` stand-in backed by in-memory dict tables."""

    def __init__(self) -> None:  # bypass Database.__init__ (no real client)
        self.store: dict[str, list[dict[str, Any]]] = {}
        self.is_service_role = False

    def table(self, name: str) -> _Query:  # type: ignore[override]
        return _Query(name, self.store)

    def rpc(self, fn: str, params: dict[str, Any]) -> _Rpc:  # type: ignore[override]
        return _Rpc(fn, params, self.store)
