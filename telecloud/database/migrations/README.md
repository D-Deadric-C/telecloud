# TeleCloud migrations

Ordered SQL implementing the frozen data model (SPEC.md §4). Apply them in
numeric order — each is wrapped in its own transaction and is idempotent
(`create … if not exists`, `create or replace`, guarded enum creation), so a
re-run is safe.

| File | Purpose |
|------|---------|
| `0001_schema.sql`    | Enums (`file_status`, `chunk_status`), tables (`profiles`, `folders`, `files`, `chunks`, `shares`), indexes, FKs — SPEC §4. |
| `0002_rls.sql`       | Enables Row-Level Security on every table and the owner-scoped policies — SPEC §4 "RLS expectations". |
| `0003_functions.sql` | `adjust_storage_used` and `increment_share_download` — the two atomic counters PostgREST can't express in a single request. |
| `0004_profiles_verification_token.sql` | *(superseded by 0005)* Added `profiles.verification_token` + `verification_token_expires_at` and their unique partial index for the old custom Resend verification flow. |
| `0005_supabase_email_verification.sql` | Switches verification to **Supabase built-in email confirmation**: trigger to auto-create the `profiles` shell on `auth.users` insert, trigger to mirror `auth.users.email_confirmed_at` → `profiles.email_verified`, backfill, and drops the now-unused `verification_token` columns. Run in the Supabase SQL editor (creates triggers on `auth.users`). |

## Prerequisites

These run against a **Supabase** Postgres database, which already provides
`auth.users`, the `authenticated`/`service_role` roles, the `auth.uid()` helper,
and the `pgcrypto` `gen_random_uuid()` function. No extra extensions are needed.

## Running them

Pick whichever matches your workflow — all three apply the same files in order.

### Supabase SQL Editor (quickest)
Open the project → **SQL Editor**, paste the contents of `0001`, run; then `0002`;
then `0003`. (Order matters: policies and functions reference the tables.)

### psql against the project's connection string
Get the connection string from **Project Settings → Database → Connection string**
(use the direct, non-pooler URL for DDL):

```bash
export DATABASE_URL='postgresql://postgres:<password>@db.<ref>.supabase.co:5432/postgres'
for f in 0001_schema.sql 0002_rls.sql 0003_functions.sql 0004_profiles_verification_token.sql 0005_supabase_email_verification.sql; do
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "telecloud/database/migrations/$f"
done
```

### Supabase CLI
Copy these files into the project's `supabase/migrations/` (keeping the numeric
prefixes) and run `supabase db push`, or for local dev `supabase db reset`.

## Verifying

After applying, confirm RLS is on and the policies exist:

```sql
select relname, relrowsecurity from pg_class
 where relname in ('profiles','folders','files','chunks','shares');
-- relrowsecurity should be true for all five

select tablename, policyname from pg_policies where schemaname = 'public'
 order by tablename, policyname;
```

A quick functional check (run as an authenticated user) that a user only sees
their own rows: insert a file as user A, then `select * from files` as user B —
it should return nothing.
