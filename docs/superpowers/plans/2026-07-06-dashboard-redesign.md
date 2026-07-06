# Dashboard Redesign Implementation Plan

> **For agentic workers:** staged restructure of visual code (embedded HTML/CSS/JS + Compose). Not pure TDD — each stage is gated by compile + HTTP smoke + a live visual check. Steps use `- [ ]`.

**Goal:** Reorganise both dashboards to the approved bento + 4-destination design (`docs/superpowers/specs/2026-07-06-dashboard-bento-redesign-design.md`).

**Architecture:** Desktop is a `DASHBOARD_HTML` string in `claude_usage_tracker.py`; restructure markup/CSS/nav while **reusing every existing JS renderer and all `/api` endpoints**. Phone is native Compose; re-compose pages and add `extra` + embedded `team_overview` parsing. Desktop ships first.

**Tech Stack:** Python stdlib, embedded HTML/CSS/vanilla-JS, Kotlin/Compose, pytest, wrangler.

---

## PLAN 1 — Desktop (branch `feat/dashboard-redesign`)

Safe-stage approach: additive CSS first, then nav, then Home bento, then fold/cleanup, then restyle. Commit per stage. After every stage: `py_compile` + the HTTP smoke; after Stage C+ also drive the live app.

### Task D0: Baseline snapshot for regression
- [ ] **D0.1** Run `.venv/Scripts/python.exe -m pytest tests/ -q` → expect all pass.
- [ ] **D0.2** Save the current dashboard for before/after: launch the app headless server path via the HTTP smoke and note it serves 200. (No commit.)

### Task D1: Design tokens + card/bento/nav CSS (additive)
**Files:** `claude_usage_tracker.py` (the `<style>` block in `DASHBOARD_HTML`).
- [ ] **D1.1** In `:root`, add the token set from the spec (`--card`, `--card2`, and align existing `--bg/--panel/--panel2/--line/--ink/--dim/--faint/--accent` to the spec hexes; keep `--ok/--warn/--hot`). Keep existing var names so nothing breaks.
- [ ] **D1.2** Append new rules: `.navbar`/`.navbar a`/`.navbar a.on`; `.bento`/`.card`/`.hero`; `.homecard` helpers (hero number, mini bars, sparkline card). Do **not** remove `.tabs`/`.gauges` yet.
- [ ] **D1.3** `py_compile` + HTTP smoke → still `SMOKE-OK` (no visual change yet). Commit: `style(dashboard): add design tokens + card/bento/nav CSS`.

### Task D2: 4-destination nav + rename panes
**Files:** `DASHBOARD_HTML` markup + the tab-switch JS.
- [ ] **D2.1** Replace the `.tabs` markup (`Live/All-time/Team/Status/Settings`) with `.navbar` markup: `Home · Team · History · Settings` (data-t = `home|team|history|settings`).
- [ ] **D2.2** Rename pane `#tab-alltime` → `#tab-history` (and its title). Rename `#tab-live` → `#tab-home` (contents replaced in D3). Delete the `#tab-status` **button** (pane kept for now, hidden).
- [ ] **D2.3** Update the tab-switch JS: destinations `home/team/history/settings`; `home`→renderHome, `history`→renderAlltime, `team`→renderTeamPage, `settings`→renderSettings. Admin-gate the Team nav item (hide unless `LASTD.team?.role==='admin'`).
- [ ] **D2.4** `py_compile` + smoke (update smoke needle `data-t="home"`). Commit: `feat(dashboard): 4-destination nav (Home/Team/History/Settings)`.

### Task D3: Home bento
**Files:** `DASHBOARD_HTML` `#tab-home` markup + new `renderHome()` JS.
- [ ] **D3.1** Markup: bento grid with card containers — `#home-hero`, `#home-extra`, `#home-ctx`, `#home-sessions`, `#home-team` (admin), `#home-spark`, `#home-status`.
- [ ] **D3.2** `renderHome(d)`: pick hero window = higher of five_hour/seven_day pct; fill hero (big number + bar + countdown + burn via existing `project`/`eta` fields + secondary window bar). Fill extra via existing `renderExtra` logic (repoint to `#home-extra`), context from `d.context`/sessions, sessions preview (top 2) via a trimmed `renderSessions`, team-mini from `d.team`/overview, spark via `renderSpark`, status via `statusView`.
- [ ] **D3.3** Call `renderHome(d)` from `refresh()` when Home is active; keep `renderSpark(LASTH)` on resize.
- [ ] **D3.4** `py_compile` + smoke + **drive live app** (run/verify skill): Home shows hero + cards, numbers match. Commit: `feat(dashboard): Home bento (hero + cards)`.

### Task D4: Fold Status into Home + Settings; remove dead panes
- [ ] **D4.1** Move the full Anthropic-status component list into a Settings card (reuse `renderStatusPage` → a Settings container). Remove the old `#tab-status` pane.
- [ ] **D4.2** Remove the now-unused `#tab-live` gauges markup + `.tabs`/`.gauges` CSS that nothing references. Keep `renderGauge` only if still used by the hero; else drop.
- [ ] **D4.3** `py_compile` + smoke + live check (Settings shows status; no orphan tab). Commit: `refactor(dashboard): fold Status into Home/Settings, drop dead panes`.

### Task D5: Restyle Team + History to the card language
- [ ] **D5.1** Team pane: wrap KPIs/roster/ledger in `.card`; apply tokens (already close from team v2). History pane: stat tiles + heatmap + projects into `.card`s with the new spacing.
- [ ] **D5.2** `py_compile` + full `pytest` + smoke + live check across all four destinations. Commit: `style(dashboard): restyle Team + History as cards`.

### Task D6: Desktop verification + screenshots + release prep
- [ ] **D6.1** Full sweep: `pytest -q`, `node --check relay/src/index.js` (untouched, sanity), HTTP smoke, live drive of Home/Team/History/Settings + widget + HUD bar still work.
- [ ] **D6.2** Capture new screenshots → `docs/dashboard.png` (Home), `docs/team.png`, refresh `docs/alltime.png`→ History, `docs/status.png` (now in Settings) or retire it; update README `<img>` refs. Commit: `docs: new dashboard screenshots`.
- [ ] **D6.3** Release: bump `pyproject.toml` version + `__version__` (0.1.34 → 0.2.0, a UI-major), `gh release create v0.2.0` (triggers PyPI + installer). **Ask the user before creating the release.**

---

## PLAN 2 — Phone (after desktop; needs Android Studio to build)

### Task P1: Desktop embeds `team_overview` in the snapshot (admin only)
**Files:** `claude_usage_tracker.py` poll loop.
- [ ] **P1.1** When `load_team_identity().role == 'admin'` and a phone is paired (`remote_enabled` + paired), fetch the same overview+ledger the `/api/team/overview` proxy builds, run `team_overview_merge`, and attach a **compact** projection (per member: name, fh_pct, sd_pct, month_spend, near flag; org totals) as `snap["team_overview"]`. Gate to the remote sync cadence; skip if no phone paired.
- [ ] **P1.2** Test: extend `tests/test_team.py` with a `team_overview_compact(...)` pure fn (name/pct/€/near) and assert its shape. `pytest -q`. Commit: `feat(team): embed compact team_overview in the admin snapshot`.

### Task P2: Snapshot.kt parses extra + team_overview
**Files:** `android/.../Snapshot.kt`.
- [ ] **P2.1** Add `extra: ExtraUsage?` (`enabled, used, limit, currency, pct`) and `teamOverview: TeamOverview?` (org totals + member rows) data classes + defensive parse.
- [ ] **P2.2** (No unit harness on device here — parse is exercised by the build; keep it total/defensive.) Commit: `feat(android): parse extra usage + team overview from snapshot`.

### Task P3: Home bento + Theme
**Files:** `android/.../ui/DashboardScreen.kt`, `ui/Theme.kt`.
- [ ] **P3.1** `Theme.kt`: add `Card`/`Card2` colors matching spec.
- [ ] **P3.2** `OverviewPage`: recompose to bento — hero 5h gauge (kept), + Weekly card, Extra € card (from `s.extra`), Context gauge, Sessions preview (top 2). Reuse `GaugeStat`/`Bar`.
- [ ] **P3.3** Restyle `Card()`/`StatTile` to shared radii/spacing. Commit: `feat(android): Home bento + refreshed card styling`.

### Task P4: Team destination (admin) + bottom nav
**Files:** `android/.../ui/DashboardScreen.kt`.
- [ ] **P4.1** Add a `TeamPage` composable rendering `s.teamOverview` (KPIs + member rows with bars + € + near-limit). Show empty/"admin only" when absent.
- [ ] **P4.2** Bottom nav: when `s.teamOverview != null`, show `Home · Team · Chat · History · Settings` (Sessions → a Home "see all" row); else `Home · Sessions · Chat · History · Settings`. Relabel `Stats`→`History`. Commit: `feat(android): Team destination + adaptive bottom nav`.

### Task P5: Phone verification + release
- [ ] **P5.1** Build in Android Studio (debug), pair with an admin desktop, verify Home bento, Team page, parity look. (Manual — cannot build in this env.)
- [ ] **P5.2** Bump `versionCode` 14→15, `versionName`; build signed AAB; Play Console release notes; new store screenshots `docs/play/02-dashboard.png`. **User-driven.**

---

## Self-review notes
- Every spec section maps to a task: design tokens→D1; IA/nav→D2; Home bento→D3; Status fold→D4; Team/History restyle→D5; screenshots/release→D6; team-on-phone→P1/P2/P4; phone bento→P3; phone release→P5.
- No logic/data/relay/endpoint changes on desktop (restructure only) — regression guard is the unchanged `tests/` + HTTP smoke.
- Reused names verified against source: `renderGauge/renderSpark/renderExtra/renderScoped/renderSessions/renderAlltime/renderStatusPage/renderSettings/renderTeamPage`, `team_overview_merge`, `GaugeStat/Bar/Card/StatTile`, colors `Bg/Panel/Panel2/Ink/Dim/Faint/Accent`.
