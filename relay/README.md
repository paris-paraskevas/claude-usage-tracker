# Relay (Cloudflare Worker)

Two features share one Worker:

- **Phone sync** (`/v1/acct/{id}/snapshot | push-token | push`): a zero-knowledge pipe that
  forwards **end-to-end-encrypted** blobs from the desktop tracker to the Android app. It
  never sees the `e2eeKey` or any plaintext. Snapshot ciphertext is stored in **D1**
  (`snapshots`). Each account's auth-token hash and its FCM push-token set are stored in
  **KV**. Contract: [`../docs/REMOTE.md`](../docs/REMOTE.md).
- **claude.ai connector** (`/mcp` plus `/oauth/*` and `/.well-known/oauth-*`): a remote MCP
  server that serves your team's Claude account pool **read-only from Supabase**. It sits
  behind a small OAuth 2.1 provider with dynamic client registration, PKCE (S256) and
  rotating refresh tokens. On the consent screen you paste a **connector token**, which a
  team admin mints in the desktop app (Team tab → claude.ai connector → **Mint connector
  token**). Every grant is tied to that token. If the token is revoked or expires in
  Supabase, the connector stops working within about 60 s and claude.ai asks you to
  authorize again. OAuth state is stored in D1 (`oauth_*`). Details:
  [`../docs/MCP-REMOTE.md`](../docs/MCP-REMOTE.md).

The team pool itself lives in Supabase (see [`../supabase/`](../supabase/)). The relay no
longer stores any team data, and the old D1 team stack (`/v1/team/*`, token escrow, the
nightly ledger cron and `TEAM_SEAL_KEY`) has been removed.

## Deploy (fresh setup)

```bash
cd relay
npm install
npx wrangler kv namespace create KV            # paste the id into wrangler.toml
npx wrangler kv namespace create KV --preview  # paste the preview_id into wrangler.toml
npx wrangler d1 create claude-usage-team       # paste database_id into wrangler.toml
npx wrangler d1 execute claude-usage-team --remote --file=schema.sql
npx wrangler secret put SUPABASE_SECRET        # the Supabase sb_secret_… key (service role)
npx wrangler deploy                            # prints your https://<worker> URL
```

`SUPABASE_URL` is a public `[vars]` entry in `wrangler.toml`. `SUPABASE_SECRET` must only
ever be set as a secret. Without it, phone sync still works, but the connector's consent
and `/mcp` calls fail with "team directory unavailable".

## Upgrading an existing deployment to this version

Apply [`migrations/0002_drop_team_stack.sql`](migrations/0002_drop_team_stack.sql) **once**,
right after deploying:

```bash
cd relay
npx wrangler deploy
npx wrangler d1 execute claude-usage-team --remote --file=migrations/0002_drop_team_stack.sql
```

Or from GitHub: **Actions › Deploy relay › Run workflow** with migration `0002_drop_team_stack.sql`
(needs the repository secrets `CLOUDFLARE_API_TOKEN` — Workers + D1 edit — and
`CLOUDFLARE_ACCOUNT_ID`). It runs the relay tests, deploys, then applies the migration.

This migration does three things:

- It adds `ct_hash`/`used` columns and the `oauth_refresh` and `oauth_reg_rl` tables.
- It drops the dead team tables (`teams`, `members`, `accounts`, `usage_rows`, `finals`,
  `escrow`).
- It deletes access tokens issued before the migration. Each connected claude.ai user
  re-authorizes once.

Until it runs, the connector endpoints return errors, but phone sync is unaffected. The
file's header covers the timing and why it can't be run twice. You can also delete the old
`TEAM_SEAL_KEY` secret: `npx wrangler secret delete TEAM_SEAL_KEY`.

## Push (optional, enables phone notifications)

Create a Firebase project and a service account, then:

```bash
npx wrangler secret put FCM_PROJECT_ID       # e.g. my-firebase-project
npx wrangler secret put FCM_CLIENT_EMAIL     # service-account client_email
npx wrangler secret put FCM_PRIVATE_KEY      # service-account private_key (full PEM)
```

Without these secrets, everything except push still works: `POST /v1/acct/{id}/push` returns
`503 fcm_unconfigured`.

## Housekeeping

A single daily cron (`17 3 * * *` UTC) prunes expired snapshots, OAuth codes, access tokens
and refresh tokens. It also removes OAuth clients that never completed a grant within 7
days, and old registration rate-limit rows. Retention limits:

- Snapshots and push-token sets are forgotten after 7 days without activity. An active
  desktop refreshes both TTLs, at most once a day.
- Access tokens last 24 h.
- Refresh tokens last 90 days and are rotated on every use.
- Dynamic client registration is limited to 10 registrations per IP per hour.

## Tests / local dev

```bash
npm test             # node --test: pure helpers, OAuth/MCP flow, phone sync, widget (no Workers runtime)
npm run check        # bundle + validate with wrangler (no deploy)
npx wrangler d1 execute claude-usage-team --local --file=schema.sql
npm run dev          # local server with simulated KV/D1 (miniflare)
```

The tests use `node:sqlite` as a stand-in for D1, so `schema.sql` and the Worker's real
SQL run against them. They need Node 22.13 or later. CI runs them in the `node` job of
`.github/workflows/ci.yml`.

## API

- **Phone sync**: see [`../docs/REMOTE.md`](../docs/REMOTE.md). Every route needs
  `Authorization: Bearer <readToken>`. The first `PUT …/snapshot` pins the token hash
  (trust on first use).
  - `PUT` or `DELETE …/push-token` with `{"token": "<fcm token>"}` registers or removes a
    phone.
  - `POST …/push` (desktop) fans an E2EE blob out to the registered phones.
  - The old phone→desktop `…/command` channel has been removed and now returns 404.
- **Connector**: `GET /.well-known/oauth-authorization-server`,
  `GET /.well-known/oauth-protected-resource`, `POST /oauth/register`,
  `GET/POST /oauth/authorize`, `POST /oauth/token` (`authorization_code`, `refresh_token`),
  `POST /mcp` (JSON-RPC: `initialize`, `ping`, `tools/list|call`, `resources/list|read`).
  The tools are `get_team_overview`, which also renders an MCP Apps pool widget, and
  `get_team_ledger`. Months and dates follow the team's timezone from Supabase
  `public.teams.tz`, defaulting to Europe/Athens.
