# Team mode — shared Claude account pool

Team mode aggregates a **shared pool of Claude accounts** for everyone on your email
**domain**: each pooled account's live 5-hour / weekly load and monthly extra-usage €
spend in one **Team** tab, plus a per-account **monthly € ledger** frozen at month-end.
Optional and off until you sign in.

> **Why:** Anthropic has no API for *billed extra-usage € per account*. The only exact
> source is each account's own `GET /api/oauth/usage` (answers only for the calling
> account), so each running tracker reports its own account and the pool aggregates.

## How it works — hosted Supabase, keyed by your email domain
- **Sign in** in the Team tab with your **work email**; you get a numeric code by email
  (email-OTP — no password). Your **team is your email domain**: everyone at `@yourco.com`
  shares one pool, isolated from every other domain by Postgres **row-level security (RLS)**.
- The tracker **upserts** its account's usage row every ~10 s while it runs; the Team tab
  reads the whole pool live. Month-end ledger rows are frozen by a database cron (`pg_cron`).
- The **first person to sign up for a domain becomes its admin** (can mint the claude.ai
  connector token). Free-mail domains (gmail, outlook, …) are rejected.

## Trust model — plaintext pool, deliberately (unlike phone sync)
| | Phone sync | Team pool |
|---|---|---|
| payload | full snapshot, **E2EE** | compact row, **plaintext** |
| stored in | your own relay (Cloudflare KV) | central **Supabase (EU)** |
| readable by | only you | you + teammates on your domain (+ the service operator) |
| shared | — | **usage numbers only**: 5h/weekly %, reset times, extra-usage € used/cap, monthly token count, the Claude-account email + display name. Never sessions, projects, paths, or conversation text. |

You **consent** at sign-in that this data is stored centrally (EU, not end-to-end
encrypted) and is visible to your domain teammates. Full security model (RLS tenancy,
device-lock, signup gate): `docs/SUPABASE-MIGRATION.md`. Hosted distribution + data-controller
notes: `docs/RELEASE-0.3.md`.

## Using it
**Everyone:** install the tracker → **Team tab** → enter your work email → **Send code** →
enter the code + a display name → **Sign in**. Keep the tracker running for live rows.
Sign out any time (Team tab → **Sign out**).

**Admin (first per domain):** **Team tab → Mint connector token** → paste it at the
claude.ai custom-connector consent screen so Claude can read your team's pool.

## Data & security (summary)
- **Supabase Postgres + RLS:** `team = jwt_team()`, where `team` is an *immutable*
  `app_metadata` claim stamped from your verified email domain at signup — so cross-domain
  read/write is impossible even if the app is patched (the publishable key it ships is safe;
  `anon` is granted nothing).
- Tables: `profiles`, `accounts`, `usage` (one row per account × device × local day, ~10 s
  upsert), `finals` (frozen month-end, written by `pg_cron`), `connector_tokens`.
- **Soft device-lock** deters casual login-sharing; it is never the isolation boundary — RLS is.
- Month spend = day-over-day increases of each account's credit meter (robust to the
  mid-month billing-cycle reset; the first tracked month seeds the baseline).

## What changed from v0.2.x team mode
Join codes, admin/member tokens, token escrow, and the D1 team relay are **gone**. Existing
0.2.x teams **re-onboard** by signing in with email in the Team tab. The Cloudflare relay is
retained for phone sync (E2EE) + FCM and for serving the claude.ai connector (which reads the
Supabase pool server-side).
