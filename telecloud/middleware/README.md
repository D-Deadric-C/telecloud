# `middleware/` — cross-cutting request pipeline (SPEC §6.7)

The plumbing every request passes through, registered on the FastAPI app at
startup. It owns four concerns and **no feature logic**:

| Concern        | What it does                                                                 |
|----------------|------------------------------------------------------------------------------|
| Error handling | `TeleCloudError` → SPEC §5.1 envelope; anything else → generic `internal_error` 500 |
| Rate limiting  | per-user / per-IP request limit via `rate_limit.limiter`; over-limit → `rate_limited` 429 |
| CORS           | allowed frontend origin(s) from `config`                                     |
| Request logging| one minimal structured line: method, path, status, duration                  |

Dependencies are exactly the four allowed by SPEC §6.7: `config`, `shared`,
`auth`, `rate_limit`. No DB, no Telegram, no feature modules.

## Public surface

```python
from telecloud.middleware import register_middleware

register_middleware(app)            # wire the whole pipeline (call once at startup)
```

`register_middleware(app, *, rate_limit=…, rate_window_seconds=…, limiter_check=…,
cors_origins=…, settings=…)` returns the same `app`. The keyword arguments let a
deploy tune the request budget, inject a limiter backend (tests/wiring), or
override CORS origins without editing this package.

Also exported for finer control / tests: `register_error_handlers`,
`render_error`, `telecloud_error_handler`, `unhandled_exception_handler`,
`RateLimitMiddleware`, `resolve_key`, `register_cors`, `resolve_cors_origins`,
`RequestLoggingMiddleware`, and the `DEFAULT_REQUEST_LIMIT` /
`DEFAULT_WINDOW_SECONDS` constants.

## How it behaves

- **Error envelope.** Every `TeleCloudError` is rendered as
  `{"error": {"code", "message"}}` with the status it carries (`from_code` fills
  the conventional status per `shared.DEFAULT_STATUS`). Unexpected exceptions are
  logged *with traceback* for operators but returned to the client as a generic
  `internal_error` 500 — the real cause is never leaked.
- **Rate limiting.** Each request is bucketed by `user:<id>` when a valid bearer
  token is present (verified the same way `auth.current_user` verifies it) or
  `ip:<addr>` otherwise (honoring `X-Forwarded-For` behind Fly.io's proxy). The
  middleware emits the 429 **directly** rather than raising, because it sits
  outside the routing layer where the exception handler applies. If the limiter
  backend is unreachable it **fails open** (logs a warning, lets the request
  through) so a Redis outage degrades fairness rather than denying everything.
- **Logging.** One `INFO` line on `telecloud.middleware.request` per request, with
  structured `extra` fields (`http_method`, `http_path`, `http_status`,
  `duration_ms`). Only the *path* is logged — never the query string (it can
  carry share/verification tokens) and never request headers (the bearer token
  lives there).

## Stack ordering

Starlette wraps each `add_middleware` call outside the previous one. The final
order, outermost → innermost, is:

```
CORS → request logging → rate limit → [exception handlers → router]
```

so CORS headers land on every response (including errors), the log line times the
whole request and sees the final status (including a 429), and the limiter runs
before any route work. Exception handlers live in Starlette's own
`ExceptionMiddleware` / `ServerErrorMiddleware`, so route-raised errors render
correctly regardless of the middleware order.

## Contract notes (flagged, not silently diverged — per SPEC top matter)

1. **CORS origins have no dedicated `config` field.** SPEC §6.7 says CORS is
   "configured from `config`," but `config/settings.py` exposes no
   `cors_allowed_origins` (and `Settings` uses `extra="ignore"`, so an undeclared
   env var could not be read). This module therefore derives the allowed origin
   from the existing `Settings.app_base_url` (the public app URL the single-file
   frontend is served from, SPEC §2). **If the frontend is ever hosted on a
   different origin than the API, `config/` should gain an explicit
   `cors_allowed_origins: list[str]`** — that is a `config/` change and is
   *flagged here, not made*. In the meantime, pass `cors_origins=` to
   `register_middleware` to override.

2. **Rate-limit budget is a `middleware/` policy, not `config`.** There is no
   config field for the request limit/window, so sensible defaults live here
   (`DEFAULT_REQUEST_LIMIT = 120` per `DEFAULT_WINDOW_SECONDS = 60`s per key —
   deliberately generous for the ~10-user scale, SPEC §1) and are overridable via
   `register_middleware`. No shared contract is touched.

3. **Per-request user keying re-verifies the token.** To key the limiter by user
   *before* routing, the middleware verifies the bearer token itself (via
   `auth.verify_supabase_token`), duplicating the verification `current_user`
   does during routing. For a ~10-user system this is negligible and keeps the
   limiter fair per identity; an invalid token simply falls back to IP keying
   (it is rejected later by `auth`). No shared contract is touched.

## Tests

`tests/` covers the two required guarantees and the supporting behaviour:

- `test_errors.py` — the §5.1 envelope for both a `TeleCloudError` route and an
  unexpected exception (and that the real cause does not leak).
- `test_rate_limit.py` — the `rate_limited` 429 path, independent buckets per
  key, fail-open on backend error, and key resolution (token → user, else IP).
- `test_logging.py` — the structured fields, 500 logging, and no token/query leak.
- `test_registration.py` — the full pipeline wired via `register_middleware`
  (envelope + 429 + CORS header) with the limiter backend injected.

Run: `python -m pytest telecloud/middleware/tests/ -q`
