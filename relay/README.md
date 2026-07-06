# Relay (Cloudflare Worker)

Two features on one Worker:
- **Phone sync** — a zero-knowledge dumb pipe forwarding **end-to-end-encrypted** blobs
  (never sees the `e2eeKey` or any plaintext). Stored in **KV**. Contract:
  [`../docs/REMOTE.md`](../docs/REMOTE.md).
- **Team mode** — an admin aggregator storing plaintext usage numbers in **D1** (SQLite).
  Contract: [`../docs/TEAM.md`](../docs/TEAM.md).

## Deploy

```bash
cd relay
npm install
npx wrangler kv namespace create KV          # paste the id into wrangler.toml
npx wrangler kv namespace create KV --preview # paste the preview_id into wrangler.toml
npx wrangler deploy                          # prints your https://<worker> URL
```

## Team database (D1) — only if you use team mode

```bash
npx wrangler d1 create claude-usage-team               # paste database_id into wrangler.toml
npx wrangler d1 execute claude-usage-team --file=schema.sql          # apply schema (remote)
npx wrangler d1 execute claude-usage-team --local --file=schema.sql  # ...and for `wrangler dev --local`
npx wrangler secret put TEAM_SEAL_KEY                  # 32-byte base64; enables token escrow
```

## Push (optional, enables phone notifications)

Create a Firebase project + a service account, then:

```bash
npx wrangler secret put FCM_PROJECT_ID       # e.g. my-firebase-project
npx wrangler secret put FCM_CLIENT_EMAIL     # service-account client_email
npx wrangler secret put FCM_PRIVATE_KEY      # service-account private_key (full PEM)
```

Without these, everything works except push (`POST /v1/.../push` returns `503
fcm_unconfigured`).

## Local check / dev

```bash
npm run check        # bundle + validate (no deploy)
npm run dev          # local server with simulated KV (miniflare)
```

## API

Phone sync — see [`../docs/REMOTE.md`](../docs/REMOTE.md); all routes require
`Authorization: Bearer <readToken>`; the first `PUT .../snapshot` pins the token hash
(trust-on-first-use); KV with a 7-day TTL forgets accounts that stop syncing.
Team mode — see [`../docs/TEAM.md`](../docs/TEAM.md); `/v1/team/*` routes backed by D1,
with the cron pruning old rows in place of a TTL.
