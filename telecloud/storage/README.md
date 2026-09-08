# `telecloud.storage` — the chunking engine

Splits an upload stream into fixed **18 MiB** chunks driving the two-phase commit
(SPEC.md §7.1) and streams downloads back in order, range-aware (SPEC §7.2). It
yields **bytes + metadata** straight to/from Telegram with **no disk buffering**
(SPEC §1). It does **not** check quota, do auth, or build HTTP responses — `files/`
wraps it and owns those concerns (SPEC §6.9). It talks to Telegram only through the
`telegram/` module.

Depends only on `config`, `shared`, `database`, and `telegram` (SPEC §6.9).

## Public surface

```python
from telecloud.storage import (
    store_upload, open_download, DownloadResponse, ByteRange, parse_range,
)
```

### `store_upload(db, file_meta, stream, *, transport=telegram, chunk_size=CHUNK_SIZE) -> FileMeta`

Given the `pending` `files` row `files/` already created (SPEC §7.1 step 3) and an
async byte `stream`, it:

1. re-chunks `stream` into exact 18 MiB pieces (last may be smaller, SPEC §1) —
   buffering at most ~one chunk plus the current inbound piece, never the whole file;
2. for each piece: `telegram.send_document(None, piece)` → records a `pending`
   `chunks` row with the returned `message_id` / `telegram_file_id` / `bot_id` /
   `channel_id` (channel chosen by the bot pool, SPEC §4.4, §6.8);
3. once **all** chunks land, commits: `chunks_repo.mark_all_committed` then
   `files_repo.mark_committed`, returning the now-`committed` `FileMeta`.

On a mid-stream failure the exception propagates and the file is **left `pending`**
for the `jobs/` orphan sweeper (SPEC §7.1 step 6, §7.4) — `store_upload` does not
roll back.

### `open_download(db, file_id, range_=None, *, transport=telegram, chunk_size=CHUNK_SIZE) -> DownloadResponse`

Streams a committed file. With no range it streams chunks `0..n-1` whole (a `200`).
With a range (`"bytes=start-end"` string or a `ByteRange`) it uses the shared
chunk-math (`locate_byte`) to find the start chunk + intra-chunk offset, skips the
offset, and stitches bytes across chunk boundaries until `end` inclusive (a `206`).

`DownloadResponse` is plain data — an async byte iterator plus the framing metadata
`files/` needs:

| field / prop | meaning |
|--------------|---------|
| `stream` | the async byte iterator to send |
| `size_bytes` | total file size |
| `content_length` | bytes this response yields (full size, or range length) |
| `is_partial` / `status_code` | `True`/`206` for a range, else `False`/`200` |
| `content_range` | `"bytes start-end/total"` for a range, else `None` |
| `mime_type` | the file's content type |
| `headers` | convenience dict: `Content-Type`, `Content-Length`, `Accept-Ranges`, and `Content-Range` when partial |

`files/` builds the actual FastAPI `Response`, picks 200 vs 206, and adds
`Content-Disposition` (storage stays out of response-building, SPEC §6.9).

Guards: `not_found` if the file is missing or soft-deleted; `upload_incomplete`
(HTTP 409) if it is still `pending`; `validation_error` (HTTP 416 unsatisfiable /
422 malformed) for a bad range.

## Tests

`pytest telecloud/storage` — covers chunk count, the pending→committed transition,
"leave it pending on failure", full download, a single-chunk range, and a range
**spanning chunk boundaries** (the tricky case). Telegram and `database` are faked
in-memory (`tests/_fakes.py`); a tiny `chunk_size` makes the byte math exact.

## Flagged contract notes (flagged, not changed)

Conforms to SPEC §6.9. Three places where the frozen contracts were thin — surfaced
here rather than diverged on silently:

1. **The public functions take a `db` argument.** SPEC §6.9 writes
   `store_upload(file_meta, stream)` / `open_download(file_id, range)`. But storage
   reaches `database/` through repositories that require a `Database` handle, and it
   cannot mint one itself — `get_db(user_jwt)` needs the user's JWT, an `auth`/`files`
   concern (and `get_service_db()` is reserved for the share path, SPEC §4, §7.3). So
   `files/` (and `sharing/`) pass their request-scoped `db` in as the first argument.
   This mirrors `database/`'s own flagged `get_db(user_jwt)` decision. No shared model
   changes; if a tokened `UserContext` is ever introduced, this could revisit.

2. **The §7.1 commit is sequential, not one DB transaction — and quota is not here.**
   SPEC §7.1 step 5 says "in one transaction: set all chunks `committed`, set the file
   `committed`, and `quota.add_usage(user, size)`." Two obstacles make a single atomic
   step impossible from `storage/` today:
   - The `database/` layer exposes no multi-statement transaction primitive — only
     per-table PostgREST calls (`chunks_repo.mark_all_committed`,
     `files_repo.mark_committed`) and `rpc`. So the two flips are two round-trips.
   - `quota.add_usage` lives in `quota/` and is invoked by `files/`, not `storage/`
     (SPEC §6.9 forbids storage touching quota). The "one transaction" therefore spans
     three modules and cannot be a single DB transaction without restructuring.

   storage does the safest thing available: it flips **chunks first, the file last**,
   so the file row — the commit marker — only becomes visible once its chunks are
   committed. A crash between the two flips leaves the file `pending` (chunks
   committed), which the orphan sweeper reclaims and the client retries — no
   corruption, no half-visible file.

   **To honor §7.1 literally**, add a Postgres function (e.g. `commit_file(file_id)`)
   to `database/` that flips both rows in one server-side transaction, exposed via
   `Database.rpc`; and have `files/` call `quota.add_usage` immediately after a
   successful commit. Both are changes to **`database/` and `files/`**, out of scope
   for this module — flagged for those owners.

3. **No `range_not_satisfiable` error code.** SPEC §5.1's reserved codes have no entry
   for HTTP 416. An unsatisfiable range raises `TeleCloudError(validation_error, …,
   416)` — correct status, reused code. If a dedicated `range_not_satisfiable` code is
   wanted, add it to `shared/ErrorCode` + `DEFAULT_STATUS`; flagged for that owner.
```
