# `telecloud.users` — profile state

Reads the user profile and exposes verification status for other modules (e.g.
`quota/`) to read off the `profiles` row (SPEC.md §3, §6.5).

> **Email verification is Supabase-managed.** Supabase sends the confirmation
> email and owns the link; a DB trigger mirrors `auth.users.email_confirmed_at`
> onto `profiles.email_verified` (migration `0005`). This module no longer issues
> or redeems verification tokens, builds links, or sends mail. Re-sending the
> confirmation email lives in `auth/` (`POST /auth/resend-confirmation`), because
> an unconfirmed user has no session to authenticate a `users/` route.

## Public surface

```python
from telecloud import users

await users.get_profile(user, access_token=token)   # -> UserContext (read via repo)
users.router                                         # FastAPI APIRouter
```

### Endpoints (`router`)

| Method & path     | Auth   | Purpose                             |
|-------------------|--------|-------------------------------------|
| `GET  /users/me`  | bearer | Caller's profile (fresh repo read). |

## Boundaries (SPEC §6.5)

- **Does NOT** compute or store quota usage math — that's `quota/`. This module
  holds only the `email_verified` flag (kept in sync by the DB trigger).
- **Does NOT** run a verification flow or send email — Supabase does.
- Service-layer dependencies are `config`, `shared`, `database`.

### ℹ️ Note: router depends on `auth` (minor)

The authed route uses `auth.current_user` / `auth.access_token`, so `router.py`
imports `auth` even though §6.5 lists the service dependency set as
config/shared/database. This is a build-order-safe **composition** dependency
(`auth` is already built and never imports `users`); the **service layer stays
free of `auth`**.

## Tests

```
python -m pytest telecloud/users/tests/ -q
```

`test_service.py` covers `get_profile` (fresh read + not-found). `test_router.py`
checks `/users/me` wiring with the service stubbed and `current_user` overridden.
The DB is the in-memory `FakeDatabase` running the real `profiles_repo`; no
network or live Supabase is touched.
