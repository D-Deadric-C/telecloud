-- TeleCloud — migration 0001: core schema (SPEC.md §4, FROZEN)
--
-- Tables, enums, indexes, and foreign keys exactly as specified in SPEC §4.
-- Row-Level Security policies live in 0002_rls.sql; atomic helper functions in
-- 0003_functions.sql. Run the files in numeric order (see migrations/README.md).
--
-- All app tables live in the `public` schema. Supabase Auth owns `auth.users`.
-- Every id is uuid (default gen_random_uuid()) unless noted; every timestamp is
-- timestamptz defaulting to now(); soft deletes use a nullable deleted_at.

begin;

-- 4.1 profiles: one row per auth user --------------------------------------
create table if not exists public.profiles (
    id                             uuid primary key references auth.users (id) on delete cascade,
    email                          text not null,
    email_verified                 boolean not null default false,
    storage_used_bytes             bigint not null default 0,
    verification_token             text,           -- pending email-verification token (users/)
    verification_token_expires_at  timestamptz,    -- absolute expiry for that token
    created_at                     timestamptz not null default now()
);
-- One pending verification token at a time; NULLs are unconstrained so any
-- number of profiles may have no token. (SPEC §4.1 profiles_verification_token_key)
create unique index if not exists profiles_verification_token_key
    on public.profiles (verification_token)
    where verification_token is not null;

-- 4.2 folders: virtual hierarchy (adjacency list) --------------------------
create table if not exists public.folders (
    id          uuid primary key default gen_random_uuid(),
    owner_id    uuid not null references public.profiles (id) on delete cascade,
    parent_id   uuid references public.folders (id) on delete cascade,
    name        text not null,
    created_at  timestamptz not null default now(),
    deleted_at  timestamptz
);
create index if not exists folders_owner_parent_idx
    on public.folders (owner_id, parent_id);

-- 4.3 files: metadata + lifecycle status -----------------------------------
do $$
begin
    if not exists (select 1 from pg_type where typname = 'file_status') then
        create type public.file_status as enum ('pending', 'committed', 'deleting');
    end if;
end
$$;

create table if not exists public.files (
    id           uuid primary key default gen_random_uuid(),
    owner_id     uuid not null references public.profiles (id) on delete cascade,
    folder_id    uuid references public.folders (id) on delete set null,
    name         text not null,
    size_bytes   bigint not null,
    mime_type    text not null default 'application/octet-stream',
    chunk_count  int not null,
    status       public.file_status not null default 'pending',
    created_at   timestamptz not null default now(),
    deleted_at   timestamptz
);
create index if not exists files_owner_folder_idx
    on public.files (owner_id, folder_id);
create index if not exists files_status_idx
    on public.files (status);

-- 4.4 chunks: one row per 18 MiB piece, channel-aware ----------------------
do $$
begin
    if not exists (select 1 from pg_type where typname = 'chunk_status') then
        create type public.chunk_status as enum ('pending', 'committed');
    end if;
end
$$;

create table if not exists public.chunks (
    id                uuid primary key default gen_random_uuid(),
    file_id           uuid not null references public.files (id) on delete cascade,
    chunk_index       int not null,            -- 0-based, ordered
    size_bytes        int not null,            -- last chunk may be < CHUNK_SIZE
    channel_id        bigint not null,         -- Telegram channel the chunk lives in
    message_id        bigint not null,         -- Telegram message id (getFile/delete)
    telegram_file_id  text not null,           -- file_id returned by sendDocument
    bot_id            text not null,           -- which bot uploaded it (RR pool key)
    status            public.chunk_status not null default 'pending',
    created_at        timestamptz not null default now(),
    unique (file_id, chunk_index)
);
create index if not exists chunks_file_idx
    on public.chunks (file_id);

-- 4.5 shares: public URL access to a file ----------------------------------
create table if not exists public.shares (
    id              uuid primary key default gen_random_uuid(),
    file_id         uuid not null references public.files (id) on delete cascade,
    owner_id        uuid not null references public.profiles (id) on delete cascade,
    token           text not null unique,      -- unguessable, URL-safe
    expires_at      timestamptz,               -- null = never expires
    download_limit  int,                       -- null = unlimited
    download_count  int not null default 0,
    revoked         boolean not null default false,
    created_at      timestamptz not null default now()
);
create index if not exists shares_token_idx
    on public.shares (token);

commit;
