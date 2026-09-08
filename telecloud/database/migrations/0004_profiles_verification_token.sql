-- TeleCloud — migration 0004: profiles email-verification token (SPEC.md §4.1)
--
-- Forward migration that brings databases which already applied the original
-- 0001 (created before the verification-token columns were part of the schema
-- file) up to the FROZEN SPEC §4.1 shape. `create table if not exists` in 0001
-- is a no-op on an existing table, so the columns must be added with ALTER here.
--
-- Idempotent: `add column if not exists` / `create index if not exists`, so this
-- is safe to run on a fresh database (where 0001 already created the columns) and
-- safe to re-run. The columns are owned by the `users/` verification flow
-- (SPEC §6.5); this migration only shapes the table.

begin;

alter table public.profiles
    add column if not exists verification_token text;

alter table public.profiles
    add column if not exists verification_token_expires_at timestamptz;

-- One pending verification token at a time; NULLs are unconstrained so any
-- number of profiles may have no token. (SPEC §4.1 profiles_verification_token_key)
create unique index if not exists profiles_verification_token_key
    on public.profiles (verification_token)
    where verification_token is not null;

commit;
