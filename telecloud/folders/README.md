# `telecloud.folders` — virtual folder hierarchy

Owns the adjacency-list folder tree (`public.folders`, SPEC.md §4.2, §6.11): a
per-user hierarchy modeled by `parent_id` (root = `parent_id IS NULL`).
Single-user-per-account — folders are never shared. Every operation is
owner-scoped and enforced by Row-Level Security via the user's JWT (SPEC §6.3);
each loaded row is re-checked against `user.id` as defense-in-depth (the in-memory
test fake has no RLS).

## Public surface

```python
from telecloud import folders

await folders.create_folder(user, access_token=tok, name="Photos", parent_id=None)
await folders.list_contents(user, access_token=tok, folder_id=None)   # -> (subfolders, files)
await folders.rename_folder(user, fid, access_token=tok, name="New")
await folders.move_folder(user, fid, access_token=tok, new_parent_id=other)
await folders.soft_delete_folder(user, fid, access_token=tok, delete_file=deleter)
folders.validate_name("Photos")                                       # -> trimmed name
folders.router                                                        # FastAPI APIRouter
```

### Endpoints (`router`)

| Method & path                 | Auth   | Purpose                                          |
|-------------------------------|--------|--------------------------------------------------|
| `POST   /folders`             | bearer | Create a folder (optional `parent_id`).          |
| `GET    /folders`             | bearer | List root contents (subfolders + files).         |
| `GET    /folders/{id}`        | bearer | List one folder's contents.                      |
| `PATCH  /folders/{id}`        | bearer | Rename (`{ "name": ... }`).                       |
| `POST   /folders/{id}/move`   | bearer | Re-parent (`{ "new_parent_id": ... | null }`).   |
| `DELETE /folders/{id}`        | bearer | Soft-delete, cascading to descendants + files.   |

Folders the caller doesn't own (or that are already soft-deleted) return
`not_found` (404) — never `forbidden` — so the API leaks nothing about other
users' folders. Names are validated by `validate_name`: non-empty after trimming,
≤ 255 chars, no path separators (`/`, `\`), no control characters, not `.`/`..`.
Invalid names and cycle-forming moves return `validation_error` (422).

### Cascading soft-delete

`soft_delete_folder` collects the target plus every live descendant folder
(breadth-first over `folders_repo.list_children`, which already excludes deleted
rows), hands each contained **committed** file to `files/`'s deletion path, then
stamps `deleted_at` on each folder. Files are handed off *before* their folder is
marked deleted, so an interrupted run never strands a file under a
deleted-but-still-listed folder. This module **does not** delete Telegram messages
or rows directly — that is deferred to a `jobs/` deletion job (SPEC §7.4) and the
quota decrement is owned by `files/` (SPEC §6.12).

### Cycle rejection (move)

A move into the folder itself, or into any of its descendants, is rejected.
Detection walks the ancestor chain *upward* from the proposed new parent; if the
folder being moved appears among those ancestors, the move would create a cycle. A
visited set bounds the walk even against pre-existing malformed data.

## Boundaries (SPEC §6.11)

- **Does NOT** manage file bytes, talk to Telegram/storage, or compute quota.
- Reads files only to *list* and *enumerate* folder contents (via
  `database.files_repo`, the data layer) — it never writes file rows.
- Dependencies: `config` (transitively), `shared`, `database`, `auth` (router only).

## ⚠️ Flagged contract tension — file-deletion entrypoint

**The cascade needs `files/`, which is not built yet (this is module 11; `files/`
is module 13).** Per SPEC §6.12, `files/` depends on `folders/`, so `folders/`
**cannot import `files/`** — that would be a circular dependency. The cross-module
build note instructs: *call the deletion through `files/`'s public service; if that
interface isn't defined yet, FLAG it rather than reaching into the `files` tables
directly.*

**Resolution chosen (dependency inversion, flagged here):** `folders/` defines the
port it needs — `FileDeleter` in [`ports.py`](ports.py) — and the service accepts a
`delete_file` callable (injected). The router resolves it via `get_file_deleter`,
which **raises `internal_error` until `files/` registers a concrete deleter** with
`folders.set_file_deleter(...)` at app composition (module 13). Nothing reaches
into the `files` tables to mark `deleting` itself, which would skip the quota
decrement and the Telegram-deletion job that `files/` owns.

**Action for module 13 (`files/`):**
1. Implement the single-file soft-delete (mark `deleting` → decrement quota →
   enqueue the Telegram-deletion job, SPEC §6.12, §7.4).
2. Register it via `folders.set_file_deleter(...)` during startup/composition.
3. **Reconcile the signature** against `FileDeleter`:
   `async def (user: UserContext, file_id: UUID, *, access_token: str) -> None`.
   If `files/`'s actual entrypoint differs, adapt at registration (a thin wrapper)
   or raise the mismatch as a contract change.

### ℹ️ Note: router depends on `auth` (minor)

The authed routes use `auth.current_user` / `auth.access_token`, so `router.py`
imports `auth`. SPEC §6.11 lists `auth` in the dependency set, so this is expected
and build-order-safe (`auth` is module 5 and never imports `folders/`). The service
layer itself stays free of `auth`.

## Tests

```
python -m pytest telecloud/folders/tests/ -q
```

- `test_service.py` — create under a parent, ownership `not_found` paths, listing,
  rename, move (to parent / to root / **cycle rejection** into self & descendant),
  and **cascading soft-delete** marking all descendants + handing their files to
  the deleter (and leaving sibling subtrees untouched).
- `test_router.py` — route wiring with the service stubbed and `current_user`
  overridden, including the injected file-deletion port on `DELETE`.
- `test_ports.py` — the flagged seam: `get_file_deleter` raises `internal_error`
  until a deleter is registered, then returns it.

The DB is the in-memory `FakeDatabase` running the real `folders_repo` /
`files_repo`; no network or live Supabase is touched.
