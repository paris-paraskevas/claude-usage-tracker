# Dashboard redesign — bento + 4-destination nav (desktop + phone)

Approved direction 2026-07-06 (mockups: `.superpowers/brainstorm/5055-*/content/{directions,hifi-bento}.html`).
Full reorganisation of both surfaces to fix the four stated problems: weak hierarchy,
tab structure/navigation, dated visual style, and clutter/density. Both surfaces share
one **design language** and one **information architecture**; each is its own
implementation plan (they ship independently — desktop first).

## Design language (shared)

Both surfaces already sit on the same warm dark Claude palette; this formalises it as
tokens and adds a type scale + card system.

- **Palette:** `--bg #100e0c` · surfaces `--panel #1a1613` / `--card #1e1a16` / raised
  `--card2 #26211c` · hairline `--line #332e28` · text `--ink #f2ede5` / `--dim #a99f93`
  / `--faint #786f65` · accent `--accent #d97757`. Usage bands unchanged: `--ok #5e9e72`
  / `--warn #cda24e` / `--hot #d4694f` (the existing `usage_style` thresholds stay).
  Phone `Theme.kt` gains `Card`/`Card2` to match (it has Bg/Panel/Panel2/Ink/Dim/Faint/Accent).
- **Type scale:** hero number 40/34 (desktop/phone), section value 24, body 12–14, label
  10 uppercase +1.2px tracking. All numerics **tabular** (mono, `font-variant-numeric`).
- **Cards:** radius 14 (phone screen frame keeps its device radius), padding 15–16, 11px
  gaps, 1px `--line` border, no heavy shadow; the hero card gets a subtle
  `--card2→--card` gradient. Accent is used sparingly — active nav, one or two key figures.
- **Motion:** keep existing bar/gauge transitions; no new animation.

## Information architecture (shared)

**Four destinations**, replacing the current five-tab layouts:

| Destination | Contains | Notes |
|---|---|---|
| **Home** | the bento (below) | the landing surface |
| **Team** | admin KPIs + roster + ledger (the v2 view) | **admin only** — hidden for members/solo |
| **History** | all-time: total tokens, stat tiles, heatmap, by-project, models | today's "All-time" |
| **Settings** | settings + Anthropic status detail + remote/team pairing | Status detail folds in here |

- **Status** stops being a top-level tab: a glanceable Ok/Errors/Down **card on Home**,
  full component list under Settings.
- **Adaptivity:** a solo user or plain member sees **Home · History · Settings** (3);
  an admin additionally gets **Team**. Desktop renders this as a nav bar; phone as its
  existing `NavigationBar` (bottom). The phone also keeps its **Chat** destination
  (remote prompts — phone-only) and **Sessions**; see the phone section for its bar.

### Home bento

Cards, in priority order (desktop lays them in a 3-column grid; phone stacks to 1):

1. **Hero — hottest limit.** The higher of 5h/weekly as a big number + bar + reset
   countdown + burn-rate/ETA, with the *other* window as a secondary bar beneath. Spans
   2 rows on desktop. (Which window is "hero" = whichever has the higher pct; ties → 5h.)
2. **Extra usage €** — used / limit, bar, % of cap (from `snap.extra`).
3. **Context** — active session context %, tokens.
4. **Sessions** — top 2 sessions with bars; "see all" → Sessions (phone) / expands.
5. **Team** (admin only) — org € this month + "N near limit" names → Team destination.
6. **Usage · 30d** — the existing history sparkline (spans 2 columns desktop).
7. **Status** — Ok/Errors/Down + description.

Reflow rule: desktop `grid-template-columns: 1.5fr 1fr 1fr` with the hero at
`grid-row: 1 / span 2`; phone is a single column in the order above. No card shows on
phone that isn't on desktop or vice-versa — same components, different flow.

## Desktop implementation (plan 1)

Single-file `claude_usage_tracker.py`, the `DASHBOARD_HTML` string (CSS `:root` +
`.tabs`/panes + JS renderers). This is a **restructure, not a rewrite of logic**:

- **Keep unchanged:** all `/api/*` endpoints and handlers; the poll loop; the widget and
  HUD-bar overlays (`WIDGET_HTML`); every data function; the snapshot shape.
- **Reuse the JS renderers** already present — `renderGauge`, `renderSpark`,
  `renderSessions`, `renderExtra`, `renderScoped`, `renderAlltime`, `renderStatusPage`,
  and the team renderers — repointing them at new card containers. The work is markup +
  CSS + the nav controller, not new rendering math.
- **Replace** the `.tabs` nav with a 4-destination nav bar and the `#tab-live` pane with
  the Home **bento** grid; move All-time under **History**, fold Status into a Home card
  + Settings section. Team pane keeps its v2 internals, restyled to the card language.
- **CSS:** promote the ad-hoc colors to the token set above; add `.bento`, `.card`,
  `.hero`, `.navbar` rules; retire `.gauges`/`.tabs` styles no longer used.
- **Verification:** the existing `tests/` stay green (logic untouched); the scratchpad
  HTTP smoke asserts the new nav + bento ids and that `/api/*` still respond; drive the
  live dashboard via the run/verify skill to eyeball Home/Team/History/Settings.

## Phone implementation (plan 2)

Native Compose (`android/.../ui/DashboardScreen.kt`, `Snapshot.kt`, `Theme.kt`). The
phone **already has a bottom `NavigationBar`**, so this is re-composing pages + adding two
data bits, not new navigation infra.

- **Snapshot.kt:** parse `extra` (`{enabled, used, limit, currency, pct}`) — currently
  dropped — and an optional compact `team_overview` (see below). Defensive as today.
- **Home (`OverviewPage`)** becomes the bento: keep the hero 5h gauge, add a **weekly**
  card, an **Extra €** card, the **Context** gauge, and a **Sessions preview** (top 2).
  The account switcher header stays (it's how you pick among paired desktops).
- **Bottom nav** (≤5): **Home · Sessions · Chat · History · Settings** for a solo user;
  when the active account is a team admin, insert **Team** and drop Sessions into a Home
  "see all" so the bar stays ≤5. (`Stats` label → **History**.)
- **Team on the phone — no new credentials.** The phone must not hold team admin tokens.
  Instead, the **admin's desktop embeds a compact `team_overview`** (members: name, 5h/
  weekly %, month €, near-limit flag; org totals) into the **E2EE snapshot** it already
  relays. So team data reaches the admin's phone encrypted, via the producer that already
  computes it; member/solo phones simply never receive the field. This reuses the same
  relay reads the Team destination already makes (overview + current/previous-month
  ledger for the € figures) **on the admin's machine only**, gated to the sync cadence,
  and skipped entirely when no phone is paired.
  - Desktop change (small, belongs to plan 2): in the poll loop, when `role == admin`,
    attach `snap["team_overview"]` = the merged overview (reuse `team_overview_merge`).
- **Theme.kt:** add `Card`/`Card2`; restyle `Card()`/tiles to the shared radii/spacing.
- **Verification:** builds only in **Android Studio** (no Gradle/Firebase in this repo's
  env) — plan 2 ends with a manual build + on-device check, then the Play Console release
  (versionCode 14→15, versionName bump, new screenshots).

## Sequencing & releases

1. **Desktop redesign** → new README screenshots (`docs/dashboard.png` + `docs/team.png`)
   → desktop release (bump `pyproject.toml` + `__version__`, `gh release create`).
2. **Phone redesign** → Android Studio build → Play Console release + new store
   screenshots (`docs/play/02-dashboard.png`).

Do the desktop first; only screenshot/release once the new UI is in.

## Not changing

Data layer, relay routes, snapshot **transport** (contract just gains the optional
`team_overview` field, which older phones ignore), auth/org-binding, crons, the widget
and HUD-bar overlays, phone Chat/remote-prompt behaviour.

## Out of scope (YAGNI)

Light theme, drag-to-reorder cards, per-user card preferences, desktop-side "logged into
all accounts" (the phone already multi-accounts via pairing; desktop stays single-account).
