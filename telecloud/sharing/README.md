# `telecloud.sharing` — public URL access (SPEC.md §6.13)

Public, URL-based file sharing. Two surfaces with **different trust models**:

- **Authed management** (owner-scoped, RLS via the caller's JWT): create a public
  link for one of the caller's committed files, list a file's links, revoke a link.
- **Public download** (`GET /s/{token}`, **no auth**): resolve the share via the
  **service-role** client — the single sanctioned RLS bypass (SPEC §4, §7.3) —
  enforce `revoked` / `expires_at` / `download_limit`, bump the counter, then reuse
  `storage.open_download` to stream (range-aware). Nothing about the owner is
  exposed (SPEC §6.13).

## Public surface

```python
from telecloud import sharing

await sharing.create_share(user, access_token=tok, file_id=fid,
                           expires_at=None, download_limit=None)  # -> ShareMeta
await sharing.list_shares(user, access_token=tok, file_id=fid)    # -> [ShareMeta]
await sharing.revoke_share(user, share_id, access_token=tok)      # -> ShareMeta (soft)
await sharing.open_share_download(token, range_="bytes=0-99")     # -> (DownloadResponse, name)

sharing.router          # authed APIRouter, prefix /shares
sharing.public_router   # unauthenticated APIRouter, prefix /s
```

### Endpoints

| Method & path                 | Auth   | Purpose                                            |
|-------------------------------|--------|----------------------------------------------------|
| `POST   /shares`              | bearer | Create a link (`{ file_id, expires_at?, download_limit? }`). |
| `GET    /shares?file_id=`     | bearer | List a file's links (incl. revoked).               |
| `POST   /shares/{id}/revoke`  | bearer | Soft-revoke a link (`revoked=true`).               |
| `GET    /s/{token}`           | **none** | Public download, range-aware (`Range:` → `206`). |

`ShareResponse` (the management response shape) deliberately **omits `owner_id`**;
the public route returns only the file's bytes, content type, and name. No email or
user id is ever read or returned on the public path (SPEC §6.13).

## Design decisions (SPEC §6.13 left these open)

- **Default expiry: none.** A share never expires unless the creator sets
  `expires_at`. When set it must be **timezone-aware and in the future**
  (`validation_error` otherwise). `null` = never expires (the SPEC §4.5 column
  default).
- **Default download limit: none (unlimited).** An explicit `download_limit` must be
  a **positive integer** and is enforced as `download_count >= limit` (so `limit=1`
  permits exactly one download). `null` = unlimited.
- **Revocation is soft** (`revoked=true`). The row is kept so a later download
  attempt is answered with the specific `share_revoked` (HTTP 410) rather than an
  indistinguishable `not_found`, and so a management UI can show revoked history.
  Hard delete would lose that signal; a separate purge (e.g. a `jobs/` sweep) can
  reclaim old revoked rows later if desired.

## Download gating & counting (SPEC §7.3)

On `GET /s/{token}` the gates are applied in this order — first failure wins:

1. `revoked` → `share_revoked` (410)
2. `expires_at < now()` → `share_expired` (410)
3. `download_limit` set and `download_count >= download_limit` → `forbidden` (403)

On success the counter is incremented **before** streaming (SPEC §7.3) via the
atomic `increment_share_download` RPC. Two notes, acceptable at this scale
(~10 users) and documented rather than over-engineered:

- The limit check and the increment are not a single transaction, so highly
  concurrent requests could momentarily over-count past the limit by a small
  amount. The count itself is always correct (atomic UPDATE).
- Every successful resolve counts as one download, **including ranged/resumable
  requests** — a multi-range resumable download consumes multiple counts. SPEC §7.3
  specifies "increment and stream" without distinguishing ranges, so we keep it
  simple.

## Boundaries (SPEC §6.13)

- Never bypasses the `revoked` / `expiry` / `limit` checks.
- Uses the service-role client **only** to resolve the token and stream the shared
  file — nothing else.
- Never reimplements chunk streaming: the byte path is `storage.open_download`
  wholesale (range mapping, 200-vs-206 framing). The route replicates only the tiny
  `Content-Disposition` helper from `files/`'s router (a `files/` concern per SPEC
  §6.9) to avoid importing across a module boundary.

## Flagged / contract notes

- **`files/` dependency unused.** SPEC §6.13 lists `files` among the allowed
  dependencies, but `sharing/` does not import it. The only thing it needs from the
  file domain is an *owner + committed* check on a `files` row, which it performs at
  the `database.files_repo` layer exactly as `files/` does internally
  (`files/`'s `_load_owned_file` is private and not exported). Using fewer of the
  allowed dependencies breaks no contract. If a shared "load my file" helper is
  later exported from `files/`, this check can route through it instead.
- **No cross-file "list all my shares".** `list_shares` is scoped to one `file_id`
  because `database.shares_repo` exposes `list_for_file` but not a
  `list_for_owner`. A future "all my links" view would need a
  `shares_repo.list_for_owner(owner_id)` added in `database/` (a shared-contract
  change — flagged here, not made).

## Tests

`tests/test_service.py` covers the create→download happy path, revoked/expired/
over-limit rejections, no-owner-leak on the return value, and the create/revoke/list
ownership checks (real `files_repo`/`shares_repo` over the in-memory `FakeDatabase`;
`storage.open_download` stubbed at the seam). `tests/test_router.py` and
`tests/test_public.py` cover HTTP framing for the authed and public routers
respectively, including that `owner_id`/email never appear on the wire.
