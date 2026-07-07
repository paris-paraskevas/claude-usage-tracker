-- Claude Usage Tracker — Supabase account-pool schema + RLS.
-- Source of truth: docs/SUPABASE-MIGRATION.md (9-agent design + security review).
--
-- SECURITY-CRITICAL. Apply to a FRESH Supabase project, top to bottom (order matters:
-- tables → helper fns → RLS+triggers → connector RPCs → hardening → signup gate → cron).
-- This is Postgres-specific and is NOT verified against a live Postgres yet — the
-- grant/isolation "ship-gate" probe (docs checklist step 3) is the verification: prove
-- team-A cannot read/write team-B (and cannot EXECUTE the connector RPCs) BEFORE onboarding
-- a second domain. Run these two assertions AS `authenticated` via the client SDK/PostgREST
-- (never the SQL editor, which runs as owner) after applying:
--   has_function_privilege('authenticated','public.get_team_usage(text,date[])','EXECUTE') = false
--   has_schema_privilege('authenticated','public','CREATE')                                = false
--
-- The hard tenant boundary is RLS `team = jwt_team()`, where team comes from an IMMUTABLE
-- app_metadata claim stamped at signup (NOT the mutable email). Device-lock is soft and is
-- never on the isolation path.

-- ============================================================================
-- 1. TABLES
-- ============================================================================

create table public.profiles (
  user_id           uuid primary key references auth.users(id) on delete cascade,
  email             text not null,
  username          text not null,
  team              text not null,                    -- = app_metadata.team; server-forced
  is_admin          boolean not null default false,   -- forced from app_metadata.role; NOT client-writable
  active_device_id  text,                             -- soft device-lock (not a boundary)
  device_claimed_at timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create unique index profiles_team_username_key on public.profiles (team, username);
create index profiles_team_idx on public.profiles (team);

-- Shared POOL registry (auto-discovered Claude accounts). acct = lowercased Claude-account
-- email; org is INFORMATIONAL ONLY (never a security gate).
create table public.accounts (
  team text not null, acct text not null,
  display_name text, org text,
  created_at timestamptz not null default now(),
  primary key (team, acct)
);
create index accounts_team_idx on public.accounts (team);

-- One row per pooled account × reporting device × local day. ~10s upsert overwrites today's
-- row; midnight rolls a new row. Column names match the old D1 schema 1:1 so the desktop's
-- merge/spend/token math is reused unchanged (did -> device).
create table public.usage (
  team text not null, acct text not null, device text not null, date date not null,
  writer_uid uuid,                                    -- server-forced = auth.uid()
  by_name text,                                       -- server-forced from profiles.username
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

-- Frozen month-end row per account (written by pg_cron; no writer_uid — never exposed).
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

-- sha256(claude.ai-connector token) -> team; admin-minted, expiring.
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

-- Team = immutable app_metadata claim (NOT the mutable email). Fail-closed to NULL.
-- CLOSES HIGH-2: email is user-mutable via updateUser({email}); app_metadata cannot be
-- set by the user, so the boundary survives a "Confirm email OFF" toggle.
create or replace function public.jwt_team()
returns text language sql stable set search_path = '' as $$
  select nullif(lower(auth.jwt() -> 'app_metadata' ->> 'team'), '')
$$;
revoke execute on function public.jwt_team() from public;
grant  execute on function public.jwt_team() to authenticated, service_role;

-- SECURITY DEFINER so it doesn't recurse through profiles RLS.
create or replace function public.is_admin()
returns boolean language sql stable security definer set search_path = '' as $$
  select coalesce((select is_admin from public.profiles where user_id = auth.uid()), false)
$$;
revoke execute on function public.is_admin() from public;
grant  execute on function public.is_admin() to authenticated;

-- Soft device-lock predicate (DEFINER avoids profiles-RLS recursion). OPTIONAL.
create or replace function public.active_device()
returns text language sql stable security definer set search_path = '' as $$
  select active_device_id from public.profiles where user_id = auth.uid()
$$;
revoke execute on function public.active_device() from public;   -- FIX: from PUBLIC, not just anon
grant  execute on function public.active_device() to authenticated;

-- ============================================================================
-- 3. RLS POLICIES + SERVER-FORCE TRIGGERS
-- ============================================================================

-- profiles ------------------------------------------------------------------
alter table public.profiles enable row level security;
create policy profiles_select on public.profiles for select to authenticated
  using ( team = (select public.jwt_team()) );
create policy profiles_insert on public.profiles for insert to authenticated
  with check ( user_id = (select auth.uid()) and team = (select public.jwt_team()) );
create policy profiles_update on public.profiles for update to authenticated
  using ( user_id = (select auth.uid()) )
  with check ( user_id = (select auth.uid()) and team = (select public.jwt_team()) );

-- is_admin comes from app_metadata.role (NOT client-writable), so a user cannot self-promote
-- by patching their own profile row.
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

-- accounts ------------------------------------------------------------------
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

-- usage ---------------------------------------------------------------------
alter table public.usage enable row level security;
create policy usage_select on public.usage for select to authenticated
  using ( team = (select public.jwt_team()) );
create policy usage_insert on public.usage for insert to authenticated
  with check ( team = (select public.jwt_team()) );
-- FIX F7/MED-5: UPDATE bound to the row's own writer -> a member can't overwrite a teammate's
-- row or the cron's device='account' rows (writer_uid NULL).
create policy usage_update on public.usage for update to authenticated
  using ( team = (select public.jwt_team()) and writer_uid = (select auth.uid()) )
  with check ( team = (select public.jwt_team()) );
-- no DELETE policy for authenticated -> default-deny; pg_cron/service_role prunes.

-- FIX (intra-team forgery): client must NOT set team/writer_uid/by_name; server-force them.
-- DEFINER so the profiles lookup ignores caller RLS; auth.uid() is request-scoped so it still
-- returns the real caller inside a DEFINER function.
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

-- OPTIONAL soft device-lock hardening: append to usage_insert/usage_update WITH CHECK:
--   and device = (select public.active_device())
-- Deters casual login-sharing only (four documented bypasses) — never a security guarantee.

-- finals / teams / connector_tokens -----------------------------------------
alter table public.finals enable row level security;
create policy finals_select on public.finals for select to authenticated
  using ( team = (select public.jwt_team()) );        -- writes are pg_cron only (bypasses RLS)

alter table public.teams enable row level security;
create policy teams_select on public.teams for select to authenticated
  using ( team = (select public.jwt_team()) );         -- writes out-of-band / service_role

alter table public.connector_tokens enable row level security;
create policy ct_select on public.connector_tokens for select to authenticated
  using ( team = (select public.jwt_team()) );
-- FIX F6: only ADMINS mint; created_by forced to self.
create policy ct_insert on public.connector_tokens for insert to authenticated
  with check ( team = (select public.jwt_team())
               and created_by = (select auth.uid())
               and (select public.is_admin()) );
-- FIX F6: a member deletes only their OWN tokens (no cross-member griefing); admins any.
create policy ct_delete on public.connector_tokens for delete to authenticated
  using ( team = (select public.jwt_team())
          and ( created_by = (select auth.uid()) or (select public.is_admin()) ) );
create trigger ct_force before insert on public.connector_tokens
  for each row execute function public.force_team_col();

-- ============================================================================
-- 4. CONNECTOR READ RPCs — CRIT-1 fix (the ship-blocker)
-- ============================================================================
-- `revoke ... from anon, authenticated` is a NO-OP — Postgres grants EXECUTE to PUBLIC by
-- default and both roles inherit it. Must revoke FROM PUBLIC. Extra backstop: SECURITY
-- INVOKER (not DEFINER) — service_role has BYPASSRLS so the Worker still sees all rows and
-- p_team filters; but if the grant is EVER loosened to authenticated, RLS re-applies and
-- p_team=<victim> returns ZERO rows. These must stay in the EXPOSED public schema to be
-- PostgREST-/rpc-callable by the Worker; the revoke-FROM-PUBLIC + INVOKER combo is what makes
-- public safe. (Move to a private schema only if you later adopt the direct-Postgres path.)

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

revoke execute on function public.get_team_usage(text,date[]),
                          public.get_team_month(text,text),
                          public.resolve_connector_token(text)
  from public, anon, authenticated;                    -- the REAL gate
grant  execute on function public.get_team_usage(text,date[]),
                          public.get_team_month(text,text),
                          public.resolve_connector_token(text)
  to service_role;

-- ============================================================================
-- 5. INSTANCE-WIDE FAIL-CLOSED HARDENING (mandatory)
-- ============================================================================
-- CLOSES HIGH-4: blocks the set_config('request.jwt.claims',…) claims-spoof, which needs
-- CREATE on an exposed schema to plant a function.
revoke create on schema public from public, anon, authenticated;
-- New tables fail closed (Supabase auto-grants DML to authenticated -> a future RLS-less
-- table would be world-readable across teams).
alter default privileges in schema public revoke all on tables from anon, authenticated;
-- Standing rules (enforce in review/CI): every new table gets `enable row level security`;
-- every view is `create view … with (security_invoker = on)`; run Supabase Security Advisor.

-- ============================================================================
-- 6. SIGNUP GATE — team derivation + domain-spoof mitigation
-- ============================================================================
-- Stamps app_metadata.team (from the verified email domain) + role at signup. Fixes F4
-- (after-LAST-@ extraction, not split_part field #2) and HIGH-3 (free-mail/disposable
-- denylist). SPIKE-4: auth is a MANAGED schema — verify this trigger fires on the current
-- Supabase; a "Before-User-Created" auth HOOK is cleaner (rejects BEFORE the OTP email is
-- sent) if your tier supports it. First signup per domain = admin (heuristic; for controlled
-- rollout set role via the admin API instead).
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
create trigger on_auth_user_created before insert on auth.users
  for each row execute function public.on_auth_user_created();

-- ============================================================================
-- 7. MONTH-END FINALS + RETENTION (pg_cron)
-- ============================================================================
-- SPIKE-5: enable pg_cron (Dashboard → Database → Extensions) and PROBE the tz/month-boundary
-- math with test timestamps before scheduling (dates/tz trap zone). v1 single-team may hardcode
-- a fixed-tz schedule instead of the per-team loop.
create or replace function public.capture_finals()
returns void language plpgsql security definer set search_path = '' as $$
declare t record; lt timestamptz; m text;
begin
  for t in select team, tz from public.teams loop
    lt := now() at time zone t.tz;
    if extract(hour from lt) = 23
       and to_char(lt,'YYYY-MM') <> to_char(lt + interval '1 day','YYYY-MM') then   -- last day of month
      m := to_char(lt,'YYYY-MM');
      insert into public.finals(team,month,acct,display_name,fh_pct,sd_pct,fh_resets_at,sd_resets_at,
             extra_enabled,extra_used,extra_limit,extra_currency,extra_pct,tok_month,ts)
      select u.team,m,u.acct,u.display_name,u.fh_pct,u.sd_pct,u.fh_resets_at,u.sd_resets_at,
             u.extra_enabled,u.extra_used,u.extra_limit,u.extra_currency,u.extra_pct,u.tok_month,u.ts
      from (select distinct on (acct) * from public.usage
            where team=t.team and date=lt::date order by acct, updated_at desc) u
      on conflict (team,month,acct) do update set
        display_name=excluded.display_name, fh_pct=excluded.fh_pct, sd_pct=excluded.sd_pct,
        fh_resets_at=excluded.fh_resets_at, sd_resets_at=excluded.sd_resets_at,
        extra_enabled=excluded.extra_enabled, extra_used=excluded.extra_used,
        extra_limit=excluded.extra_limit, extra_currency=excluded.extra_currency,
        extra_pct=excluded.extra_pct, tok_month=excluded.tok_month, ts=excluded.ts;
    end if;
  end loop;
end $$;
-- Schedule after enabling pg_cron:
-- select cron.schedule('capture-finals','59 * * * *', $$select public.capture_finals()$$);
-- select cron.schedule('prune-usage',   '7 3 * * *',  $$delete from public.usage where date < current_date - 90$$);
