-- 0002 — remove the dead D1 team stack; add connector-token revocation, refresh tokens and
-- DCR hardening to the OAuth tables.
--
-- Run ONCE against an existing database that was created from the pre-0002 schema.sql
-- (a fresh database just uses schema.sql and must NOT run this). It is applied manually:
--
--   cd relay
--   npx wrangler deploy
--   npx wrangler d1 execute claude-usage-team --remote --file=migrations/0002_drop_team_stack.sql
--
-- Timing: run it right after deploying this Worker version. Until it runs, the new Worker's
-- OAuth/MCP endpoints fail (missing ct_hash/used columns and new tables); phone sync is
-- unaffected. Running it just BEFORE the deploy is also safe — the old Worker never reads
-- the new columns, and the DROPs only break its dead /v1/team routes and ledger cron.
--
-- Not idempotent: SQLite has no "ADD COLUMN IF NOT EXISTS", so a second run fails at the
-- first ALTER ("duplicate column name") — harmless, nothing after it is needed again.
--
-- Effect on existing claude.ai connections: access tokens issued before this migration have
-- no ct_hash and are rejected (401), so each connected user re-authorizes once, pasting a
-- current connector token. After that, refresh tokens keep them connected.

-- 1) OAuth hardening ----------------------------------------------------------
ALTER TABLE oauth_codes   ADD COLUMN ct_hash TEXT;
ALTER TABLE oauth_tokens  ADD COLUMN ct_hash TEXT;
ALTER TABLE oauth_clients ADD COLUMN used INTEGER;

CREATE TABLE IF NOT EXISTS oauth_refresh (
  token_hash TEXT PRIMARY KEY,
  client_id  TEXT NOT NULL,
  tid        TEXT NOT NULL,
  ct_hash    TEXT NOT NULL,
  exp        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_reg_rl (
  ip  TEXT NOT NULL,
  win INTEGER NOT NULL,
  n   INTEGER NOT NULL,
  PRIMARY KEY (ip, win)
);

-- Pre-0002 access tokens can't be revocation-checked; drop them now rather than at expiry.
DELETE FROM oauth_tokens WHERE ct_hash IS NULL;
DELETE FROM oauth_codes  WHERE ct_hash IS NULL;

-- 2) Drop the dead team stack (pool data now lives in Supabase) ---------------
DROP INDEX IF EXISTS idx_usage_tid_date;
DROP TABLE IF EXISTS escrow;
DROP TABLE IF EXISTS finals;
DROP TABLE IF EXISTS usage_rows;
DROP TABLE IF EXISTS accounts;
DROP TABLE IF EXISTS members;
DROP TABLE IF EXISTS teams;
