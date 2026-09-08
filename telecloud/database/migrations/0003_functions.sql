-- TeleCloud — migration 0003: atomic helper functions
--
-- PostgREST cannot express `col = col + delta` in a single request, so the two
-- read-modify-write counters in the data model are done server-side as atomic
-- UPDATEs exposed via RPC. The *business rules* (quota limits, share expiry) are
-- enforced by the quota/ and sharing/ modules — these functions only perform the
-- atomic mutation the data layer needs (SPEC §6.3: data access, no business
-- rules).
--
-- Both are SECURITY INVOKER (the default): they run with the caller's role and
-- privileges, so Row-Level Security still applies. Under the user-scoped client
-- a user can therefore only adjust their own profile; under the service-role
-- client (share download path) RLS is bypassed as designed.

begin;

-- Atomically add `p_delta` (which may be negative) to a profile's
-- storage_used_bytes and return the new value. Used by quota.add_usage /
-- quota.subtract_usage on commit / delete (SPEC §3, §7.1). RLS on profiles
-- restricts the row a user-scoped caller can touch to their own.
create or replace function public.adjust_storage_used(
    p_owner uuid,
    p_delta bigint
)
returns bigint
language sql
volatile
as $$
    update public.profiles
       set storage_used_bytes = storage_used_bytes + p_delta
     where id = p_owner
    returning storage_used_bytes;
$$;

-- Atomically increment a share's download_count and return the new count. Used
-- by the public share-download path, which runs under the service-role client
-- (SPEC §7.3). Returns NULL if no share with that id exists.
create or replace function public.increment_share_download(
    p_share_id uuid
)
returns int
language sql
volatile
as $$
    update public.shares
       set download_count = download_count + 1
     where id = p_share_id
    returning download_count;
$$;

commit;
