# `telecloud.files` — file lifecycle & orchestration

The orchestrator (SPEC.md §6.12). It ties `quota` + `storage` + `folders` together
and is the **only file-domain module that builds FastAPI responses**. It owns the
upload two-phase commit (SPEC §7.1), the range-aware authenticated download (SPEC
§7.2), list / rename / move, and the public **soft-delete** entrypoint (SPEC §6.12,
§7.4). Every operation is owner-scoped and RLS-enforced via the user's JWT (SPEC
§6.3); each loaded row is re-checked against `user.id` as defense-in-depth (the
in-memory test fake has no RLS).

## Public surface

```python
from telecloud import files

await files.upload_file(user, access_token=tok, name="m.mp4",
                        size_bytes=512, stream=req.stream(), folder_id=None,
                        mime_type="video/mp4")          # -> committed FileMeta
await files.open_file_download(user, file_id, access_token=tok,
                               range_="bytes=0-99")     # -> (DownloadResponse, name)
await files.list_files(user, access_token=tok, folder_id=None)   # -> [FileMeta]
await files.rename_file(user, file_id, access_token=tok, name="new")
await files.move_file(user, file_id, access_token=tok, new_folder_id=dest)
await files.soft_delete_file(user, file_id, access_token=tok)    # deletion entrypoint
files.validate_file_name("report.pdf")                 # -> trimmed name
files.router                                           # FastAPI APIRouter
```

### Endpoints (`router`)

| Method & path              | Auth   | Purpose                                             |
|----------------------------|--------|-----------------------------------------------------|
| `POST   /files`            | bearer | Upload (body = stream; `?name=`, `?folder_id=`).    |
| `GET    /files`            | bearer | List committed files in a folder (`?folder_id=`).   |
| `GET    /files/{id}`       | bearer | Download, range-aware (`Range:` → `206`).           |
| `PATCH  /files/{id}`       | bearer | Rename (`{ "name": ... }`).                          |
| `POST   /files/{id}/move`  | bearer | Move (`{ "folder_id": ... \| null }`).               |
| `DELETE /files/{id}`       | bearer | Soft-delete (mark `deleting`, decrement quota, job). |

Files the caller doesn't own (or that are soft-deleted / not `committed`) return
`not_found` (404) — never `forbidden` — so the API leaks nothing about other users'
files. Names are validated by `validate_file_name`: non-empty after trimming,
≤ 255 chars, no path separators, no control chars, not `.`/`..` (`validation_error`,
422).

### Upload (SPEC §7.1)

The request **body is the raw file stream** (no disk buffering, SPEC §1), so the
parameters travel out-of-band: `name` + optional `folder_id` as query params, the
**declared size** as `Content-Length` (required — quota must reject *before* the
stream is read), and the MIME type as `Content-Type`. The service order is exactly
SPEC §7.1: validate folder → `quota.check_can_upload` → create `pending` row →
`storage.store_upload` (which chunks to Telegram and commits the file + chunks) →
`quota.add_usage`. **`files/` owns the quota calls; `storage/` owns the chunk/file
commit** — the boundary SPEC §7.1 step 5 splits between them. `add_usage` runs only
after `store_upload` returns *committed*, so a mid-upload failure (file left
`pending` for the `jobs/` sweeper, SPEC §7.1 step 6) never inflates usage.

### Download with Range (SPEC §7.2)

`open_file_download` re-checks ownership, then calls `storage.open_download`, which
yields a `DownloadResponse` (byte iterator + framing: 200-vs-206, `Content-Length` /
`Content-Range` / `Accept-Ranges`). `storage` does **not** build the response — the
router reflects that framing onto the wire and adds `Content-Disposition` (a
`files/` concern, SPEC §6.9). The request-scoped DB is closed before streaming; the
returned stream reads only from Telegram, never the DB.

## Boundaries (SPEC §6.12)

- **Does NOT** talk to Telegram directly — always via `storage`/`telegram`.
- **Does NOT** reimplement quota math or chunking — delegates to `quota`/`storage`.
- Dependencies: `config` (transitively), `shared`, `database`, `auth` (router),
  `quota`, `storage`, `folders`.

## ⚠️ Flagged contract tension — the deferred-deletion enqueuer (`jobs/`)

**Soft-delete must enqueue a Telegram-deletion job, but `jobs/` is module 14 — not
built yet — and `files/`'s frozen dependency set (SPEC §6.12) does not list
`jobs/`.** Per SPEC §6.12 the soft-delete must *not* delete Telegram messages
inline; per SPEC §7.4 the actual removal is a deferred `jobs/` job. We therefore
cannot import a `jobs/` enqueue helper here (it doesn't exist, and the dependency
isn't sanctioned).

**Resolution chosen (dependency inversion, flagged here)** — symmetric to how
`folders/` inverts its dependency on `files/` (see `folders/ports.py`): `files/`
defines the port it needs — `DeletionEnqueuer` in [`ports.py`](ports.py) — and the
soft-delete resolves it via `get_deletion_enqueuer()`, which **raises
`internal_error` until `jobs/` registers a concrete enqueuer** with
`files.set_deletion_enqueuer(...)` at app composition (module 14). The enqueuer is
resolved *before* any state change, so an unwired `jobs/` fails fast without leaving
a half-deleted file. The SPEC §7.4 `find_deleting` sweep is the independent backstop:
a file left in `deleting` is reclaimed by the job sweeper even if an enqueue is lost.

**Action for module 14 (`jobs/`):**
1. Implement the deferred file-deletion job (delete Telegram messages for `deleting`
   files, then their rows, SPEC §7.4) and an **enqueue helper**.
2. Register it via `files.set_deletion_enqueuer(...)` during startup/composition.
3. **Reconcile the signature** against `DeletionEnqueuer`:
   `async def (file_id: UUID) -> None`. If `jobs/`'s enqueue helper differs (e.g.
   needs more than `file_id`), adapt at registration (a thin wrapper) or raise the
   mismatch as a contract change.

### ℹ️ Reconciled contract — the `folders/` file-deletion seam (resolved)

`folders/` (module 11) flagged that its cascade needs `files/`'s deletion entrypoint
and defined the `folders.ports.FileDeleter` port for it
(`async def (user, file_id, *, access_token) -> None`). This module's
`soft_delete_file` **matches that signature exactly**, so importing
`telecloud.files` registers it directly via `folders.set_file_deleter(...)` (in
[`__init__.py`](__init__.py)) — no adapter needed. `files/` depends on `folders/`
(SPEC §6.12), so this import direction is legal; `folders/` never imports `files/`.

### ℹ️ Note: declared size vs. streamed bytes (minor)

`chunk_count` and the quota charge use the **declared** `Content-Length`. `storage`
chunks whatever the stream actually yields; for this portfolio scope (SPEC §1, ~10
users) the declared size is treated as authoritative. A future hardening could
reconcile the committed file's `size_bytes` against the bytes `storage` actually
stored.

## Tests

```
python -m pytest telecloud/files/tests/ -q
```

- `test_service.py` — upload happy path (mock `quota`/`storage`), the quota-reject
  short-circuit, folder-ownership rejection, list/rename/move ownership paths, and
  soft-delete (marks `deleting`, decrements quota, enqueues the job; fails fast and
  mutates nothing when the enqueuer is unwired).
- `test_router.py` — route wiring + HTTP framing: `201` upload, `200` full download,
  **`206` ranged download with `Content-Range`/`Accept-Ranges`/`Content-Length`**,
  `204` delete, rename/move.
- `test_ports.py` — the flagged seam: `get_deletion_enqueuer` raises `internal_error`
  until an enqueuer is registered, then returns it.

The DB is the in-memory `FakeDatabase` running the real `files_repo` /
`folders_repo`; no network or live Supabase is touched.
