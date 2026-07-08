# Release v0.3.0 — team mode → Supabase account pool

Team mode moves off the D1 relay + join codes to a **Supabase account pool** with
**email-OTP sign-in (team = your email domain)**. Distribution decision: **hosted for
everyone** — the app ships with the maintainer's Supabase URL + publishable key, so every
installer signs into one EU Supabase, isolated per email domain by RLS. The maintainer is
therefore host + payer + **data controller** for all installers' Claude-usage PII.

**Breaking:** 0.2.x team users re-onboard via OTP; join codes + token escrow are gone.

## Status — Supabase wiring COMPLETE (branch `feat/supabase-pool-client`, not pushed)
Slices 1–6 done, verified, committed:
- `a61c994` stdlib `supabase_pool` client (urllib → GoTrue + PostgREST; keyring session)
- `6de4327` push path (`TeamSync` → pool upsert)
- `00fcdf6` team-tab reads from the pool
- `6560b44` `_team_action` → login/logout/set-username/connector-token
- `05efe96` Team-tab auth-gate + pool UI (Playwright-verified: login → pool → logout)
- `ec7bbe6` poll-loop snapshot + phone embed → Supabase
- `e2db018` deleted dead D1 helpers + obsolete tests (pytest 52 passed)

Live server verified: schema/RLS/hardening (5 migrations), advisor clean, ship-gate
assertions, real OTP signup → JWT `app_metadata.team`, push/read, admin connector-token mint.

## Gates before a public publish

### Legal / consent (owner)
- [ ] **Anthropic ToS/consent read** — 3rd-party app centralizing identifiable Claude usage (#7).
- [ ] **Supabase DPA** signed (EU region set).
- [ ] **In-app consent** before first sign-in (checkbox: central EU DB, not E2EE, domain-visible). *(building)*

### Security hardening (Supabase dashboard = owner; code = maintainer)
- [ ] CAPTCHA (Turnstile) ON; OTP expiry 300 s; access-token (JWT) 30 min; custom SMTP with sane per-hour limits.
- [ ] Signup gate: free-mail **denylist** ON, allowlist OFF (any corporate domain; free-mail rejected). First-signup-per-domain = admin (or switch to invite-based).
- [ ] Supabase **Pro** + egress monitoring (Free 5 GB will 402 under public load).
- [ ] (Residual) `worker_reader` least-priv role over the connector RPCs instead of raw `service_role`.

### Feature completeness (maintainer)
- [ ] **Worker connector → Supabase**: relay reads the pool via `resolve_connector_token` + `get_team_usage`/`get_team_month` (service_role RPCs). Needs `SUPABASE_SECRET` via `wrangler secret put` + a deploy. Else the desktop "mint connector token" produces tokens the claude.ai connector can't use.
- [ ] Remove the phone chat-answering feature (`remote_accept_prompts` + armed prompt runner).
- [ ] repo-sync `supabase/migrations/0001_pool.sql` to the deployed state (diverged: is_admin INVOKER, deeper revokes, capture_finals fixes, pg_cron).
- [ ] 2-domain cross-tenant isolation test before onboarding a 2nd domain.

## Screenshots
- **Desktop (maintainer, via Playwright):** Team-tab **login** (with consent) · Team-tab **signed-in pool** (seed 2–3 demo accounts for a non-empty shot, then clean up) · optional admin "mint token". Home/bento reused (unchanged).
- **Phone (owner):** team overview on device/emulator (Android Studio only in this repo).

## Instructions to rewrite
- `README.md` team section + `docs/TEAM.md`: "sign in with your work email → your team is your domain → the pool fills as teammates sign in; admin mints a connector token for the claude.ai connector." Add the consent/privacy note.
- Release notes (GitHub + PyPI): breaking-change/re-onboard; new sign-in flow; escrow removed.
- Mark `docs/SUPABASE-MIGRATION.md` slices 1–6 done.

## Sequence
1. Legal read + DPA (owner). 2. Consent gate (code) ✅. 3. Worker connector + phone-feature removal + repo-sync (code). 4. Supabase hardening + Pro (owner). 5. Screenshots + docs (docs ✅; screenshots captured as proof-of-look, need a clean scrubbed capture). 6. Bump `0.3.0`, tag, GitHub release, PyPI (`pipx`). Merge branch first.

## Worker connector — implementation spec (deploy session; needs Cloudflare auth + `SUPABASE_SECRET`)
Migrate the relay's claude.ai connector from D1 to the Supabase pool. Keep D1/KV for phone sync + FCM. In `relay/src/index.js`:
- Add `async function rpc(env, fn, body)` → `POST ${env.SUPABASE_URL}/rest/v1/rpc/${fn}` with headers `apikey: env.SUPABASE_SECRET` + `Authorization: Bearer ${env.SUPABASE_SECRET}`, JSON body; return `res.json()`.
- `mcpResolveTeam` (617) + OAuth consent (1021–1088): validate the pasted **connector token** via `rpc(env,'resolve_connector_token',{p_hash: sha256hex(token)})` → **team (domain)**; store `team` in `oauth_codes`/`oauth_tokens` (repurpose the `tid` column to hold the domain).
- `teamOverviewData(env, team, tz)` (362): swap the D1 `SELECT … usage_rows` for `rpc(env,'get_team_usage',{p_team:team, p_dates:[today,yesterday]})`; reshape via `rowFromDb` (device = `device` col) into the existing `{tz,today,accounts[]}` shape.
- `teamLedgerData(env, team, month)` (394): swap for `rpc(env,'get_team_month',{p_team:team, p_month:month})` (rows `{kind:'usage'|'final', r}`); split into `days`/`finals`, reshape via `rowFromDb`.
- `overviewMerge`/`ledgerComputed`/`memberMonthTokens` (451–513) + `handleMcp` (665/681): unchanged except pass `team` instead of `tid`.
- `relay/wrangler.toml`: add `[vars] SUPABASE_URL = "https://sxciunvkygtehhztfjjo.supabase.co"`. Secret out-of-band: `cd relay && npx wrangler secret put SUPABASE_SECRET` (the `sb_secret_…` key — never in the repo), then `npx wrangler deploy`. Add a startup assertion `SUPABASE_SECRET !== publishable key`.
- **Verify:** claude.ai connector consent with a freshly-minted token → `get_team_overview` returns the domain's pool; an expired/foreign token is rejected.
