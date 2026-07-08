-- Claude Usage Tracker — Supabase account-pool schema + RLS (AS DEPLOYED).
-- Source of truth for the deployed state of project sxciunvkygtehhztfjjo (2026-07-08).
-- Apply top-to-bottom to a FRESH Supabase project created with **"Automatically expose new
-- tables" ON** (the tables rely on Supabase's default DML grant to `authenticated`; §5 then
-- revokes that default for FUTURE tables). Design + security review: docs/SUPABASE-MIGRATION.md.
--
-- Deviations from the original design, baked in after live verification:
--   * is_admin is SECURITY INVOKER (only called from connector_tokens policies, never a
--     profiles policy -> no RLS recursion; DEFINER just tripped the exposed-DEFINER lint);
--   * every function's EXECUTE is revoked from anon/authenticated EXPLICITLY (Supabase grants
--     them independently of PUBLIC, so `revoke ... from public` alone is a no-op) — §5;
--   * capture_finals derives teams from `distinct usage.team` + takes an optional p_now (§7).
-- Verified: Security Advisor clean; authenticated cannot EXECUTE the connector RPCs or CREATE
-- in public; real OTP signup stamps app_metadata.team/role into the JWT.

-- ============================================================================
-- 1. TABLES
-- ============================================================================
create table public.profiles (
  user_id           uuid primary key references auth.users(id) on delete cascade,
  email             text not null,
  username          text not null,
  team              text not null,                    -- = app_metadata.team; server-forced
  is_admin          boolean not null default false,   -- forced from app_metadata.role
  active_device_id  text,                             -- soft device-lock (not a boundary)
  device_claimed_at timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create unique index profiles_team_username_key on public.profiles (team, username);
create index profiles_team_idx on public.profiles (team);

create table public.accounts (
  team text not null, acct text not null,
  display_name text, org text,
  created_at timestamptz not null default now(),
  primary key (team, acct)
);
create index accounts_team_idx on public.accounts (team);

create table public.usage (
  team text not null, acct text not null, device text not null, date date not null,
  writer_uid uuid,          -- server-forced = auth.uid()
  by_name text,             -- server-forced from profiles.username
  display_name text,
  fh_pct real, sd_pct real, fh_resets_at text, sd_resets_at text,
  extra_enabled boolean, extra_used numeric(12,2), extra_limit numeric(12,2),
  extra_currency text, extra_pct real,
  tok_month bigint, src text, ts bigint,
  updated_at timestamptz not null default now(),
  primary key (team, acct, device, date)
);
create index usage_team_date_idx on public.usage (team, date);
create index usage_team_updated_idx on public.usage (team, updated_at desc);

create table public.finals (
  team text not null, month text not null, acct text not null, display_name text,
  fh_pct real, sd_pct real, fh_resets_at text, sd_resets_at text,
  extra_enabled boolean, extra_used numeric(12,2), extra_limit numeric(12,2),
  extra_currency text, extra_pct real, tok_month bigint, ts bigint,
  primary key (team, month, acct)
);

create table public.teams (
  team text primary key,
  tz   text not null default 'Europe/Athens'
);

create table public.connector_tokens (
  token_hash   text primary key,
  team         text not null,
  created_by   uuid not null default auth.uid(),
  expires_at   timestamptz,                           -- NULL = never (discouraged); Worker enforces
  last_used_at timestamptz,
  created_at   timestamptz not null default now()
);
create index connector_tokens_team_idx on public.connector_tokens (team);

-- ============================================================================
-- 2. HELPER FUNCTIONS
-- ============================================================================
create or replace function public.jwt_team()
returns text language sql stable set search_path = '' as $$
  select nullif(lower(auth.jwt() -> 'app_metadata' ->> 'team'), '')
$$;

-- is_admin: INVOKER (see header). Reads the caller's own team-scoped profile row.
create or replace function public.is_admin()
returns boolean language sql stable security invoker set search_path = '' as $$
  select coalesce((select is_admin from public.profiles where user_id = auth.uid()), false)
$$;

-- Soft device-lock predicate (DEFINER avoids profiles-RLS recursion). Currently unused.
create or replace function public.active_device()
returns text language sql stable security definer set search_path = '' as $$
  select active_device_id from public.profiles where user_id = auth.uid()
$$;

-- ============================================================================
-- 3. RLS POLICIES + SERVER-FORCE TRIGGERS
-- ============================================================================
-- profiles
alter table public.profiles enable row level security;
create policy profiles_select on public.profiles for select to authenticated
  using ( team = (select public.jwt_team()) );
create policy profiles_insert on public.profiles for insert to authenticated
  with check ( user_id = (select auth.uid()) and team = (select public.jwt_team()) );
create policy profiles_update on public.profiles for update to authenticated
  using ( user_id = (select auth.uid()) )
  with check ( user_id = (select auth.uid()) and team = (select public.jwt_team()) );

create or replace function public.force_profile()
returns trigger language plpgsql security invoker set search_path = '' as $$
begin
  new.user_id  := auth.uid();
  new.email    := auth.jwt() ->> 'email';
  new.team     := public.jwt_team();
  new.is_admin := (auth.jwt() -> 'app_metadata' ->> 'role') = 'admin';
  new.updated_at := now();
  return new;
end $$;
create trigger profiles_force before insert or update on public.profiles
  for each row execute function public.force_profile();

-- accounts
alter table public.accounts enable row level security;
create policy accounts_select on public.accounts for select to authenticated
  using ( team = (select public.jwt_team()) );
create policy accounts_insert on public.accounts for insert to authenticated
  with check ( team = (select public.jwt_team()) );
create policy accounts_update on public.accounts for update to authenticated
  using ( team = (select public.jwt_team()) )
  with check ( team = (select public.jwt_team()) );

create or replace function public.force_team_col()
returns trigger language plpgsql security invoker set search_path = '' as $$
begin new.team := public.jwt_team(); return new; end $$;      -- overwrite any client-supplied team
create trigger accounts_force before insert or update on public.accounts
  for each row execute function public.force_team_col();

-- usage
alter table public.usage enable row level security;
create policy usage_select on public.usage for select to authenticated
  using ( team = (select public.jwt_team()) );
create policy usage_insert on public.usage for insert to authenticated
  with check ( team = (select public.jwt_team()) );
create policy usage_update on public.usage for update to authenticated
  using ( team = (select public.jwt_team()) and writer_uid = (select auth.uid()) )
  with check ( team = (select public.jwt_team()) );

create or replace function public.force_usage_owner()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  new.team       := public.jwt_team();
  new.writer_uid := auth.uid();
  new.by_name    := (select username from public.profiles where user_id = auth.uid());
  new.updated_at := now();
  return new;
end $$;
create trigger usage_force before insert or update on public.usage
  for each row execute function public.force_usage_owner();

-- finals / teams / connector_tokens
alter table public.finals enable row level security;
create policy finals_select on public.finals for select to authenticated
  using ( team = (select public.jwt_team()) );

alter table public.teams enable row level security;
create policy teams_select on public.teams for select to authenticated
  using ( team = (select public.jwt_team()) );

alter table public.connector_tokens enable row level security;
create policy ct_select on public.connector_tokens for select to authenticated
  using ( team = (select public.jwt_team()) );
create policy ct_insert on public.connector_tokens for insert to authenticated
  with check ( team = (select public.jwt_team())
               and created_by = (select auth.uid())
               and (select public.is_admin()) );
create policy ct_delete on public.connector_tokens for delete to authenticated
  using ( team = (select public.jwt_team())
          and ( created_by = (select auth.uid()) or (select public.is_admin()) ) );
create trigger ct_force before insert on public.connector_tokens
  for each row execute function public.force_team_col();

-- ============================================================================
-- 4. CONNECTOR READ RPCs (SECURITY INVOKER; service_role-only — see §5)
-- ============================================================================
create or replace function public.get_team_usage(p_team text, p_dates date[])
returns table(acct text, device text, "date" date, by_name text, display_name text,
              fh_pct real, sd_pct real, fh_resets_at text, sd_resets_at text,
              extra_enabled boolean, extra_used numeric, extra_limit numeric,
              extra_currency text, extra_pct real, tok_month bigint, src text, ts bigint)
language sql stable security invoker set search_path = '' as $$
  select acct, device, date, by_name, display_name, fh_pct, sd_pct, fh_resets_at, sd_resets_at,
         extra_enabled, extra_used, extra_limit, extra_currency, extra_pct, tok_month, src, ts
  from public.usage where team = p_team and date = any(p_dates)
$$;

create or replace function public.get_team_month(p_team text, p_month text)
returns table(kind text, r jsonb) language sql stable security invoker set search_path = '' as $$
  select 'usage', to_jsonb(u) - 'writer_uid' from public.usage u
    where u.team = p_team and to_char(u.date,'YYYY-MM') = p_month
  union all
  select 'final', to_jsonb(f) from public.finals f
    where f.team = p_team and f.month = p_month
$$;

create or replace function public.resolve_connector_token(p_hash text)
returns text language sql stable security invoker set search_path = '' as $$
  select team from public.connector_tokens
  where token_hash = p_hash and (expires_at is null or expires_at > now())
$$;

-- ============================================================================
-- 5. FUNCTION-GRANT LOCKDOWN + INSTANCE-WIDE FAIL-CLOSED HARDENING
-- ============================================================================
-- Supabase grants EXECUTE to anon+authenticated on public functions independently of PUBLIC,
-- so every revoke must name them explicitly (revoking from PUBLIC alone is a no-op).
revoke execute on function public.jwt_team()          from public, anon;      -- authenticated + service_role keep it (policies)
grant  execute on function public.jwt_team()          to authenticated, service_role;
revoke execute on function public.is_admin()          from public, anon;      -- authenticated needs it (connector_tokens policies)
grant  execute on function public.is_admin()          to authenticated;
revoke execute on function public.active_device()     from public, anon, authenticated;
revoke execute on function public.force_profile()     from public, anon, authenticated;   -- trigger fns; never called directly
revoke execute on function public.force_team_col()    from public, anon, authenticated;
revoke execute on function public.force_usage_owner() from public, anon, authenticated;

-- Connector RPCs: service_role ONLY (the Worker). The revoke-FROM-anon/authenticated is the
-- real gate; SECURITY INVOKER is the backstop (a future grant-leak re-applies RLS -> 0 rows).
revoke execute on function public.get_team_usage(text,date[]),
                          public.get_team_month(text,text),
                          public.resolve_connector_token(text)
  from public, anon, authenticated;
grant  execute on function public.get_team_usage(text,date[]),
                          public.get_team_month(text,text),
                          public.resolve_connector_token(text)
  to service_role;

-- Supabase's "Enable automatic RLS" (optional at project creation) adds public.rls_auto_enable();
-- lock it out of the exposed API if present (conditional so a fresh apply without it still runs).
do $$ begin
  if exists (select 1 from pg_proc
             where proname = 'rls_auto_enable' and pronamespace = 'public'::regnamespace) then
    execute 'revoke execute on function public.rls_auto_enable() from public, anon, authenticated';
  end if;
end $$;

-- Instance-wide fail-closed: block schema-CREATE (claims-spoof vector) + auto-grant on FUTURE tables.
revoke create on schema public from public, anon, authenticated;
alter default privileges in schema public revoke all on tables from anon, authenticated;

-- ============================================================================
-- 6. SIGNUP GATE — team derivation + domain-spoof mitigation
-- ============================================================================
-- Stamps app_metadata.team (from the verified email domain) + role at signup. after-LAST-@
-- extraction (not split_part field #2); free-mail/disposable denylist. SPIKE-verified live:
-- the trigger fires on OTP signup and the mutation persists into the JWT app_metadata.
create or replace function public.on_auth_user_created()
returns trigger language plpgsql security definer set search_path = '' as $$
declare dom text; is_first boolean;
begin
  dom := lower(substring(new.email from '@([^@]+)$'));
  if dom is null or dom = any (array[
      'gmail.com','googlemail.com','outlook.com','hotmail.com','live.com','yahoo.com',
      'icloud.com','me.com','proton.me','protonmail.com','aol.com','gmx.com',
      'mailinator.com','guerrillamail.com','10minutemail.com' /* maintain this list */ ]) then
    raise exception 'signups not allowed for domain %', dom;
  end if;
  -- CLOSED deployment: uncomment to allowlist exactly one domain:
  --   if dom <> 'skg-t.com' then raise exception 'domain not allowed'; end if;
  select not exists(select 1 from auth.users
      where id <> new.id and lower(substring(email from '@([^@]+)$')) = dom) into is_first;
  new.raw_app_meta_data := coalesce(new.raw_app_meta_data,'{}'::jsonb)
    || jsonb_build_object('team', dom, 'role', case when is_first then 'admin' else 'member' end);
  return new;
end $$;
revoke execute on function public.on_auth_user_created() from public, anon, authenticated;
create trigger on_auth_user_created before insert on auth.users
  for each row execute function public.on_auth_user_created();

-- ============================================================================
-- 7. MONTH-END FINALS + RETENTION (pg_cron)
-- ============================================================================
-- Derives the team list from distinct usage.team (teams is a tz-override table, default Athens);
-- p_now is injectable for testing. Verified live: month-boundary incl. leap-year, and a synthetic
-- capture (distinct-on-acct latest / day-filter / column mapping).
create or replace function public.capture_finals(p_now timestamptz default now())
returns int language plpgsql security definer set search_path = '' as $$
declare d record; lt timestamp; m text; z text; rc int; n int := 0;
begin
  for d in select distinct team from public.usage loop
    z  := coalesce((select tz from public.teams where team = d.team), 'Europe/Athens');
    lt := p_now at time zone z;
    if extract(hour from lt) = 23
       and to_char(lt,'YYYY-MM') <> to_char(lt + interval '1 day','YYYY-MM') then
      m := to_char(lt,'YYYY-MM');
      insert into public.finals(team,month,acct,display_name,fh_pct,sd_pct,fh_resets_at,sd_resets_at,
             extra_enabled,extra_used,extra_limit,extra_currency,extra_pct,tok_month,ts)
      select u.team,m,u.acct,u.display_name,u.fh_pct,u.sd_pct,u.fh_resets_at,u.sd_resets_at,
             u.extra_enabled,u.extra_used,u.extra_limit,u.extra_currency,u.extra_pct,u.tok_month,u.ts
      from (select distinct on (acct) * from public.usage
            where team = d.team and date = lt::date order by acct, updated_at desc) u
      on conflict (team,month,acct) do update set
        display_name=excluded.display_name, fh_pct=excluded.fh_pct, sd_pct=excluded.sd_pct,
        fh_resets_at=excluded.fh_resets_at, sd_resets_at=excluded.sd_resets_at,
        extra_enabled=excluded.extra_enabled, extra_used=excluded.extra_used,
        extra_limit=excluded.extra_limit, extra_currency=excluded.extra_currency,
        extra_pct=excluded.extra_pct, tok_month=excluded.tok_month, ts=excluded.ts;
      get diagnostics rc = row_count;
      n := n + rc;
    end if;
  end loop;
  return n;
end $$;
revoke execute on function public.capture_finals(timestamptz) from public, anon, authenticated;

-- Enable pg_cron (Dashboard → Database → Extensions, or here) then schedule. cron runs in UTC;
-- capture_finals self-gates to 23:xx in each team's local tz, so hourly covers every offset.
create extension if not exists pg_cron;
select cron.schedule('capture-finals', '59 * * * *', $$select public.capture_finals()$$);
select cron.schedule('prune-usage',    '7 3 * * *',  $$delete from public.usage where date < current_date - 90$$);
