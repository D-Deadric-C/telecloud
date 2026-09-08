-- TeleCloud — migration 0004: Supabase-managed email verification
--
-- Switches email verification from the old custom Resend/token flow to
-- Supabase's built-in email confirmation. Supabase owns the token, the email,
-- and the confirmation link; the only thing the app needs is for
-- `public.profiles.email_verified` (the authoritative tier gate, SPEC §3) to
-- track `auth.users.email_confirmed_at`.
--
-- Two consequences for this schema:
--   1. Profiles can no longer be created by app code at signup, because with
--      "Confirm email" enabled Supabase returns NO session at signup (no JWT to
--      satisfy the RLS insert). So a trigger creates the profile shell when the
--      auth user is inserted, the standard Supabase pattern.
--   2. A trigger mirrors `email_confirmed_at` onto `profiles.email_verified`, so
--      every existing reader (auth.current_user / require_verified) keeps working
--      unchanged — the column just becomes correct on its own.
--
-- The old per-profile verification token columns + index are dropped (the custom
-- flow they backed is gone).
--
-- Triggers live on `auth.users`, which is owned by Supabase. Run this in the
-- Supabase SQL editor (the `postgres` role can create triggers there). The
-- functions are SECURITY DEFINER so they may write `public.profiles` regardless
-- of the inserting/updating role and bypass RLS for this controlled sync.

begin;

-- 1. Auto-create the profile shell when a new auth user is inserted ----------
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.profiles (id, email, email_verified)
    values (
        new.id,
        coalesce(new.email, ''),
        new.email_confirmed_at is not null
    )
    on conflict (id) do nothing;
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

-- 2. Mirror email confirmation onto profiles.email_verified -----------------
create or replace function public.sync_email_verified()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    -- Fire only on the transition to confirmed (idempotent, avoids needless writes).
    if new.email_confirmed_at is not null
       and (old.email_confirmed_at is null
            or old.email_confirmed_at is distinct from new.email_confirmed_at) then
        update public.profiles
           set email_verified = true
         where id = new.id;
    end if;
    return new;
end;
$$;

drop trigger if exists on_auth_user_confirmed on auth.users;
create trigger on_auth_user_confirmed
    after update of email_confirmed_at on auth.users
    for each row execute function public.sync_email_verified();

-- 3. Backfill any existing users (e.g. confirmed before this migration) ------
update public.profiles p
   set email_verified = true
  from auth.users u
 where u.id = p.id
   and u.email_confirmed_at is not null
   and p.email_verified = false;

-- 4. Drop the now-unused custom-verification token columns ------------------
drop index if exists public.profiles_verification_token_key;
alter table public.profiles
    drop column if exists verification_token,
    drop column if exists verification_token_expires_at;

commit;
