-- Claude Usage Tracker relay — D1 (SQLite) schema for a FRESH database.
-- Apply:  npx wrangler d1 execute claude-usage-team --remote --file=schema.sql   (remote)
--         npx wrangler d1 execute claude-usage-team --local  --file=schema.sql   (local dev)
-- Upgrading an existing database instead? Apply the files in migrations/ (see relay/README.md).
--
-- What lives where:
--   D1 (this file) — phone-sync snapshot ciphertext + the claude.ai connector's OAuth state.
--   KV             — per-account phone-sync auth hash and FCM push-token set.
--   Supabase       — the team account pool itself (read by the connector via service_role RPCs).
-- The old D1 team stack (teams/members/accounts/usage_rows/finals/escrow) is gone; see
-- migrations/0002_drop_team_stack.sql.

-- OAuth 2.1 provider (docs/MCP-REMOTE.md) for the claude.ai remote MCP connector.
-- Codes and tokens are stored only as sha256 hashes. Consent takes a connector token (minted by
-- a team admin in the desktop app); `tid` is the team (email domain) it resolved to, and
-- `ct_hash` is sha256(connector token) so every request can re-check it hasn't been revoked.
CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id     TEXT PRIMARY KEY,
  redirect_uris TEXT NOT NULL,                 -- JSON array of exact-match redirect URIs (≤5)
  name          TEXT,                          -- client_name (≤64 chars), shown on the consent page
  created       INTEGER,                       -- epoch ms
  used          INTEGER                        -- epoch ms of the last grant/refresh; NULL = never used
);
CREATE TABLE IF NOT EXISTS oauth_codes (
  code_hash      TEXT PRIMARY KEY,             -- sha256(authorization code)
  client_id      TEXT NOT NULL,
  redirect_uri   TEXT NOT NULL,
  code_challenge TEXT NOT NULL,                -- PKCE S256 challenge
  tid            TEXT NOT NULL,                -- team this grant authorizes
  exp            INTEGER NOT NULL,             -- short-lived (epoch ms)
  ct_hash        TEXT                          -- sha256(connector token) the grant came from
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
  token_hash TEXT PRIMARY KEY,                 -- sha256(access token)
  tid        TEXT NOT NULL,
  exp        INTEGER NOT NULL,                 -- epoch ms
  ct_hash    TEXT                              -- sha256(connector token); NULL rows are rejected
);
CREATE TABLE IF NOT EXISTS oauth_refresh (
  token_hash TEXT PRIMARY KEY,                 -- sha256(refresh token); rotated on every use
  client_id  TEXT NOT NULL,
  tid        TEXT NOT NULL,
  ct_hash    TEXT NOT NULL,
  exp        INTEGER NOT NULL                  -- epoch ms (90 days from issue)
);
-- Dynamic Client Registration rate limit: registrations per (hashed) client IP per clock hour.
CREATE TABLE IF NOT EXISTS oauth_reg_rl (
  ip  TEXT NOT NULL,                           -- truncated sha256 of CF-Connecting-IP
  win INTEGER NOT NULL,                        -- epoch hour
  n   INTEGER NOT NULL,
  PRIMARY KEY (ip, win)
);

-- Phone-sync snapshots. In D1 (not KV) so the desktop can push every ~10s without
-- blowing KV's 1,000-writes/day free cap (D1 allows 100k/day). Still E2EE: the row
-- holds only the opaque ciphertext blob; the auth-token hash stays in KV. The daily cron
-- prunes rows past `exp` (KV used to expire these automatically).
CREATE TABLE IF NOT EXISTS snapshots (
  account_id TEXT PRIMARY KEY,
  v          INTEGER,
  nonce      TEXT,                             -- E2EE nonce (b64)
  ct         TEXT,                             -- secretbox ciphertext (b64)
  ts         INTEGER,                          -- producer timestamp (epoch s)
  wts        INTEGER,                          -- last write (epoch ms) — the throttle
  exp        INTEGER                           -- expiry (epoch ms) — the 7-day forget
);
