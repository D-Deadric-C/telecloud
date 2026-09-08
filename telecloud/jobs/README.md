# `telecloud.jobs` — async cleanup via QStash

QStash-triggered cleanup that keeps storage consistent without blocking requests
(SPEC.md §6.14, §7.4). Two idempotent, bounded jobs plus the enqueue helper
`files/` uses.

- **Orphan sweep** — reclaim abandoned `pending` uploads (a two-phase commit that
  never finished, SPEC §7.1 step 6): delete every chunk's Telegram message, then
  the chunk + file rows.
- **Deferred delete** — finish soft-deletes: for files in `deleting` (already
  marked, and **quota already decremented**, by `files/`, SPEC §6.12), delete the
  Telegram messages, then the rows. Quota is **not** touched here (SPEC §6.14).

Every job verifies the QStash signature before doing anything, processes at most a
bounded batch and returns, and is safe to re-run.

## Public surface

```python
from telecloud import jobs

jobs.router                       # FastAPI APIRouter: the two QStash job endpoints
jobs.register(publisher=None)     # wire the deferred-delete enqueuer into files/
await jobs.sweep_orphans()        # the orphan-sweep job
await jobs.delete_deferred()      # the deferred-delete job
await jobs.process_retries()      # drain the retry queue (bounded), dead-letter at cap
jobs.verify_qstash_signature(sig, body, keys)   # the signature gate
```

### Endpoints (`router`)

| Method & path             | Auth          | Purpose                                  |
|---------------------------|---------------|------------------------------------------|
| `POST /jobs/sweep-orphans`  | QStash sig  | Run the orphan sweep.                    |
| `POST /jobs/deferred-delete`| QStash sig  | Run the deferred delete.                 |

Both reject any request without a valid `Upstash-Signature` with `401
unauthorized` **before** touching the DB or Telegram (SPEC §6.14 "Must NOT").

## Signature verification (SPEC §6.14)

QStash signs each delivery with a short-lived `HS256` JWT in the
`Upstash-Signature` header. We verify against the **current** then **next** signing
key from `config` (so a key rotation never drops a request, SPEC §6.1) and check:

- the JWT signature (against either key),
- `exp` / `nbf` (validity window),
- `iss == "Upstash"`,
- `body == base64url(sha256(raw_body))` — binds the signature to *this* body, so a
  captured signature can't be replayed against a different payload.

`sub` (destination URL) checking is **optional** (off by default): reverse proxies
rewrite host/scheme, so a strict match causes false rejections. Callers that can
reconstruct the exact public URL may pass `url=` to enforce it.

## Retry & dead-letter (SPEC §6.6, §7.4)

A chunk's Telegram message is deleted inline. On a **transient** failure
(rate-limit / `429` / `5xx` / network) the delete is enqueued on
`rate_limit.queue` as a **self-contained descriptor** (it carries `channel_id` /
`message_id` / `bot_id`, so finishing it needs no DB row). That lets the file's
rows be removed in a single pass while the queue finishes the straggler. A
**permanent** failure (e.g. the message is already gone) is tolerated and treated
as done — retrying can't help, and blocking row removal forever would re-sweep the
file every run.

`process_retries` drains a bounded batch of queued retries each run, replaying each
delete and handing failures to `queue.mark_failed`, which re-enqueues until the op
has been attempted `max_attempts` times and then parks it in the **dead-letter**
list — so a permanently-failing op is retired instead of cycling forever. Each job
run drains retries first, then does its sweep.

## Boundaries (SPEC §6.14)

Depends only on the §6.14 set — `config`, `shared`, `database`, `telegram`,
`rate_limit` — plus `files.ports` for the registration seam (see flag below). It
reads rows via `database` repos and moves bytes via `telegram`; it never reaches
into bot or Redis internals, and it never touches `quota`.

## Flagged contract notes

1. **`config` lacks QStash *publish* credentials.** SPEC §6.1 says `config` owns
   "QStash URLs + keys", but the built `config` exposes only the two *signing* keys
   (the verify side) — there is no publish token/URL. On-demand triggering of the
   deferred-delete job (publishing a one-off QStash message when `files/`
   soft-deletes) therefore can't be wired without **changing the `config`
   contract**, so per the build rules it is **flagged, not made**. Until those
   credentials exist, the registered enqueuer is **sweep-backed**: `files/` already
   persists `status='deleting'` (the durable signal `find_deleting` reads), and the
   scheduled deferred-delete job reclaims it — exactly the backstop
   `files/ports.py` documents. The `QStashPublisher` seam is in place so on-demand
   triggering can be added later with **no change to `files/`**, just a real
   publisher passed to `jobs.register`.

2. **Importing `files.ports`.** SPEC §6.14's dependency list does not name `files/`,
   but `files/ports.py` deliberately inverts the dependency (mirroring
   `folders/ports.py`): it defines the `DeletionEnqueuer` port and asks `jobs/` to
   register a concrete implementation via `set_deletion_enqueuer`. `jobs.register`
   does exactly that. The enqueuer signature `async (file_id: UUID) -> None` matches
   the port — the reconciliation `files/ports.py` asked for when `jobs/` was built.

## App composition

The app root should, at startup: `app.include_router(jobs.router)`,
`jobs.register()` (wire the deferred-delete enqueuer into `files/`), and schedule
the two endpoints in QStash. None of that lives here — `jobs/` only provides the
router and the registration helper.
