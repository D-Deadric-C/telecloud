# `telecloud.quota` — verification-based quota enforcement

Owns the quota rules from SPEC.md §3 (§6.10): the pre-upload gate and the
`profiles.storage_used_bytes` accounting that keeps usage accurate on commit /
delete. This is **pure policy + bookkeeping** — it never moves bytes, never talks
to Telegram, and never builds HTTP responses. The `files/` orchestrator calls in
here *before* handing the transfer to `storage/` (SPEC §7.1).

## Tiers (SPEC §3)

| State        | Total quota   | Max file size | Code on reject                 |
|--------------|---------------|---------------|--------------------------------|
| Unverified   | **500 MiB**   | **30 MiB**    | `quota_exceeded` / `file_too_large` |
| Verified     | **Unlimited** | **No cap**    | — (always allowed)             |

The numbers are **not** hardcoded here. They come from `config`
(`QUOTA_UNVERIFIED_BYTES`, `MAX_FILE_SIZE_UNVERIFIED`; verified = `None`
sentinel), so this module can never drift from the canonical limits.

## Public surface

```python
from telecloud import quota

await quota.check_can_upload(user, size_bytes, access_token=token)  # raise or allow
await quota.add_usage(user, delta, access_token=token)             # -> new total (commit)
await quota.subtract_usage(user, delta, access_token=token)        # -> new total (delete)
quota.evaluate_upload(verified=..., current_usage=..., size_bytes=...)  # pure decision
```

- **`check_can_upload`** reads the **fresh** verification flag via
  `users.get_profile` and (for unverified users) current usage via the
  `database` profiles repo, then applies `evaluate_upload`. Per-file cap is
  checked before the total cap, so an oversized file is `file_too_large`
  regardless of remaining quota. Verified users skip the usage read entirely.
- **`add_usage` / `subtract_usage`** mutate `storage_used_bytes` through
  `profiles_repo.adjust_storage_used` (an atomic SQL `UPDATE`, migration 0003).
  `delta` is a non-negative magnitude (negative raises `ValueError`).
  `subtract_usage` floors the stored value at **zero**.

## Boundaries (SPEC §6.10)

- **Does NOT** move bytes, touch Telegram, or build HTTP responses.
- **Does NOT** reimplement the verification handshake — it reads the flag **via**
  `users` (that's why `users` is a dependency).
- Dependencies are exactly the §6.10 set: `config`, `shared`, `database`, `users`.

## Design notes

### No-negative floor lives here, not in SQL
`adjust_storage_used` only performs the atomic mutation; migration 0003's own
comment and SPEC §6.3 put the *business rule* (never go negative) in `quota/`.
`subtract_usage` applies the atomic decrement, and if accounting drift would have
produced a negative balance it adds back exactly the observed deficit with a
second atomic update — correcting the underflow without a separate read.

## Contract notes

- **`access_token` argument.** SPEC §6.10 writes these as `check_can_upload(user,
  size)` / `add_usage(user, delta)` / `subtract_usage(user, delta)`. Every profile
  read/write is RLS-scoped to `auth.uid()`, which needs the caller's JWT, so —
  exactly like `database.get_db(user_jwt)` and `users.get_profile(user,
  access_token=...)` — each function also takes the request's `access_token`. This
  follows the established convention (see `database/README.md` "Contract notes");
  it is **not** a change to a shared contract.
- **Reads the fresh flag, not the token.** `check_can_upload` re-reads
  `email_verified` from the profile rather than trusting `user.email_verified`, so
  a just-verified user is not held to the unverified limits (SPEC §3: enforce
  against the profile). No shared contract is touched.

## Tests

```
python -m pytest telecloud/quota/tests/ -q
```

`test_policy.py` covers the pure decision (per-file cap, total cap, under-both,
verified unlimited, boundary equality). `test_service.py` covers the four
`check_can_upload` outcomes plus `add`/`subtract` correctness, the no-negative
floor, negative-delta rejection, and the fresh-flag read — against the in-memory
`FakeDatabase` running the real `profiles_repo` (no network or live Supabase).
