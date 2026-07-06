-- Claude Usage Tracker relay — team-mode schema (Cloudflare D1 / SQLite).
-- Apply:  npx wrangler d1 execute claude-usage-team --file=schema.sql            (remote)
--         npx wrangler d1 execute claude-usage-team --local --file=schema.sql     (local dev)
-- Phone sync stays in KV (E2EE blobs); only team mode lives here. See docs/TEAM.md.

CREATE TABLE IF NOT EXISTS teams (
  tid        TEXT PRIMARY KEY,
  admin_hash TEXT NOT NULL,                    -- sha256(admin readToken), trust-on-first-use
  tz         TEXT NOT NULL DEFAULT 'Europe/Athens',
  org        TEXT                              -- Claude org uuid; null = org binding off
);

CREATE TABLE IF NOT EXISTS members (
  tid        TEXT NOT NULL,
  mid        TEXT NOT NULL,
  token_hash TEXT NOT NULL,                    -- sha256(member readToken)
  name       TEXT,
  PRIMARY KEY (tid, mid)
);

-- One row per member DEVICE per LOCAL day. did='account' is the cron's account-level row.
CREATE TABLE IF NOT EXISTS usage_rows (
  tid            TEXT NOT NULL,
  date           TEXT NOT NULL,                -- YYYY-MM-DD in the team's tz
  mid            TEXT NOT NULL,
  did            TEXT NOT NULL,
  name           TEXT,
  fh_pct         REAL,
  sd_pct         REAL,
  fh_resets_at   TEXT,
  sd_resets_at   TEXT,
  extra_enabled  INTEGER,                      -- NULL = no extra block; else 0/1
  extra_used     REAL,
  extra_limit    REAL,
  extra_currency TEXT,
  extra_pct      REAL,
  tok_month      INTEGER,
  device         TEXT,                         -- hostname (per-device rows); NULL for 'account'
  src            TEXT,                         -- 'push' | 'cron'
  ts             INTEGER,                      -- report time (epoch seconds)
  wts            INTEGER,                      -- last write (epoch ms) — the per-device throttle
  PRIMARY KEY (tid, date, mid, did)
);
CREATE INDEX IF NOT EXISTS idx_usage_tid_date ON usage_rows (tid, date);

-- Frozen month-end row (written by the 23:59 cron on the last local day). Never pruned.
CREATE TABLE IF NOT EXISTS finals (
  tid            TEXT NOT NULL,
  month          TEXT NOT NULL,                -- YYYY-MM
  mid            TEXT NOT NULL,
  name           TEXT,
  fh_pct         REAL,
  sd_pct         REAL,
  fh_resets_at   TEXT,
  sd_resets_at   TEXT,
  extra_enabled  INTEGER,
  extra_used     REAL,
  extra_limit    REAL,
  extra_currency TEXT,
  extra_pct      REAL,
  tok_month      INTEGER,
  ts             INTEGER,
  PRIMARY KEY (tid, month, mid)
);

CREATE TABLE IF NOT EXISTS escrow (
  tid TEXT NOT NULL,
  mid TEXT NOT NULL,
  iv  TEXT NOT NULL,                           -- AES-GCM nonce (b64url)
  ct  TEXT NOT NULL,                           -- sealed access token (b64url)
  exp INTEGER NOT NULL,                        -- token expiry (epoch ms)
  PRIMARY KEY (tid, mid)
);

-- Phone-sync snapshots. Moved off KV so the desktop can push every ~10s without
-- blowing KV's 1,000-writes/day free cap (D1 allows 100k/day). Still E2EE: the row
-- holds only the opaque ciphertext blob; the auth-token hash stays in KV. The cron
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
