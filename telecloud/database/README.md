# `telecloud.database` — Supabase access layer

The data-access layer for TeleCloud (SPEC.md §6.3): client factories, the SQL
migrations (§4), and one repository per table. It returns the shared read models
from `telecloud.shared` — never raw rows — and contains **no business rules**
(quota math, two-phase orchestration, auth, Telegram). Those live in `quota/`,
`storage/`, `files/`, `auth/`, and `telegram/`.

Depends only on `telecloud.config` and `telecloud.shared`.

## Public surface

```python
from telecloud.database import (
    get_db, get_service_db, close_service_db, Database,
    profiles_repo, folders_repo, files_repo, chunks_repo, shares_repo,
)
```

### Clients

| Accessor | Key | RLS | Use |
|----------|-----|-----|-----|
| `await get_db(user_jwt)`   | anon key + user JWT | **enforced** (`owner_id = auth.uid()`) | every authenticated request |
| `await get_service_db()`   | service-role key    | **bypassed** | share-download read path **only** (SPEC §4, §7.3) |

`get_db` creates a fresh client per call and forwards the user's JWT to PostgREST,
so the database resolves `auth.uid()` and applies the owner-scoped policies. Close
it with `await db.aclose()` when the request ends.

`get_service_db` returns a cached, process-wide service-role client. **It bypasses
RLS** — the only sanctioned use is resolving a share by token on the public,
unauthenticated download route, after which `sharing/` enforces
`revoked` / `expires_at` / `download_limit` in code. Don't reach for it anywhere
else; doing so silently defeats RLS. Call `await close_service_db()` on shutdown.

### Repositories

Each is a namespace of `async` functions taking a `Database` first and returning
shared models. Highlights (operations the §6/§7 contracts imply):

- **`profiles_repo`** — `insert`, `get`, `get_storage_used`, `adjust_storage_used`
  (atomic ±delta on commit/delete). The `email_verified` flag is kept in sync from
  `auth.users` by a DB trigger (migration `0005`), so there are no
  verification-token primitives here anymore (verification is Supabase-managed —
  SPEC §6.4).
- **`folders_repo`** — `insert`, `get`, `list_children`, `rename`, `move`,
  `soft_delete`.
- **`files_repo`** — `insert_pending`, `get`, `list_in_folder`, `mark_committed`,
  `mark_deleting` (soft-delete), `rename`, `move`, `find_pending_older_than`
  (orphan sweep), `find_deleting` (deferred delete), `delete_row`.
- **`chunks_repo`** — `insert_pending`, `list_for_file` (ordered),
  `get_by_index` (Range download), `mark_all_committed`, `delete_for_file`.
- **`shares_repo`** — `insert`, `get`, `list_for_file`, `resolve_by_token`
  (service-role path), `increment_download_count` (atomic), `revoke`.

```python
db = await get_db(user_jwt)
f  = await files_repo.insert_pending(db, owner_id=user.id, name="a.bin",
                                     size_bytes=size, chunk_count=n)
# ... upload chunks ...
await chunks_repo.mark_all_committed(db, f.id)
await files_repo.mark_committed(db, f.id)
```

The two-phase **orchestration** (when to flip statuses, in what transaction) is
`storage/`/`files/`'s job; this layer only provides the atomic primitives.

## Migrations

SQL lives in [`migrations/`](migrations/) and implements SPEC §4 exactly. See
[`migrations/README.md`](migrations/README.md) for how to run them against
Supabase (SQL Editor, `psql`, or the Supabase CLI) and how to verify RLS.

## Dependencies

```
pip install "supabase>=2.0"      # PostgREST async client used by the factories
pip install pytest-asyncio       # tests only
```

## Contract notes (flagged, not changed)

This module conforms to SPEC §6.3. Two points where the frozen contracts were
ambiguous or thin — surfaced here rather than diverged on silently:

1. **`get_db(user)` takes the user's JWT, not a `UserContext`.** RLS is honored by
   forwarding the user's *access token* to PostgREST, and `UserContext` (the
   shared model `auth.current_user` yields) carries no token field. So the
   factory signature is `get_db(user_jwt: str)`. If a future change wants
   `get_db(user: UserContext)`, `UserContext` in `shared/` would need a token
   field — a shared-model change to coordinate there, out of scope here.

2. **No `ProfileMeta` shared model.** SPEC §6.2 lists no profile read model, yet
   `profiles` carries `storage_used_bytes` that `quota/`/`users/` need.
   `profiles_repo` returns `UserContext` for identity and exposes the usage column
   as a scalar `int` (`get_storage_used`, `adjust_storage_used`) rather than
   inventing a model. If a richer profile read model is wanted later, add
   `ProfileMeta` to `shared/` — flagged for that owner, not added here.

3. **Email verification is Supabase-managed (no token columns).** Verification was
   migrated off the custom token flow to Supabase's built-in confirmation (SPEC
   §6.4 amendment). Migration `0005` adds triggers on `auth.users` that create the
   `profiles` shell on signup and mirror `email_confirmed_at` onto
   `profiles.email_verified`, and drops the old `verification_token` columns. The
   repo therefore exposes no verification-token primitives.

None of these block downstream modules; all are noted so the next session doesn't
mistake them for drift.
