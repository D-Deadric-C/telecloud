# `telecloud.config` — settings layer

The single typed configuration layer for the whole app (SPEC.md §5.2, §6.1).
Everything else in TeleCloud reads config **only** through here; no other module
touches `os.environ`.

## Usage

```python
from telecloud.config import get_settings, CHUNK_SIZE

settings = get_settings()            # cached singleton; validated on first call
tokens = settings.telegram_bot_tokens   # list[str], the RR bot pool
channels = settings.telegram_channel_ids  # list[int]
n_chunks = -(-file_size // CHUNK_SIZE)    # constants imported directly
```

`get_settings()` is cached with `functools.lru_cache`, so the environment is read
and validated exactly once. If a required variable is missing or invalid it raises
`ConfigError` with a message naming each offending variable — the process fails
fast at startup instead of mid-request.

## Dependencies

- `pydantic` (v2)
- `pydantic-settings` (>= 2.2, for the `NoDecode` annotation)

```
pip install "pydantic-settings>=2.2"
```

This package depends on **no** other TeleCloud module (SPEC §6.1).

## Hard constants (not environment-configurable)

These are fixed by the design and exposed as module-level constants so they can
never drift per-deploy:

| Constant                    | Value          | Meaning |
|-----------------------------|----------------|---------|
| `CHUNK_SIZE`                | 18 MiB         | Fixed chunk size. Bound by Telegram's 20 MB `getFile` download cap (SPEC §1). |
| `QUOTA_UNVERIFIED_BYTES`    | 500 MiB        | Total storage for an unverified user (SPEC §3). |
| `MAX_FILE_SIZE_UNVERIFIED`  | 30 MiB         | Max single-file size for an unverified user (SPEC §3). |
| `QUOTA_VERIFIED_BYTES`      | `None`         | Sentinel: verified users have unlimited total storage. |
| `MAX_FILE_SIZE_VERIFIED`    | `None`         | Sentinel: verified users have no per-file cap. |

## Environment variables

All variables below are **required** unless marked optional. See
[`.env.example`](../../.env.example) (repo root) for a copy-paste template. Field
names map case-insensitively to the `UPPER_SNAKE_CASE` env var of the same name.

| Env var                      | Type        | Notes |
|------------------------------|-------------|-------|
| `TELEGRAM_BOT_TOKENS`        | list (csv)  | Round-robin bot pool. Comma-separated; ≥1 required. |
| `TELEGRAM_CHANNEL_IDS`       | list (csv)  | Storage channel ids (negative bigints). Comma-separated; ≥1 required. |
| `SUPABASE_URL`               | http(s) URL | Supabase project URL. |
| `SUPABASE_ANON_KEY`          | str         | Anon/public API key. |
| `SUPABASE_SERVICE_ROLE_KEY`  | str         | Service-role key (RLS bypass for sanctioned paths). |
| `SUPABASE_JWT_SECRET`        | str         | Secret for issuing/verifying session JWTs. |
| `UPSTASH_REDIS_REST_URL`     | http(s) URL | Upstash Redis REST endpoint. |
| `UPSTASH_REDIS_REST_TOKEN`   | str         | Upstash Redis REST token. |
| `RESEND_API_KEY`             | str         | *Deprecated/optional, unused.* Email verification is Supabase-managed now. |
| `RESEND_FROM_EMAIL`          | str         | *Deprecated/optional, unused.* See above. |
| `QSTASH_CURRENT_SIGNING_KEY` | str         | Current QStash signing key. |
| `QSTASH_NEXT_SIGNING_KEY`    | str         | Next QStash signing key (rotation). |
| `APP_BASE_URL`               | http(s) URL | Public base URL for verification/share links. No trailing slash. |
| `APP_ENV`                    | str         | *Optional.* Deploy label (default `development`). |

### Notes on parsing

- **Lists** (`TELEGRAM_BOT_TOKENS`, `TELEGRAM_CHANNEL_IDS`) are given as plain
  comma-separated strings and split into typed lists. They are **not** JSON.
- **URL** fields (`SUPABASE_URL`, `UPSTASH_REDIS_REST_URL`, `APP_BASE_URL`) must
  be absolute `http(s)` URLs; a trailing slash is stripped automatically.
