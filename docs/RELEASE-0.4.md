# Release v0.4.0 — decision-first redesign

Desktop **0.4.0**, Android **0.3.0** (versionCode 16). The desktop app was rebuilt around
one question — *will I get through my limits at this pace?* — with a new engine, a new UI
and a new tray icon. Design: [design/V2-SPEC.md](design/V2-SPEC.md) (direction A "Native"
with the runway chart from direction B).

## What changed

**Desktop UI (new, `ctu/ui`)**
- Dashboard: Now (verdict banner with actions, runway chart, limit bullet bars with an
  even-pace tick and the projection, active session, waiting sessions, extra usage) ·
  Sessions · Team pool · History · Settings. Follows Windows light/dark, accent colour and
  12/24-hour clock; keyboard and screen-reader friendly; Windows High Contrast supported.
- One overlay replaces the mini widget and the HUD bar: a single line at rest, expands on
  hover/click, pin, snap to edges, hides in full screen, optional click-through.
- Tray: the binding limit's % in taskbar colours at the exact DPI size, a status bar for
  watch / will-hit / stale, a clock when limited; left-click flyout, right-click menu.

**Engine (`ctu` package; `claude_usage_tracker.py` is now a shim)**
- Forecast: least-squares pace, projection to the refill, time until the limit only when it
  falls before the refill, sustainable pace, levels with hysteresis, binding limit, resume
  time, plain-English verdict strings, team-pool suggestion, staleness.
- Alerts: one toast per episode (will hit / limited / refilled / context / extra usage /
  waiting / needs approval / incident opt-in); mute until refill, 1 h, quiet hours.
  **Behaviour change:** the 20% threshold pings and the per-percent 95–100% pings are gone.
- Near-live data from Claude Code's status line (`--statusline`, installed from Settings ›
  Data source; never replaces an existing status line). API polling is the fallback.
- Session hooks: waiting for reply, needs approval, cleared on prompt submit / session end;
  the hook path is stdlib-only (~35 ms).
- Live UI updates over SSE; typed settings API; v1 `/api/usage` and the relay payload kept
  for the MCP server and the phone, now carrying `decision`, `source` and per-window `pace`.
- Performance: no JSON rewrites per tick, one log reader, tray redraws only on change,
  relay heartbeat 50 s, team pushes on change.
- `--selftest` (fixtures, no network) runs on the built installer bundle and the wheel in CI.

**Fixes from the October audit** (desktop, relay, MCP, Android) — see the commit history on
this branch: local API origin guard on GET, settings.json never rewritten when it doesn't
parse, uninstall removes the hooks, scanner drift, connector OAuth hardening, widget XSS,
dead relay team stack removed, phone Chat removed, push token refresh, and more.

**Android**
- Redesigned home-screen widget: 2×1, 2×2, 4×2; limit-reached, stale, signed-out, not-paired
  and loading states; per-widget account; dynamic colour; absolute reset times.
- App fixes: rotation-safe state, visible refresh errors, cached encrypted prefs, permission
  flow, light theme, accessibility, two-column layout on wide screens.

## Breaking / migration

- Config keys for the old widget and HUD bar are ignored; overlay settings start fresh.
- The phone's Chat tab is gone (the desktop no longer reads phone commands).
- Relay: `/v1/team/*` and `/v1/acct/*/command` now return 404.

## Owner steps before releasing

1. **Relay:** `cd relay && npx wrangler deploy`, then once:
   `npx wrangler d1 execute claude-usage-team --remote --file=migrations/0002_drop_team_stack.sql`.
   Until it runs, `/oauth/*` and `/mcp` fail (phone sync is unaffected). Every claude.ai
   connector user re-authorises once. Optionally delete the old `TEAM_SEAL_KEY` secret.
2. **Android (never compiled here — the SDK host is blocked in the build environment):**
   open `android/` in Android Studio, build and fix first-build errors, run lint, then move
   compileSdk/targetSdk to 36 and upgrade dependencies (Play's target-SDK rule). Test on a
   device: rotation, unpair, notification permission flow, widget at 2×1/2×2/4×2 in light,
   dark, landscape and at 200% font size, widget reconfigure. New Play screenshots; update
   the Data safety form for the team pool.
3. **Windows test pass (untested on Windows so far):**
   - tray: left-click opens the flyout, right-click the menu; icon crisp at 100/125/150/200%
     scaling; light and dark taskbar; redraw on theme change;
   - flyout appears above the tray icon and closes on click-away / Escape;
   - overlay: no focus stealing, hover expands, pin, drag + snap, position remembered,
     hides in full screen; **click-through and opacity** (WS_EX_LAYERED on WebView2 may
     render blank on some systems — if so, disable those two options);
   - toasts with Open dashboard / Mute 1 h buttons;
   - Settings › Data source › Install with an install path containing spaces; uninstall
     removes the status line and hooks;
   - CSV export from Team pool saves a file;
   - in-app Update from 0.3.0 (installer and pipx).
4. **Still open from 0.3** (see RELEASE-0.3.md and COMPLIANCE.md): legal read of Anthropic's
   terms, DPA, Supabase Pro, CAPTCHA widget, `worker_reader` role, 2-domain isolation test,
   the privacy policy's contact address and its coverage of the team pool.
5. Tag `v0.4.0`, publish the GitHub release (installer) and PyPI.
