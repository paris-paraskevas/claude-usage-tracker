# Team tab v2 — roster with per-device tokens & calendar-month €

Approved 2026-07-06 (visual mockup: `.superpowers/brainstorm/*/content/layout-v2.html`).
Reshapes the Team tab built earlier the same day (docs/TEAM.md) **before first release** —
no live deployments, so schema changes need no migration.

## Approved layout (admin view)

1. **KPI strip** (two cards):
   - **Org spend since the 1st** — sum of every member's *computed* calendar-month €,
     with member count as the subline.
   - **Near limits now** — count of members with any window ≥ 80%, plus name + window
     ("Paris 5h · Nikos weekly") as the subline. Red accent when count > 0.
2. **Member roster** — one row per member:
   - name · last-seen · escrow state (left, fixed width)
   - 5-hour bar and weekly bar, each captioned `label · pct% · reset` (existing color bands)
   - right: **€ since the 1st** (big) over `% of cap` subline
   - **device chips line** underneath: one chip per device — `HOSTNAME  <tokens this month>`
     — plus a summed `NN.NM this month` tail when there is more than one device.
3. **Monthly ledger** card (unchanged behaviour, one new column): member · **€ month** ·
   **tokens** · cap · days · state, month nav ‹ › + CSV export.
4. Member (non-admin) view and the join/create cards keep their v1 look.

Semantics shown in-UI: € figures are calendar-month (delta-computed; the mid-month
billing-anchor reset cannot distort them). Tokens come from each machine's local Claude
Code logs; claude.ai web/mobile burn appears in € and the limit bars only.

## Data model changes

### Desktop → relay report row (v2 fields added)
- `did` — device id: 8 random bytes b64url, minted once per install, stored in `team.json`.
- `device` — display name, `socket.gethostname()` truncated to 32 chars.
- `tok_month` — this device's tokens for the current calendar month: sum of
  `_at_tokens(day)` over `alltime_cache["days"]` keys with the current `YYYY-MM` prefix
  (local dates; headline metric in+out+cache-writes, consistent with the All-time tab).

### Relay KV (device becomes part of the day key)
- `tday:{tid}:{date}:{mid}:{did}` — one row per member **per device** per local day.
  Fixes a v1 latent bug: two machines on one join code no longer clobber each other.
  Write throttle (60 s) now applies per device key.
- Cron rows use the reserved device id **`account`** (`src:"cron"`, no `tok_month`).
  `tfinal:{tid}:{month}:{mid}` unchanged (account-level, cron-written).
- Registry, escrow, auth: unchanged. Any of a member's devices may refresh the escrowed
  token (same account token; last writer is freshest).

### Reads
- **overview** — per member: `account` = the authoritative row for today (prefer
  `src:"cron"`, else newest `ts` across devices), `devices[]` = latest row per device id
  (excluding `account`), escrow presence. Today/yesterday resolved in team tz as before.
- **ledger** — returns raw rows grouped `days{date{mid{did:row}}}`; consumers pick the
  account sample per day (same rule: cron beats push, else newest ts).

### Desktop computations (testable, pure)
- `_day_account_row(devrows)` — cron-first / newest-ts selection.
- `team_month_spend` unchanged; `_ledger_samples` now feeds it account samples.
- `member_month_tokens(month_rows)` — per device take the **last** `tok_month` value in
  the month (it is cumulative per device), then sum devices.
- `/api/team/overview` proxy merges: per-member `month_spend` (from current-month ledger
  + prev-month baseline) and `month_tokens`; KPI aggregates (org spend, near-limits list,
  member count). The dashboard JS renders only — no math in the browser beyond formatting.

## Not changing
Auth model, join codes, escrow crypto, crons/timezone logic, member view, phone sync,
docs/TEAM.md trust model (gets a device paragraph + row-shape update).

## KV budget
Unchanged per machine: each running install writes ~4 rows/hour (default 900 s cadence).
Typical work-hours use: 10 devices × ~40 pushes ≈ 400/day + escrow/cron ≈ 30 + phone sync
288 ≈ 720/day — inside the free tier. Ten *always-on* devices (96/day each) would exceed
it; the dashboard's existing guidance applies — raise `team_report_seconds` as the team
grows.

## Testing
- Unit (new): device-id mint/persist; `tok_month` month-prefix sum; account-row selection
  (cron over push, newest-ts tiebreak); `member_month_tokens` cumulative-last-sum;
  ledger spend with device rows; overview merge aggregates (org spend, near-limits).
- Relay smoke (wrangler dev --local): two devices, one member — distinct keys, no
  clobber; throttle per device; cron `account` row coexists; overview/ledger shapes.
- Dashboard smoke: HTML carries the new ids; proxy endpoints return merged shapes.
