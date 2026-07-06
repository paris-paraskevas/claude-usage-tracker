# Team mode — admin dashboard & monthly extra-usage ledger

Aggregates a **Claude Team plan** on a relay the admin owns: every member's live
5-hour / weekly load and extra-usage spend, plus a per-member **monthly € ledger**
whose decisive sample is captured at **23:59 (team time) on the last day of the
month**. Optional and off unless a team is created/joined.

> **Why it works this way:** Anthropic has no API that reports *billed extra-usage
> money per member*. The Claude Code Analytics API returns *estimated* API-rate
> costs (not what you're billed) and only covers Claude Code. The only exact
> source is each member's own `GET /api/oauth/usage`, which answers **only for
> the calling account** — so each member's tracker reports for itself, and the
> relay aggregates.

## Trust model — different from phone sync, deliberately

| | Phone sync (`/v1/acct`) | Team mode (`/v1/team`) |
|---|---|---|
| payload | full snapshot, **E2EE** | compact row, **plaintext** |
| relay can read it | no | yes (it's the admin's own Worker) |
| content shared | — | usage **numbers only**: 5h/weekly %, reset times, extra-usage € used/cap. Never sessions, projects, paths, or conversation text. |

Members additionally **opt in** (`team_share_token`, on by default, toggle in the
Team tab) to escrowing their **short-lived OAuth access token** (lifetime: hours)
so the relay's nightly cron can read their usage at day's end with their machine
off. The **refresh token never leaves the member's machine**; escrowed tokens are
sealed with AES-GCM under the `TEAM_SEAL_KEY` Worker secret, are never returned by
any route, and expire from KV on the token's own TTL. Without escrow the ledger
falls back to the member's **last push of the day** — exact unless they keep using
claude.ai (web/mobile) after their desktop sleeps.

Identity and auth:

| id | bytes | held by | purpose |
|----|-------|---------|---------|
| `teamId` | 16 random → base64url | admin, members, relay | routing/storage namespace |
| admin token | 32 random → base64url | **admin only** (relay stores sha256) | all `/v1/team` admin calls; pinned trust-on-first-use at `POST /init` |
| member token | 32 random → base64url | that member (relay stores sha256) | that member's report/escrow calls |

Members are **pre-registered by the admin** (the relay learns the token *hash*
before the member ever connects), so there is no first-come race on member slots.
The join code (`cutteam1:<base64url(JSON {u,t,m,k,n})>`) is the only place the
plaintext member token travels — send it privately; re-adding the member voids it.

## Setup

**Admin (once):**
1. Deploy the relay as in `docs/REMOTE.md` (same Worker serves both features).
2. Enable escrow + the cron:
   `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`
   → `npx wrangler secret put TEAM_SEAL_KEY`, then `npx wrangler deploy` (picks up
   the `[triggers]` crons).
3. In the tracker: **Team tab → Create team** (uses the relay URL from
   Settings → Remote; enrolls you as the first member), then **Add member** per
   teammate and send each code privately.

**Each member:** install the tracker (installer or
`pip install claude-usage-tracker`), then **Team tab → paste the join code → Join**.
The tracker must run for live rows (it's their personal usage app anyway); with
escrow on, the 23:59 ledger works even when it doesn't run all evening.

## Relay API

Base `https://<worker>`; bearer = admin token (A) or member token (M).

| Method + path | auth | body → result |
|---|---|---|
| `POST /v1/team/{tid}/init` | A (TOFU) | `{tz?}` → `{ok, tz}`; pins the admin hash, sets the team clock (default `Europe/Athens`) |
| `PUT /v1/team/{tid}/member/{mid}` | A | `{token_hash, name}` → 204 (pre-register / rotate a member) |
| `DELETE /v1/team/{tid}/member/{mid}` | A | 204; drops registry + escrow, keeps ledger rows |
| `PUT /v1/team/{tid}/member/{mid}/report` | M | usage row → 204; ≥60 s gap; keyed to the team-tz **day** (last write wins; can't clobber a newer cron row) |
| `PUT /v1/team/{tid}/member/{mid}/token` | M | `{access_token, expires_at}` → 204; 503 if no `TEAM_SEAL_KEY` |
| `DELETE /v1/team/{tid}/member/{mid}/token` | M | 204 (withdraw escrow) |
| `GET /v1/team/{tid}/overview` | A | members + today/yesterday rows + escrow presence |
| `GET /v1/team/{tid}/ledger?month=YYYY-MM` | A | `{members, days{date{mid:row}}, finals{mid:row}}` |

Row: `{name, fh_pct, sd_pct, fh_resets_at, sd_resets_at, extra:{enabled, used, limit, currency, pct}, ts, src:"push"|"cron"}`.

KV: `tadm:{tid}`, `tmem:{tid}:{mid}`, `tday:{tid}:{date}:{mid}` (~13-month TTL),
`tfinal:{tid}:{month}:{mid}` (never expires), `tesc:{tid}:{mid}` (token TTL).

## The 23:59 cron & ledger semantics

- Crons fire **20:59 and 21:59 UTC**; the worker acts only when a team's local
  time is 23:5x, which covers UTC+2/+3 across DST for the default `Europe/Athens`.
  Other zones: add a cron entry matching their offset in `wrangler.toml`.
- On each end-of-day pass it fetches `/api/oauth/usage` per escrowed member and
  writes the day's row (`src:"cron"`); on the month's **last local day** it also
  freezes `tfinal`. Revoked tokens are dropped; transient failures leave the
  member's last push standing.
- **Calendar-month spend** (dashboard + CSV) = the sum of day-over-day *increases*
  of the member's credit meter, with the previous month's last row as baseline.
  Anthropic's credit cycle resets on the subscription anchor (mid-month), not the
  calendar month — a drop between samples is treated as a reset and the new value
  counts whole. Error bound: burn between the last pre-reset sample and the reset
  moment is lost (at most one day's, on the anchor day). The first tracked month
  has no baseline, so its first sample only seeds the diff.

## Cost (Cloudflare free tier)

Steady state per member: ~4 report writes/hour while the desktop runs
(`team_report_seconds` 900), ~2 escrow writes/day (token rotations), 1–2 cron
writes/day. A 5-member team plus the phone-sync feature stays comfortably under
the 1 000 KV writes/day free budget; nudge `team_report_seconds` up if you add
many members.
