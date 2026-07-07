Verified against the code: `build_team_report` (claude_usage_tracker.py:1889-1914) sets no `by_name`; the relay injects it server-side from the authenticated member (index.js:276,287). TeamSync (2072), `_escrow` (2132), `mcpResolveTeam` (617), `teamCron` (832), and schema.sql tables (escrow:86, snapshots:122, usage_rows:38, finals:64) all confirmed at the cited lines. The `by_name` bug is real and is fixed below by server-forcing it. Final doc:

---

# Claude Usage Tracker — Supabase-Hybrid Re-Architecture (Final, Reviewed)

Status: buildable. All three adversarial reviews' CONFIRMED findings are applied inline (not just listed). Unverified items are marked **SPIKE**. Code sites cited at `file:line`.

## Architecture

Two identities per person: (1) **tracker login** = Supabase Auth email-OTP → username, device-locked (soft); (2) their **Claude account(s)** = read locally from `~/.claude/.credentials.json` (unchanged). The shared per-account usage **pool** (accounts / daily usage / month-end finals) moves to **Supabase Postgres under RLS**, keyed on `team` = the email domain from an **immutable `app_metadata.team` JWT claim**. The desktop pushes ~10s upserts and reads the live pool as the logged-in Supabase user (RLS-scoped); the pywebview browser only ever calls loopback `/api/team/*`, so the user JWT never enters the DOM. The **Cloudflare Worker + D1/KV are kept unchanged** for E2EE phone sync (`/v1/acct/*`, `snapshots`), FCM, and the claude.ai **OAuth MCP connector** — the connector now reads the pool from Supabase server-to-server. Month-end **finals capture moves to Supabase `pg_cron`** (security-motivated: removes a Supabase write-key from the Worker); the Worker cron is retained for its existing FCM/housekeeping work. **The hard tenant boundary is RLS `team = jwt_team()`; device-lock is never on the isolation path.**

---

## Schema (final DDL)

```sql
-- profiles: one row per TRACKER user
create table public.profiles (
  user_id           uuid primary key references auth.users(id) on delete cascade,
  email             text not null,
  username          text not null,
  team              text not null,          -- = app_metadata.team; server-forced
  is_admin          boolean not null default false,  -- forced from app_metadata.role; NOT client-writable
  active_device_id  text,                    -- soft device-lock (not a boundary)
  device_claimed_at timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create unique index profiles_team_username_key on public.profiles (team, username);
create index profiles_team_idx on public.profiles (team);

-- accounts: shared POOL registry (auto-discovered Claude accounts). acct = lowercased
-- Claude-account email; org is INFORMATIONAL ONLY (never a security gate).
create table public.accounts (
  team text not null, acct text not null,
  display_name text, org text,
  created_at timestamptz not null default now(),
  primary key (team, acct)
);
create index accounts_team_idx on public.accounts (team);

-- usage: one row per pooled account × reporting device × local day. device = did (stable
-- device id, PK component). ~10s upsert overwrites today's row; midnight rolls a new row.
create table public.usage (
  team text not null, acct text not null, device text not null, date date not null,
  writer_uid uuid,          -- server-forced = auth.uid()
  by_name text,             -- server-forced from profiles.username (see FIX below)
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

-- finals: frozen month-end row per account (written by pg_cron). No writer_uid (never exposed).
create table public.finals (
  team text not null, month text not null, acct text not null, display_name text,
  fh_pct real, sd_pct real, fh_resets_at text, sd_resets_at text,
  extra_enabled boolean, extra_used numeric(12,2), extra_limit numeric(12,2),
  extra_currency text, extra_pct real, tok_month bigint, ts bigint,
  primary key (team, month, acct)
);

-- teams: per-domain cron tz (org dropped — was decorative)
create table public.teams (
  team text primary key,
  tz   text not null default 'Europe/Athens'
);

-- connector_tokens: maps sha256(claude.ai-connector token) → team; admin-minted, expiring
create table public.connector_tokens (
  token_hash text primary key,
  team       text not null,
  created_by uuid not null default auth.uid(),
  expires_at timestamptz,                     -- NULL = never (discouraged); Worker enforces
  last_used_at timestamptz,
  created_at timestamptz not null default now()
);
create index connector_tokens_team_idx on public.connector_tokens (team);
```

Column names match D1 1:1 (`fh_pct`, `sd_pct`, `extra_*`, `tok_month`, `by_name`, `src`, `ts`) so `build_team_report`/`team_overview_merge`/`team_ledger_computed`/`team_month_spend`/`member_month_tokens` reuse without field renames; `did → device`.

---

## RLS policies (final, with fixes applied)

### Tenant + helper functions

```sql
-- Team = immutable app_metadata claim (NOT the mutable email). Fail-closed to NULL.
-- CLOSES HIGH-2: email is user-mutable via updateUser({email}); if "Confirm email" is
-- ever toggled off, an email-derived team is spoofable. app_metadata.raw_app_meta_data
-- cannot be set by the user, so the boundary survives that toggle.
create or replace function public.jwt_team()
returns text language sql stable set search_path = '' as $$
  select nullif(lower(auth.jwt() -> 'app_metadata' ->> 'team'), '')
$$;
revoke execute on function public.jwt_team() from public;   -- tidy; returns only caller's own team
grant  execute on function public.jwt_team() to authenticated, service_role;

-- Admin check (SECURITY DEFINER so it doesn't recurse through profiles RLS).
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
```

### profiles

```sql
alter table public.profiles enable row level security;
create policy profiles_select on public.profiles for select to authenticated
  using ( team = (select public.jwt_team()) );                       -- see own team's members
create policy profiles_insert on public.profiles for insert to authenticated
  with check ( user_id = (select auth.uid()) and team = (select public.jwt_team()) );
create policy profiles_update on public.profiles for update to authenticated
  using ( user_id = (select auth.uid()) )
  with check ( user_id = (select auth.uid()) and team = (select public.jwt_team()) );

-- Server-force identity; is_admin comes from app_metadata.role (NOT client-writable), so a
-- user cannot self-promote by patching their own profile row via profiles_update.
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
```

### accounts

```sql
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
begin new.team := public.jwt_team(); return new; end $$;    -- overwrite any client team
create trigger accounts_force before insert or update on public.accounts
  for each row execute function public.force_team_col();
```

### usage (exemplar; `by_name`/writer bug fixed)

```sql
alter table public.usage enable row level security;
create policy usage_select on public.usage for select to authenticated
  using ( team = (select public.jwt_team()) );
create policy usage_insert on public.usage for insert to authenticated
  with check ( team = (select public.jwt_team()) );
-- FIX F7/MED-5: UPDATE bound to the row's own writer → a member can't overwrite a
-- teammate's row or the cron's device='account' rows (writer_uid NULL).
create policy usage_update on public.usage for update to authenticated
  using ( team = (select public.jwt_team()) and writer_uid = (select auth.uid()) )
  with check ( team = (select public.jwt_team()) );
-- no DELETE policy for authenticated → default-deny; pg_cron/service_role prunes.

-- FIX (confirmed bug): build_team_report sets NO by_name; today the relay stamps it
-- server-side (index.js:287). Client must not send by_name (KeyError) and must not be
-- allowed to forge it. DEFINER so the profiles lookup ignores caller RLS; auth.uid()
-- is request-scoped so it still returns the real caller inside a DEFINER function.
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

-- OPTIONAL device-lock hardening (soft): append to usage_insert/usage_update WITH CHECK:
--   and device = (select public.active_device())
-- Deters casual login-sharing only; see Device-lock section for four bypasses.
```

### finals / teams / connector_tokens

```sql
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
-- FIX F6: a member can delete only their OWN tokens (no cross-member griefing); admins any.
create policy ct_delete on public.connector_tokens for delete to authenticated
  using ( team = (select public.jwt_team())
          and ( created_by = (select auth.uid()) or (select public.is_admin()) ) );
create trigger ct_force before insert on public.connector_tokens
  for each row execute function public.force_team_col();
```

### Connector read functions — CRIT-1 fix (the ship-blocker)

```sql
-- CLOSES CRIT-1/F1/#1: `revoke ... from anon, authenticated` is a NO-OP — Postgres grants
-- EXECUTE to PUBLIC by default and both roles inherit it. Must revoke FROM PUBLIC.
-- Extra backstop: SECURITY INVOKER (not DEFINER). service_role has BYPASSRLS, so the Worker
-- still sees all rows and the p_team filter works; but if the grant is EVER loosened to
-- authenticated, RLS re-applies and p_team=<victim> returns ZERO rows — cross-tenant read
-- is impossible even under a grant mistake. Explicit column list drops writer_uid (LOW).
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
```
Note: these must stay in the **exposed** `public` schema to be PostgREST-`/rpc`-callable by the Worker (moving them to a private schema, as two reviewers suggested, would make them uncallable over REST). The `revoke FROM PUBLIC` + `INVOKER` combination is what makes `public` safe here. If you later adopt the least-privilege direct-Postgres path (Security §HIGH-3 residual), move them to `private` then.

### Instance-wide fail-closed hardening (mandatory)

```sql
-- CLOSES HIGH-4/#4: blocks the set_config('request.jwt.claims',…) claims-spoof, which needs
-- CREATE on an exposed schema to plant a function. Verify BEFORE relying on it:
--   select has_schema_privilege('authenticated','public','CREATE');  -- must be false
revoke create on schema public from public, anon, authenticated;

-- New tables fail closed (Supabase auto-grants DML to authenticated → a future RLS-less
-- table would be world-readable across teams). Belt-and-suspenders to "always enable RLS".
alter default privileges in schema public revoke all on tables from anon, authenticated;
```
Standing rules: **every** new table gets `enable row level security`; **every** view is `create view … with (security_invoker = on)` (a postgres-owned view reads as owner → cross-tenant leak, same class as CRIT-1); run **Supabase Security Advisor** in CI.

---

## Auth + device-lock flow (desktop email-OTP)

**Supabase Auth config (assert in a deploy check — these are the boundary's config landmines):**
- Providers: **email-OTP only.** Disable email+password, all social providers, and **anonymous sign-ins** (HIGH-2/F2-A/#2-A: any provider that can mint a session with an attacker-chosen unverified email breaks domain-derivation).
- **"Confirm email" ON**, **Secure Email Change ON** (defaults) — defense-in-depth behind `app_metadata.team`.
- Magic-Link template → `{{ .Token }}` (6-digit code path; no redirect → no link-interception/open-redirect surface).
- **OTP expiry → 300 s** (F3: 6-digit code, no per-code attempt cap, per-IP-only rate limit → ~12× smaller brute-force window).
- **Bot & Abuse Protection (Turnstile/hCaptcha) ON** on auth endpoints (F3/F8: throttles scripted `/otp` and `/verify`); pass `captchaToken` in `sign_in_with_otp`.
- **Custom SMTP** (Resend/Brevo) + raise `rate_limit_otp` to a sane per-hour value, keep 60 s/user (F8: built-in email 2/hr = trivial login-DoS).
- **Do NOT gate on `email_verified`** — it lives in `app_metadata`, is derived from `identities`, and has a known staleness bug; OTP already proves email control at signup, and providers are locked to OTP-only.
- **WAF per-email rate-limit on `/auth/v1/verify`** (GoTrue's cap is per-IP, not lowerable → IP-rotation defeats it).

**Client + keys.** Ship the **publishable** key (`sb_publishable_…`) in the binary — safe by design (RLS is the guard, `anon` is granted nothing). Never ship `sb_secret_…`.

**Session persistence** (supabase-py is in-memory by default → back it with Windows Credential Manager via `keyring`, never plaintext):
```python
class KeyringStorage(SyncSupportedStorage):   # gotrue/supabase_auth base; pin to installed ver (SPIKE-1)
    _SVC = "ClaudeUsageTracker.supabase"
    def get_item(self,k):  return keyring.get_password(self._SVC,k)
    def set_item(self,k,v): keyring.set_password(self._SVC,k,v)
    def remove_item(self,k): keyring.delete_password(self._SVC,k)
sb = create_client(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY,
      options=ClientOptions(storage=KeyringStorage(), persist_session=True))
```
gotrue auto-persists and rotates the refresh token (10 s reuse interval + family revocation) and refreshes the ~1 h access token ahead of expiry. **Set access-token lifetime to ~30 min** (F5). Never hand-cache the refresh token (out-of-window reuse revokes the family). Treat an unexpected forced sign-out (family revocation) as a **security event** surfaced to the user (breach signal).

**Launch gate** (pywebview view on first run / no valid session):
```python
sb.auth.sign_in_with_otp({"email": email,
    "options": {"should_create_user": True, "captcha_token": tok}})   # SPIKE-1: verify code still sent
res = sb.auth.verify_otp({"email": email, "token": code, "type": "email"})
device_id = load_or_make_device_id()                 # local persisted value (analog of ensure_team_device 1848)
sb.table("profiles").upsert({                        # team/email/user_id/is_admin all trigger-forced
    "username": chosen_username,
    "active_device_id": device_id, "device_claimed_at": "now()",
}, on_conflict="user_id").execute()
```

**Device-lock enforcement — primary is the client self-check, each poll tick:**
```python
me = sb.table("profiles").select("active_device_id").eq("user_id", uid).single().execute()
if me.data and me.data["active_device_id"] != device_id:
    sb.auth.sign_out(); KeyringStorage().remove_item("session")
    show_relogin("Signed in on another device")
```
**Device-lock is SOFT and off the isolation path.** Four confirmed bypasses (talk to PostgREST directly so the self-check never runs; re-claim your own `active_device_id`; clone the local `device_id`; set the RLS `device` field equal to your own `active_device_id`). **Never** promote it to a security or seat/licensing guarantee. Pro "Single session per user" is complementary only (bites at next JWT refresh, ≥5 min lag).

---

## Team scoping — derivation + domain-spoof mitigation

`team` = **`app_metadata.team`**, stamped **once at signup** from the verified email, read by `jwt_team()` (fail-closed to NULL). Stamped by a signup gate that also enforces the domain policy:

```sql
-- BEFORE INSERT on auth.users. Fixes: F4 (after-LAST-@ extraction, not split_part field #2:
-- 'victim@skg-t.com@evil.com' → 'evil.com', not 'skg-t.com'); F2-B/HIGH-3 (free-mail/disposable
-- denylist so unrelated gmail users don't collapse into one shared team). auth is a managed
-- schema → SPIKE-4: verify trigger/hook against current Supabase; a Before-User-Created auth
-- HOOK is cleaner (rejects BEFORE the OTP email is sent → also helps F8) if the tier allows it.
create or replace function public.on_auth_user_created()
returns trigger language plpgsql security definer set search_path = '' as $$
declare dom text; is_first boolean;
begin
  dom := lower(substring(new.email from '@([^@]+)$'));
  if dom is null or dom = any (array[
      'gmail.com','googlemail.com','outlook.com','hotmail.com','live.com','yahoo.com',
      'icloud.com','me.com','proton.me','protonmail.com','aol.com','gmx.com',
      'mailinator.com','guerrillamail.com','10minutemail.com' /* maintain */ ]) then
    raise exception 'signups not allowed for domain %', dom;
  end if;
  -- CLOSED deployment: uncomment to allowlist:
  --   if dom <> 'skg-t.com' then raise exception 'domain not allowed'; end if;
  select not exists(select 1 from auth.users
      where id <> new.id and lower(substring(email from '@([^@]+)$')) = dom) into is_first;
  new.raw_app_meta_data := coalesce(new.raw_app_meta_data,'{}'::jsonb)
    || jsonb_build_object('team', dom, 'role', case when is_first then 'admin' else 'member' end);
  return new;
end $$;
create trigger on_auth_user_created before insert on auth.users
  for each row execute function public.on_auth_user_created();
```
Corporate domains are globally unique → lookalikes/subdomains/homoglyphs self-isolate into their **own** tenants (cannot read the real domain) — sound. First-signup-per-domain = admin (heuristic; for controlled rollout, set `role` via the admin API instead). For legitimate personal-email cohorts, route them to **explicit invite-code tenancy** instead of domain (the sanctioned alternative).

---

## Push (~10s) and read (live) paths

**PUSH** — in `TeamSync.sync` (claude_usage_tracker.py:2099) replace the `_team_call("PUT",…/report)` with two upserts as the logged-in user. Client sends **no** `team`, `writer_uid`, or `by_name` (all server-forced); `by_name` is dropped from the payload entirely (it was never in `build_team_report` — the bug). `returning="minimal"` drops the RETURNING SELECT (less egress + no SELECT-policy dependency on the hot path):
```python
row = build_team_report(snap, dev, account)                 # existing shape (1889); keep login-switch detect (2109)
acct = account["acct"]; date = _now_local().strftime("%Y-%m-%d")
sb.table("accounts").upsert(
    {"acct": acct, "display_name": row["name"], "org": account.get("org")},
    on_conflict="team,acct", returning="minimal").execute()
sb.table("usage").upsert({
    "acct": acct, "device": dev["did"], "date": date, "display_name": row["name"],
    "fh_pct": row["fh_pct"], "sd_pct": row["sd_pct"],
    "fh_resets_at": row["fh_resets_at"], "sd_resets_at": row["sd_resets_at"],
    "extra_enabled": (row["extra"] or {}).get("enabled") if row["extra"] else None,
    "extra_used": (row["extra"] or {}).get("used"),  "extra_limit": (row["extra"] or {}).get("limit"),
    "extra_currency": (row["extra"] or {}).get("currency"), "extra_pct": (row["extra"] or {}).get("pct"),
    "tok_month": dev["tok_month"], "src": "push", "ts": row["ts"],
}, on_conflict="team,acct,device,date", returning="minimal").execute()
```
Cadence unchanged: the `self._team.due(now, ≈10s)` gate (4764-4768) stays. Writes are unlimited on Free; table stays tiny (accounts × devices × retention).

**READ — POLL, not Realtime** (10s Postgres-changes fan-out would blow the message quota for zero benefit at this scale; also removes the Realtime authz surface entirely). Keep the Python-proxy shape; only the data source changes:
- `GET /api/team/overview` (3669): `sb.table("usage").select(<explicit cols>).in_("date",[today,yesterday])` + `accounts`; reshape into the existing `{today, accounts:[…]}` structure (mirror `teamOverviewData` index.js:362), then feed the **unchanged** `team_overview_merge`/`team_month_spend`/`member_month_tokens`. RLS scopes rows to the caller's team automatically — the query passes **no** `team`.
- `GET /api/team/ledger` (3677): `sb.table("usage").select(...).like("date", f"{month}-%")` + `finals`; reshape to `{accounts,days,finals}`; reuse `team_ledger_computed` (2063). Ledger math preserved.
- Egress control (5 GB Free is a hard 402): poll the ~6 KB overview at dashboard cadence **only while the Team tab is visible**; fetch the month ledger on demand. Provision **EU region** (see Security #5). A 3–20-person team fits Free; always-on live views for all → Pro.

**Worker → Supabase connector read (service_role, team-scoped).** The connector's merge logic (`overviewMerge`/`ledgerComputed`/`memberMonthTokens`) is kept; only the data source + team resolution change. OAuth consent (index.js:1050) validates a pasted **connector token** → domain, and binds the grant to the domain (`oauth_codes.tid`/`oauth_tokens.tid` → `…team`; `mcpResolveTeam` 617 returns `{team}`):
```js
const h = await sha256hex(connectorToken);
const team = await rpc(env, "resolve_connector_token", { p_hash: h });   // null/expired → reject consent
```
```js
// service_role stays ONLY in `wrangler secret put`; never in wrangler.toml/[vars]; never logged.
// New sb_secret_ key → send in `apikey` (Bearer-only is rejected unless it equals apikey);
// legacy service_role JWT works in both headers.
async function rpc(env, fn, body){
  return fetch(`${env.SUPABASE_URL}/rest/v1/rpc/${fn}`, { method:"POST",
    headers:{ apikey: env.SUPABASE_SECRET, Authorization:`Bearer ${env.SUPABASE_SECRET}`,
              "content-type":"application/json" }, body: JSON.stringify(body) }).then(r=>r.json());
}
const rows = await rpc(env, "get_team_usage", { p_team: team, p_dates:[today,yesterday] });
```
The RPC centralizes the team filter in one audited function (a coding slip in the Worker can't widen it), and `p_team` is bound from the per-team connector token — **never** from a client MCP param. Add a Worker startup assertion that `SUPABASE_SECRET` ≠ the publishable key, and scrub headers from the fetch path's logs.

---

## Migration from D1; desktop code changes; Android

**Migration.** Membership does not migrate (`members`/token hashes obsolete → users re-onboard via OTP). `teams(tz)` → `teams`. Because a D1 team is a random `tid` not a domain, a faithful port needs a `tid→domain` map. **Recommended: clean cutover** — drop the pool, re-onboard, let ~10s pushes refill within seconds; optionally backfill `finals` only. This avoids the tid↔domain reconciliation and is the lower-risk path unless historical finals must be preserved (then: admin logs in once to establish the domain, a one-shot script reads D1 via `wrangler d1 execute --json` and upserts under that domain via service_role). **Unchanged in Worker/D1/KV:** all `/v1/acct/*` phone sync, `snapshots` (schema.sql:122), auth/tokens/commands (KV), FCM, and the OAuth provider tables (`oauth_clients/codes/tokens`, 98-112) — only `oauth_*.tid → team`.

**Escrow + token-refresh cron — DEFERRED (a security win).** The ~10s upserts already write a per-account daily row whose last value ≈ end-of-day, so month-spend works without token escrow. This drops the `escrow` table (schema.sql:86), the `/token` endpoint, and `TeamSync._escrow` (2132) — removing central storage of users' Claude OAuth tokens (a far worse target). **Finals capture → Supabase `pg_cron`** (removes a Supabase write-key from the Worker; the Worker cron `teamCron` at 832 is retained for FCM/snapshot housekeeping only):
```sql
-- Trap zone (dates/tz): PROBE with test timestamps before shipping (SPIKE). v1 single-team
-- (Europe/Athens) may hardcode a fixed-tz schedule instead of the per-team loop.
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
-- select cron.schedule('capture-finals','59 * * * *', $$select public.capture_finals()$$);
-- retention prune (also pg_cron): delete from public.usage where date < current_date - 90;
```

**Desktop code changes (concrete).**
- New pywebview **auth gate** (OTP→username→device claim). Add `supabase`+`keyring` to `pyproject.toml` (authoritative) and `requirements.txt`.
- Retire identity block (1710-1886): `TEAM_PATH`/`load_team_identity`/`team_create`/`team_join`/`team_add_member`/`team_leave`/`team_parse_join`/`ensure_team_device` → thin module holding the keyring-backed client + a persisted local `device_id`; delete `_team_call`/`_team_get_json` (1732/1753).
- `TeamSync` (2072): `enabled()` → "valid Supabase session exists"; `sync()` (2099) → the two upserts above (keep login-switch detect 2109 + `build_team_report`); drop `_escrow` (2132); poll dispatch (4764-4768) unchanged.
- Team-tab data source (3669/3677) → Supabase + reshape; reuse the four merge fns; `team_admin_overview_merged` (2009) rewritten/inlined. **Dashboard HTML/JS untouched.**
- `_team_action` (3787): `create/join/leave/member-add/member-remove` → `login/logout/set-username`; `admin-token` (3815, the "Copy admin token" button) → `connector-token`: **require `is_admin` + a fresh re-auth** (F5), mint a random token, `sb.table("connector_tokens").insert({token_hash: sha256(tok), expires_at: …})`, return plaintext once for the claude.ai consent.
- Phone-embed for admin (`_refresh_team_overview` 4609, `team_overview_compact` 2029): unchanged shape; now merges Supabase data and still rides the E2EE snapshot to the phone.
- Unchanged: `read_account` (319), `read_oauth` (308), `fetch_profile_org` (479), all personal-usage/statusline/history/all-time, and the entire RemoteSync/phone-sync/FCM path.

**Android — DEFERRED.** No change: the app consumes the E2EE phone-sync snapshot (`/v1/acct/*`, Worker+KV) + FCM; admin's team overview reaches it via the unchanged `team_overview_compact` embed. The phone never talks to Supabase. A future native login would reuse the same Supabase email-OTP flow (Kotlin gotrue), tracked separately.

---

## Security review — CONFIRMED issues, mitigation applied, residual risk

| # | Severity | Issue | Mitigation applied in this doc | Residual |
|---|---|---|---|---|
| CRIT-1 | **CRITICAL (ship-blocker)** | `revoke … from anon, authenticated` on the connector RPCs is a no-op (PUBLIC retains EXECUTE) → any authenticated user calls `get_team_usage(p_team=victim)` and reads every team | `revoke … FROM PUBLIC` + `grant … to service_role`; RPCs made **SECURITY INVOKER** so even a future grant-leak re-applies RLS and yields zero cross-tenant rows; `to_jsonb(u) - 'writer_uid'` stops UUID over-share | None if the verify probe passes (see spikes) |
| HIGH-2 | HIGH | Tenant boundary rode on the **mutable** `email` claim; one "Confirm email OFF" toggle → cross-tenant read+write | Boundary moved to **immutable `app_metadata.team`**, stamped at signup; confirmations kept ON as defense-in-depth; all non-OTP providers disabled | Signup gate must run on every user creation (spike) |
| HIGH-3 | HIGH | Consumer/free-mail domains collapse into one shared team | Free-mail/disposable **denylist** enforced at the signup gate (raise) **and** fail-closed; optional allowlist for closed deployments; invite-code path for personal emails | Denylist upkeep is ongoing |
| HIGH-4 | HIGH | `set_config('request.jwt.claims',…)` in a user-planted function forges any claim (pure-RLS bypass) — only if `authenticated` can CREATE in an exposed schema | `revoke create on schema public from public, anon, authenticated` (mandated) + verify probe | None if probe passes |
| F3 | HIGH | Email-only OTP brute force (6-digit, no per-code cap, per-IP-only limit, 1 h validity) | OTP validity → 300 s; CAPTCHA; WAF per-email limit on `/verify`; MFA for admins; connector-mint gated on admin+re-auth | 6-digit space + IP-pool attacker is resource-bounded, not eliminated — monitor |
| F4/MED | MEDIUM | `split_part(email,'@',2)` mis-parses multi-`@`/quoted emails (takes field #2, not the domain) | Robust **after-last-`@`** extraction `substring(email from '@([^@]+)$')` in the signup gate; reject non-simple addresses | None |
| F7/MED-5 | MEDIUM | Intra-team forgery: client-set `by_name`/`acct`, and a member could overwrite a teammate's/cron's row | `by_name` **server-forced** from `profiles.username`; `usage_update` bound to `writer_uid = auth.uid()`; cron rows (writer NULL) protected | `acct`/`display_name`/metrics still client-asserted (intra-team only) — documented |
| F6 | MEDIUM | No admin role → any member mints/deletes team-wide connector tokens, no expiry | `is_admin` (from `app_metadata.role`, not client-writable) gates `ct_insert`; `ct_delete` scoped to owner/admin; `expires_at` + Worker enforcement | Token still bearer; rotate + audit in UI |
| #3/F5 | HIGH (custody) | `service_role` in the Worker = whole-DB blast radius; the "scoped-JWT" mitigation is **infeasible** (asymmetric signing keys are non-exportable; legacy HS256 secret mints any role) | Cron's DB write removed from Worker via **pg_cron**; connector RPC centralizes the filter; key only in `wrangler secret`, startup assertion, log-scrubbing; **do NOT attempt scoped-JWT** | **RESIDUAL:** a Worker/key compromise still reads+writes all teams. Planned hardening: a least-privilege `worker_reader` login role (`grant select on usage,finals,connector_tokens`) over **direct Postgres (Hyperdrive + postgres.js)**, capping a leak to 3 read-only tables |
| #5 | MEDIUM | Pool is plaintext PII at rest (downgrade from E2EE phone-sync): Claude emails, €spend, tokens, "used by" names | **EU region** + sign Supabase DPA; retention 90 d live (finals long-term); minimize columns; explicit "pool is not E2EE" consent | Vendor/staff/DB-credential visibility remains — accepted-by-design, must be disclosed |
| #7 | MEDIUM (legal) | Anthropic ToS: reusing Claude Code's OAuth token from a 3rd-party app + centralizing/redistributing identifiable usage via the connector | Escrow deferred (no central Claude-token storage); flag for owner | **RESIDUAL:** requires a ToS/legal read + explicit user consent before team-central storage ships |
| #8 | LOW-MED (supply chain) | `supabase`+`keyring` pull a large tree into the process holding the Claude OAuth token | Hash-pinned lockfile + exact pins + signed installer; consider hitting GoTrue+PostgREST with plain `httpx` to shrink the tree | Full-SDK tree accepted for v1 (session refresh); minimal-httpx is the hardening |
| F8 | MEDIUM (avail.) | OTP-send caps → login DoS | Custom SMTP + raised `rate_limit_otp` + 60 s/user + CAPTCHA | Monitor send-rate |

**Provably sound (attacked, held):** per-command policy split (SELECT→USING, INSERT→WITH CHECK, UPDATE→both); write path cannot cross teams (BEFORE-trigger forces `team` pre-arbitration, UPDATE-USING throws on a conflicting foreign-team row, WITH CHECK re-guards); `return=minimal` avoids the SELECT-policy dependency; no-DELETE = default-deny; `search_path=''` on every function; NULL/absent-team fails closed; publishable key in the binary is safe (RLS is the guard, `anon` granted nothing); `sb_secret_` in `apikey` header confirmed; device-lock correctly soft and off the isolation path.

**Pre-build spikes (verify at build time; do not ship on assumption):**
1. **supabase-py surface** — pin `supabase`/`supabase_auth`; confirm `SyncSupportedStorage`, `ClientOptions(storage=,persist_session=)`, `.upsert(on_conflict=, returning="minimal")`; and the `sign_in_with_otp` Pydantic-parse bug (verify a code is still sent).
2. **Grant/isolation probe (the ship-gate)** — after migration assert, via the **client SDK / PostgREST as `authenticated`** (never the SQL editor — it runs as owner): `has_function_privilege('authenticated','public.get_team_usage(text,date[])','EXECUTE')` = **false**; `has_schema_privilege('authenticated','public','CREATE')` = **false**; team-A calling `get_team_usage(p_team='B')` → 403/empty; team-A `select`/`upsert` into team-B → denied.
3. **New `sb_secret_` header shape** — confirm `apikey`-only works for the Worker's `/rest/v1/rpc` calls.
4. **Signup gate** — confirm the trigger/hook fires on OTP user creation, `raw_app_meta_data` mutation persists into the JWT `app_metadata`, and a raised exception cleanly aborts signup (tier for the Before-User-Created hook if you prefer pre-email rejection). Confirm **when** OTP creates the `auth.users` row (affects F8).
5. **pg_cron** — availability + **probe `capture_finals()` month-boundary/tz math with test timestamps** (dates/tz trap zone); confirm `cron.schedule` present.
6. **Egress meter** — watch the real dashboard meter against the ~6 KB-poll / ~250 B-row model before committing to Free.
7. **Device-lock RLS predicate (only if enabled)** — confirm `active_device()` (DEFINER) doesn't recurse and its 10s-cadence cost is acceptable.

---

## Staged implementation checklist (ordered, each verifiable)

1. **Supabase project + auth config.** EU region; OTP-only (all other providers + anonymous OFF); Confirm-email ON; OTP 300 s; custom SMTP; CAPTCHA; access-token ~30 min. *Verify:* a test OTP login succeeds; a password/social login is impossible.
2. **Schema + RLS + functions + triggers + fail-closed hardening** (all DDL above). *Verify:* Security Advisor clean; spike-2 probes (function/schema privileges) both `false`.
3. **Isolation test harness (ship-gate).** Two users in domains A and B via the SDK; assert every cross-team read/write and `get_team_usage(p_team=other)` is denied/empty. *Verify:* all assertions pass.
4. **Signup gate** (denylist + `app_metadata.team`/`role` stamping). *Verify:* gmail signup rejected; `@skg-t.com` → `jwt_team()`='skg-t.com', first user `is_admin`.
5. **Desktop auth gate** (OTP→username→device claim) + keyring session. *Verify:* login, restart persists session, wrong-device self-logout fires.
6. **Push path** (two upserts, `by_name` dropped from payload). *Verify:* row lands with `by_name`=pusher username, correct team; a second user cannot overwrite the first's `(acct,device,date)` row.
7. **Read path** (overview + ledger from Supabase, reuse merge fns; poll only when Team tab visible). *Verify:* dashboard renders the pool identically to today; egress meter sane.
8. **pg_cron finals + retention prune** (probe the tz/month math first). *Verify:* `capture_finals()` on a forced last-day timestamp writes correct `finals`; prune deletes >90 d.
9. **Worker connector** — consent validates connector token→team via `resolve_connector_token`; reads via `get_team_usage`/`get_team_month` (service_role; `oauth_*.tid→team`). *Verify:* connector shows only the bound team; an expired/foreign token is rejected.
10. **Connector-token mint** in desktop (admin + fresh re-auth; `expires_at`). *Verify:* non-admin blocked; token drives claude.ai; expiry enforced.
11. **Migration** — clean cutover (or optional finals backfill). *Verify:* pool refills within seconds of first pushes post-cutover.
12. **Hardening** — swap connector read to `worker_reader` least-priv role over Hyperdrive; hash-pinned dependency lockfile; signed installer; EU DPA signed; privacy/consent note shipped. *Verify:* a leaked `worker_reader` credential can reach only `usage`/`finals`/`connector_tokens`, read-only.

Fix order for security: **CRIT-1 (step 2/3) blocks any deployment**; **HIGH-2/HIGH-3/HIGH-4 (steps 2/4) before onboarding beyond one trusted domain**; F3/F5/F6/F7 fold into steps 1/5/6/10; #5/#7/#8 into step 12.