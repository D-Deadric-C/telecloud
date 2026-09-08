"""Supabase client factories and the thin :class:`Database` wrapper (SPEC §6.3).

Two async accessors are the public entry points to the data layer:

* :func:`get_db` — a **user-scoped** client. It carries the user's JWT so every
  query runs as that user and PostgreSQL Row-Level Security restricts rows to
  ``owner_id = auth.uid()`` (SPEC §4). This is what authenticated request
  handlers use.
* :func:`get_service_db` — a **service-role** client that BYPASSES RLS. It exists
  for exactly one sanctioned path: resolving a share by token on the public,
  unauthenticated download route (SPEC §4 RLS expectations, §6.13, §7.3). Do not
  use it anywhere else — reaching for it elsewhere defeats RLS.

Both return a :class:`Database`, a minimal wrapper exposing the PostgREST table
builder and ``rpc`` so the per-table repositories stay decoupled from the
concrete Supabase client type. Connection details come from
``config.get_settings()`` only (SPEC §5.2).

**Connection reuse (perf).** Constructing a fresh :class:`AsyncClient` per request
forces a new TCP+TLS handshake to Supabase PostgREST every time, which turned
single operations (open/create a folder) into multi-second round trips. Instead a
process-wide :class:`_ClientPool` keeps a set of long-lived clients whose httpx
keep-alive connections are reused across requests. Each :func:`get_db` **acquires**
one client, scopes it to the caller by setting the user's JWT for the duration of
the request, and **releases** it back to the pool on :meth:`Database.aclose`. A
pooled client is only ever handed to one request at a time, so mutating its bearer
token (``postgrest.auth``) never races another request's token.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from supabase import AsyncClient, create_async_client
from supabase.lib.client_options import AsyncClientOptions

from telecloud.config import get_settings


class Database:
    """Thin async handle over a Supabase :class:`AsyncClient`.

    Repositories receive a ``Database`` and call :meth:`table` / :meth:`rpc`;
    they never see the raw client, so swapping the backend (or a test double)
    touches only this module. :attr:`is_service_role` records whether this handle
    bypasses RLS, purely for clarity/asserts at call sites.

    ``_release`` is an optional callback invoked by :meth:`aclose`. Pooled
    user-scoped handles pass the pool's release hook (return the client for
    reuse); the service-role handle leaves it ``None`` so :meth:`aclose` closes
    the underlying connections outright.
    """

    def __init__(
        self,
        client: AsyncClient,
        *,
        is_service_role: bool,
        _release: Callable[[AsyncClient], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self.is_service_role = is_service_role
        self._release = _release

    def table(self, name: str) -> Any:
        """Return the PostgREST query builder for ``name`` (insert/select/...)."""
        return self._client.table(name)

    def rpc(self, fn: str, params: dict[str, Any]) -> Any:
        """Invoke a Postgres function via PostgREST RPC (atomic counters)."""
        return self._client.rpc(fn, params)

    async def aclose(self) -> None:
        """Release the handle: return it to the pool, or close it if unpooled.

        Pooled user-scoped handles are returned to :class:`_ClientPool` so their
        keep-alive connections survive for the next request. The service-role
        handle (no release hook) closes its PostgREST connections directly.
        """
        if self._release is not None:
            release, self._release = self._release, None
            await release(self._client)
        else:
            await self._client.postgrest.aclose()


def _client_options() -> AsyncClientOptions:
    # This is a server-side client: there is no browser session to persist and no
    # background token to refresh. Disabling both avoids spawning refresh tasks we'd
    # have to tear down. The per-user bearer token is applied per acquisition, not
    # here, so the same client can be reused across users.
    return AsyncClientOptions(auto_refresh_token=False, persist_session=False)


class _ClientPool:
    """A small acquire/release pool of long-lived Supabase clients.

    Clients are created lazily on a miss and, once released, kept in an idle list
    so their underlying httpx keep-alive connections are reused by the next
    request (eliminating the per-request TLS handshake). The idle list is capped
    at ``max_idle``; anything released beyond the cap is closed rather than
    hoarded. A client is only ever lent to one caller at a time, so applying a
    per-user JWT to it is race-free.
    """

    def __init__(self, url: str, key: str, *, max_idle: int = 32) -> None:
        self._url = url
        self._key = key
        self._max_idle = max_idle
        self._idle: list[AsyncClient] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> AsyncClient:
        async with self._lock:
            if self._idle:
                return self._idle.pop()
        # Create outside the lock: construction never touches the network here
        # (session is disabled), but keeping I/O-shaped calls off the lock avoids
        # serializing concurrent cold acquisitions.
        return await create_async_client(self._url, self._key, options=_client_options())

    async def release(self, client: AsyncClient) -> None:
        async with self._lock:
            if len(self._idle) < self._max_idle:
                self._idle.append(client)
                return
        # Pool is full — drop this one rather than leak connections.
        await client.postgrest.aclose()

    async def aclose(self) -> None:
        """Close every idle client and empty the pool (shutdown)."""
        async with self._lock:
            idle, self._idle = self._idle, []
        for client in idle:
            await client.postgrest.aclose()


# Process-wide user-client pool (anon key + per-request JWT). Created lazily and
# guarded against concurrent creation; closed by :func:`close_db_pool`.
_user_pool: _ClientPool | None = None
_user_pool_lock = asyncio.Lock()


async def _get_user_pool() -> _ClientPool:
    global _user_pool
    if _user_pool is None:
        async with _user_pool_lock:
            if _user_pool is None:  # re-check inside the lock
                settings = get_settings()
                _user_pool = _ClientPool(
                    settings.supabase_url, settings.supabase_anon_key
                )
    return _user_pool


async def warm_db_pool() -> None:
    """Create the pool and seed one idle client (startup warmup).

    Pre-constructing a client means the first request doesn't pay the client
    construction cost; the httpx keep-alive connection to PostgREST is still
    established lazily on that first query, then reused thereafter.
    """
    pool = await _get_user_pool()
    client = await pool.acquire()
    await pool.release(client)


async def close_db_pool() -> None:
    """Close and forget the shared user-client pool (shutdown/tests)."""
    global _user_pool
    if _user_pool is not None:
        await _user_pool.aclose()
        _user_pool = None


async def get_db(user_jwt: str) -> Database:
    """Return a user-scoped client that honors RLS via the user's JWT.

    ``user_jwt`` is the user's Supabase access token (the JWT minted by
    ``auth/``). It is forwarded as the PostgREST bearer token so the database
    resolves ``auth.uid()`` and applies the owner-scoped RLS policies (SPEC §4).

    The client is **borrowed from a process-wide pool** and exclusively held for
    the caller's request, then returned by :meth:`Database.aclose` (always call it,
    ideally in a ``finally``). Reusing the pooled client keeps httpx keep-alive
    connections warm, so repeat requests skip the TCP+TLS handshake to Supabase.
    Because a pooled client is lent to only one request at a time, setting its
    bearer token per acquisition never races another request's token.

    NOTE (contract): SPEC §6.3 writes this as ``get_db(user)``. The argument that
    actually scopes RLS is the user's JWT, so this takes the access-token string.
    ``auth.current_user`` yields a ``UserContext`` (no token field); callers pass
    the request's access token alongside it. See database/README.md "Contract
    notes".
    """
    pool = await _get_user_pool()
    client = await pool.acquire()
    # Forward the user's JWT to PostgREST → queries run as `authenticated` with
    # this user's claims, so RLS scopes every row to them. This is applied per
    # acquisition (the client is exclusively ours until released).
    client.postgrest.auth(user_jwt)
    return Database(client, is_service_role=False, _release=pool.release)


# The service-role client holds no per-user state, so a single instance is shared
# across the process (created lazily, guarded against concurrent creation).
_service_db: Database | None = None
_service_lock = asyncio.Lock()


async def get_service_db() -> Database:
    """Return the service-role client that BYPASSES RLS — sanctioned uses only.

    The service-role key authenticates as a privileged role for which RLS does
    not apply. The ONLY sanctioned use is the public share-download read path:
    resolving ``shares.token`` without a user JWT, then enforcing ``revoked`` /
    ``expires_at`` / ``download_limit`` in application code (SPEC §4, §6.13,
    §7.3). Using it for anything else silently defeats RLS — don't.

    The instance is cached for the process lifetime; call :func:`close_service_db`
    on shutdown to release its connections.
    """
    global _service_db
    if _service_db is None:
        async with _service_lock:
            if _service_db is None:  # re-check inside the lock
                settings = get_settings()
                client = await create_async_client(
                    settings.supabase_url,
                    settings.supabase_service_role_key,
                    options=_client_options(),
                )
                _service_db = Database(client, is_service_role=True)
    return _service_db


async def close_service_db() -> None:
    """Close and forget the cached service-role client (shutdown/tests)."""
    global _service_db
    if _service_db is not None:
        await _service_db.aclose()
        _service_db = None
