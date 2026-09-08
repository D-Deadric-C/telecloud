# `telecloud.auth` — JWT authentication backed by Supabase

JWT-based auth for TeleCloud (SPEC.md §3, §6.4). Owns JWT issue/verify against the
Supabase JWT secret, the signup/login/logout routes, and the two dependencies
other modules use to protect their routes. **Password handling is delegated to
Supabase** — no home-grown hashing or credential store.

Depends only on `config`, `shared`, and `database`.

## Public surface

```python
from telecloud.auth import current_user, require_verified, router
```

| Symbol | Kind | Purpose |
|--------|------|---------|
| `current_user` | FastAPI dependency | Resolve the bearer token to `shared.UserContext`, else `TeleCloudError("unauthorized", 401)` |
| `require_verified` | FastAPI dependency | Builds on `current_user`; `TeleCloudError("forbidden", 403)` if `email_verified` is false |
| `router` | `APIRouter` | `POST /auth/signup`, `POST /auth/login`, `POST /auth/logout` |

Exported for wiring/tests: `verify_token`, `encode_token`, `SupabaseAuth`,
`AuthSession`, `access_token` (raw-token dependency), `close` (shutdown hook).

### Protecting a route

```python
from fastapi import Depends
from telecloud.auth import current_user, require_verified
from telecloud.shared import UserContext

@some_router.get("/files")
async def list_files(user: UserContext = Depends(current_user)):
    ...

@some_router.post("/files")          # quota-gated routes require verification
async def upload(user: UserContext = Depends(require_verified)):
    ...   # quota *numbers* are enforced by quota/, not here
```

## How auth works

1. **Login/signup** call Supabase GoTrue (`sign_in_with_password` / `sign_up`).
   Supabase verifies the password and **issues** the access + refresh tokens; we
   return them in `SessionResponse`. We never mint our own auth tokens in
   production.
2. **`verify_supabase_token`** validates an incoming access token, routing on its
   `alg` header: `ES256`/`RS256` (modern Supabase default) are verified with the
   project's public key from the JWKS endpoint
   (`{supabase_url}/auth/v1/.well-known/jwks.json`, fetched + cached by `kid`);
   `HS256` (legacy shared secret) is verified with `config`'s `SUPABASE_JWT_SECRET`.
   Requires `sub` and `exp` with `aud = "authenticated"`. Every failure becomes
   `unauthorized` (401). (`verify_token` is the HS256-only sync helper used by
   tests / `encode_token`.)
3. **`current_user`** verifies the token, then loads the user's `profiles` row via
   the RLS-scoped `database.get_db(token)` client. The profile is the
   **authoritative** source of `email_verified` (the tier gate can flip after a
   token is issued), so it is read fresh rather than trusted from the JWT.
4. **`require_verified`** layers a 403 on top when `email_verified` is false.

`encode_token` is the symmetric issue helper (same secret/alg). In production it
is unused for request auth — it exists for issue/verify symmetry and to let tests
mint tokens the dependencies accept without a live Supabase project.

## Boundaries (SPEC §6.4)

Does **not**: enforce quota numbers (`quota/`), send verification email
(`notifications/` via `users/`), run the verification flow or mint verification
tokens (`users/`), or manage files/folders. Reads env only through `config` and
the DB only through `database/` repos — never directly.

## Dependencies

```
pip install fastapi "PyJWT[crypto]" supabase httpx
#   PyJWT[crypto]: HS256 + ES256/RS256 verify (cryptography for asymmetric keys)
#   httpx: async JWKS fetch   |   supabase: GoTrue auth client
pip install pytest pytest-asyncio email-validator   # tests / EmailStr
```

## Contract notes (flagged, not changed)

Conforms to SPEC §6.4. Points where the frozen contracts were thin or overlapped,
surfaced here rather than diverged on silently:

1. **Profile shell on signup overlaps with `users/` (SPEC §6.5).** §6.4 (and this
   module's build prompt) says `auth/` ensures a `profiles` row exists with
   `email_verified=false` on signup; §6.5 says `users/` owns "profile creation on
   signup". Resolved per the §6.4 wording: `auth/` creates the **empty shell**
   only (idempotent — see `service._ensure_profile`); the verification **flow**
   (token issue, email, flipping the flag) stays in `users/`/`notifications/`. No
   shared contract changed. If `users/` later wants sole ownership of profile
   creation, that's a coordination point — flagged, not decided here.

2. **`email_verified` is read from `profiles`, not the JWT.** TeleCloud's tier gate
   (SPEC §3) is the app's own Resend-based flag stored in `profiles`, distinct from
   Supabase's email-confirmation state. So `current_user` does a profile read per
   request (fine at the ~10-user scale, SPEC §1) to stay authoritative.

3. **`current_user` passes the raw access token to `database.get_db`.** Per the
   `database/` README contract note, `get_db(user_jwt)` takes the token (RLS is
   honored by forwarding it to PostgREST) and `UserContext` carries no token field.
   The dependency therefore resolves the token itself and threads it into the DB
   client; no shared-model change is needed or made.

4. **Signup assumes Supabase email confirmation is *disabled*.** TeleCloud runs its
   own verification (SPEC §3), so the Supabase project must not require email
   confirmation; otherwise `sign_up` returns no session and we cannot create the
   profile shell under RLS. That case is surfaced as `internal_error` with a clear
   message rather than silently producing a half-registered user. This is a
   deployment/config expectation, flagged for whoever provisions the Supabase
   project. **Verified against the live project:** it currently has
   `mailer_autoconfirm = false` (confirmation ON) — so **the email provider's
   "Confirm email" toggle must be turned OFF** in the Supabase dashboard
   (Authentication → Providers → Email) for signup to work. Until then, signup
   returns `internal_error` by design.

5. **Token signing model: SPEC says HS256 secret; the live project uses ES256/JWKS.**
   SPEC §2/§6.1 describe verification "against the Supabase JWT secret" (the legacy
   HS256 shared-secret model). The actual project issues **user access tokens
   signed with `ES256`** via Supabase's asymmetric JWT signing keys, published at
   `/auth/v1/.well-known/jwks.json` (the `SUPABASE_JWT_SECRET` still HS256-signs the
   *API keys*, but not user tokens). HS256-only verification rejects every real
   login token. Rather than break against the real backend, `verify_supabase_token`
   handles **both** models, routing on the token's `alg` and using the JWKS public
   key for asymmetric tokens. This needs no shared-contract change: the JWKS URL is
   derived from the existing `config.supabase_url`, so `config/` was not touched. If
   the project owner prefers the literal SPEC model, they can switch the project to
   the legacy HS256 secret in the dashboard; the HS256 path already supports it.
   Flagged because it widens the §2/§6.1 assumption — surfaced, not silently
   diverged on.
