-- TeleCloud — migration 0002: Row-Level Security (SPEC.md §4 "RLS expectations")
--
-- RLS is ENABLED on every table. The default policy is owner-scoped: a user may
-- only see/modify rows where `owner_id = auth.uid()` (profiles keys on
-- `id = auth.uid()`). `auth.uid()` is Supabase's helper that reads the `sub`
-- claim of the request JWT, so these policies are enforced for the user-scoped
-- client (get_db) and BYPASSED for the service-role client (get_service_db).
--
-- The only sanctioned RLS bypass is the share-download read path, which resolves
-- a file by `shares.token` using the service-role connection and then enforces
-- revoked / expires_at / download_limit in application code (SPEC §4, §6.13,
-- §7.3). No policy is needed for that — the service role bypasses RLS entirely.

begin;

alter table public.profiles enable row level security;
alter table public.folders  enable row level security;
alter table public.files    enable row level security;
alter table public.chunks   enable row level security;
alter table public.shares   enable row level security;

-- profiles: the row whose id IS the caller --------------------------------
drop policy if exists profiles_select_own on public.profiles;
create policy profiles_select_own on public.profiles
    for select using (id = auth.uid());

drop policy if exists profiles_insert_self on public.profiles;
create policy profiles_insert_self on public.profiles
    for insert with check (id = auth.uid());

drop policy if exists profiles_update_own on public.profiles;
create policy profiles_update_own on public.profiles
    for update using (id = auth.uid()) with check (id = auth.uid());

-- folders: owner-scoped ----------------------------------------------------
drop policy if exists folders_select_own on public.folders;
create policy folders_select_own on public.folders
    for select using (owner_id = auth.uid());

drop policy if exists folders_insert_own on public.folders;
create policy folders_insert_own on public.folders
    for insert with check (owner_id = auth.uid());

drop policy if exists folders_update_own on public.folders;
create policy folders_update_own on public.folders
    for update using (owner_id = auth.uid()) with check (owner_id = auth.uid());

drop policy if exists folders_delete_own on public.folders;
create policy folders_delete_own on public.folders
    for delete using (owner_id = auth.uid());

-- files: owner-scoped ------------------------------------------------------
drop policy if exists files_select_own on public.files;
create policy files_select_own on public.files
    for select using (owner_id = auth.uid());

drop policy if exists files_insert_own on public.files;
create policy files_insert_own on public.files
    for insert with check (owner_id = auth.uid());

drop policy if exists files_update_own on public.files;
create policy files_update_own on public.files
    for update using (owner_id = auth.uid()) with check (owner_id = auth.uid());

drop policy if exists files_delete_own on public.files;
create policy files_delete_own on public.files
    for delete using (owner_id = auth.uid());

-- chunks: owner-scoped THROUGH the parent file. A chunk has no owner_id of
-- its own, so visibility derives from the file it belongs to.
drop policy if exists chunks_select_own on public.chunks;
create policy chunks_select_own on public.chunks
    for select using (
        exists (
            select 1 from public.files f
            where f.id = chunks.file_id and f.owner_id = auth.uid()
        )
    );

drop policy if exists chunks_insert_own on public.chunks;
create policy chunks_insert_own on public.chunks
    for insert with check (
        exists (
            select 1 from public.files f
            where f.id = chunks.file_id and f.owner_id = auth.uid()
        )
    );

drop policy if exists chunks_update_own on public.chunks;
create policy chunks_update_own on public.chunks
    for update using (
        exists (
            select 1 from public.files f
            where f.id = chunks.file_id and f.owner_id = auth.uid()
        )
    );

drop policy if exists chunks_delete_own on public.chunks;
create policy chunks_delete_own on public.chunks
    for delete using (
        exists (
            select 1 from public.files f
            where f.id = chunks.file_id and f.owner_id = auth.uid()
        )
    );

-- shares: owner-scoped (management paths). The public token download path does
-- NOT rely on these policies — it uses the service-role client (SPEC §7.3).
drop policy if exists shares_select_own on public.shares;
create policy shares_select_own on public.shares
    for select using (owner_id = auth.uid());

drop policy if exists shares_insert_own on public.shares;
create policy shares_insert_own on public.shares
    for insert with check (owner_id = auth.uid());

drop policy if exists shares_update_own on public.shares;
create policy shares_update_own on public.shares
    for update using (owner_id = auth.uid()) with check (owner_id = auth.uid());

drop policy if exists shares_delete_own on public.shares;
create policy shares_delete_own on public.shares
    for delete using (owner_id = auth.uid());

commit;
