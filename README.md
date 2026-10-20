<p align="center"><img src="https://raw.githubusercontent.com/paris-paraskevas/claude-usage-tracker/main/docs/icon.png" width="84" alt="Claude Usage Tracker"></p>

# Claude Usage Tracker

A Windows tray app that tells you, at a glance, whether you'll get through your Claude
plan limits: how much of the **5-hour** and **weekly** windows is left, when each refills,
and whether **your current pace** runs out before the refill. It sits in the tray, in a
one-line always-on-top overlay, and in a dashboard, and it pings you only when there's
something to act on.

> **Unofficial.** An independent, open-source project — not affiliated with, endorsed by, or
> sponsored by Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic, used only
> to describe compatibility.

<p align="center">
  <a href="https://play.google.com/store/apps/details?id=com.claudeusage.tracker"><img alt="Get the Android app on Google Play" src="https://play.google.com/intl/en_us/badges/static/images/badges/en_badge_web_generic.png" height="60"></a><br>
  <img src="https://raw.githubusercontent.com/paris-paraskevas/claude-usage-tracker/main/docs/play-qr.png" width="150" alt="Scan to get the Android app on Google Play"><br>
  <em>Companion <strong>Android</strong> app and home-screen widget (scan to install)</em>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/paris-paraskevas/claude-usage-tracker/main/docs/dashboard.png" alt="Dashboard — Now" width="760"><br>
  <em><strong>Now</strong>: the verdict, the runway chart (time until the limit vs. time until the refill) and every limit with its pace (sample data)</em>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/paris-paraskevas/claude-usage-tracker/main/docs/overlay.png" alt="Overlay at rest" width="420"><br>
  <img src="https://raw.githubusercontent.com/paris-paraskevas/claude-usage-tracker/main/docs/overlay-expanded.png" alt="Overlay expanded" width="300">
  <img src="https://raw.githubusercontent.com/paris-paraskevas/claude-usage-tracker/main/docs/flyout.png" alt="Tray flyout" width="300"><br>
  <em>The <strong>overlay</strong> (one line at rest, expands on hover) and the <strong>tray flyout</strong> (sample data)</em>
</p>

## What it answers

1. **How much is left, and when does it refill?** 5-hour and weekly windows, plus the
   per-model weekly scopes, with every refill shown as both a countdown and a clock time.
2. **Will I make it at this pace?** A least-squares pace over the last 30 minutes (weekly:
   6 hours) projected to the refill. Colour comes from that forecast, not from the raw
   percentage: 70% used ten minutes before a refill is fine; 30% used at a steep pace is not.
3. **What should I do?** "Slow to 19%/h to make it", "Mute until refill", or — with a team
   pool — "Switch to ops@acme.dev, 82% left".
4. **Which sessions need me?** Context fill per Claude Code session (compact before 90%) and
   sessions that finished a turn and are **waiting for your reply** or **need approval**.
5. **Is it me or Claude?** Anthropic's status, shown separately from your limits.

## Surfaces

- **Tray icon** — the binding limit's used % as two digits in your taskbar colours, drawn at
  the exact size for your display scaling. A bar along the bottom appears only when it
  matters: amber halves for *watch pace*, solid vermillion for *will hit the limit*, grey
  dashes when the data is stale; a clock when the limit is reached. Left-click opens the
  **flyout**; right-click opens the menu.
- **Overlay** — replaces the old mini widget and HUD bar. One always-on-top line
  (`▲ 5h 64% limit ~16:26 │ 7d 41% │ ctx 73% │ 1 waiting`) that never steals focus, expands
  in place on hover or click, can be pinned, snaps to screen edges and hides during
  full-screen apps and presentations. Fields, opacity and click-through are in Settings.
- **Dashboard** — **Now** (verdict, runway chart, limits, active session, waiting sessions,
  extra usage) · **Sessions** · **Team pool** · **History** (usage over time, totals,
  streaks, activity heatmap, models, projects) · **Settings** (gear). Light and dark follow
  Windows, as do your accent colour and 12/24-hour clock.
- **Alerts** — one toast per episode, never a stream: *will hit the limit*, *limit
  reached*, *refilled*, *context almost full*, *extra usage near its cap*, *session
  waiting*, *needs approval*, and (opt-in) *Claude incident*. Mute until the refill, for an
  hour, or set quiet hours.

<p align="center">
  <img src="https://raw.githubusercontent.com/paris-paraskevas/claude-usage-tracker/main/docs/tray-icons.png" alt="Tray icon states" width="720"><br>
  <em>Tray icon at 16 and 24 px, light and dark taskbar: on pace, watch pace, will hit limit, limit reached, stale</em>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/paris-paraskevas/claude-usage-tracker/main/docs/team.png" alt="Team pool" width="380">
  <img src="https://raw.githubusercontent.com/paris-paraskevas/claude-usage-tracker/main/docs/history.png" alt="History" width="380"><br>
  <em>Team pool and History (sample data)</em>
</p>

## Install

Requires Windows 10/11, **Claude Code installed and signed in**, and the Edge **WebView2**
runtime (preinstalled on Windows 11; otherwise a free
[download](https://developer.microsoft.com/microsoft-edge/webview2/)). Then pick one:

### Option A — Installer (no Python needed)

Download **`ClaudeUsageTracker-Setup.exe`** from the
[latest release](https://github.com/paris-paraskevas/claude-usage-tracker/releases/latest)
and run it; tick the shortcuts you want (Desktop / Start Menu / start at sign-in).
Release builds are signed through SignPath when signing is configured for the release; if
Windows SmartScreen still warns, choose **More info → Run anyway**.

### Option B — pipx

```bash
pipx install claude-usage-tracker
claude-usage-tracker --install      # Desktop / Start Menu / Startup shortcuts
```

### Option C — from source

```bash
git clone https://github.com/paris-paraskevas/claude-usage-tracker.git
cd claude-usage-tracker
python install.py
```

**Updating:** the app checks daily (installer builds against GitHub releases, pip/pipx
installs against PyPI) and offers a one-click **Update** that upgrades in place and restarts.

### Near-live numbers (recommended)

Open **Settings › Data source › Install** once. It adds the tracker as Claude Code's
`statusLine` command, which receives your 5-hour and weekly usage after every Claude reply
([Claude Code docs](https://code.claude.com/docs/en/statusline)), and the session hooks
that detect *waiting* and *needs approval*. If you already have a status line, the tracker
tells you and leaves yours alone. Without it, usage comes from the API every 5 minutes.

## Usage

Launch **Claude Usage Tracker** from the Start Menu. Tray menu: **Open dashboard**,
**Show overlay**, **Mute alerts ›**, **Refresh now**, **Settings**, **Quit**.

```bash
claude-usage-tracker --once          # print usage once (add --debug for raw API JSON)
claude-usage-tracker --headless      # engine + local API only, no tray or windows
claude-usage-tracker --selftest      # build state from fixtures (no network) and exit
claude-usage-tracker --install       # shortcuts and autostart
claude-usage-tracker --uninstall     # remove shortcuts, our Claude Code hooks and status line
```

Settings live in the dashboard (gear icon) and are saved to `config.json` in the app's data
folder (`%LOCALAPPDATA%\ClaudeUsageTracker` when installed).

## How it works

- **Usage numbers:** Claude Code's status line input (`rate_limits`, per-session context
  window) when installed; otherwise `GET https://api.anthropic.com/api/oauth/usage` with the
  OAuth token Claude Code stores in `~/.claude/.credentials.json`. That call also supplies
  the per-model weekly scopes and extra-usage spend. The token is only read, never written.
- **Sessions, context and history:** your local Claude Code logs under `~/.claude/projects`
  (token counts, model and timestamps only — never message content), read incrementally.
- **Forecast and decision:** see [docs/design/V2-SPEC.md](docs/design/V2-SPEC.md).
- **The UI** is plain HTML/CSS/JS in `ctu/ui`, served by the engine on `127.0.0.1` (every
  route rejects other origins) and updated live over Server-Sent Events.

## Privacy and network

Local-first, no telemetry. The app contacts only:

| Host | Why | When |
|---|---|---|
| `api.anthropic.com` (`/api/oauth/usage`, `/api/oauth/profile`) | your usage, your account's org | always (your own token) |
| `status.anthropic.com` | service status | always |
| `api.github.com` / `pypi.org` | update check | daily, can be turned off |
| the relay (Cloudflare Worker) | end-to-end-encrypted snapshot for your phone | only if you pair a phone |
| Supabase (EU) | usage numbers shared with your email domain's pool | only if you sign in to Team pool |

## Phone (optional)

View your usage and get alerts on **Android**, end-to-end encrypted: the desktop sends an
encrypted snapshot through a zero-knowledge relay, and pushes use data-only FCM messages
decrypted on the phone. Pair from **Settings › Phone**. Includes a home-screen widget
(2×1, 2×2, 4×2). See [docs/REMOTE.md](docs/REMOTE.md), [`relay/`](relay/), [`android/`](android/).

## Team pool (optional) — Claude Team plans

Share a pool of Claude accounts across your company: sign in to **Team pool** with your work
email (a one-time code; your team is your email domain). See every pooled account's 5-hour
and weekly load, who has headroom right now, and a per-account monthly extra-usage ledger
with CSV export. You consent at sign-in that **usage numbers only** are shared with domain
teammates — never sessions, projects or conversation content. Admins can mint a token for
the claude.ai connector. See [docs/TEAM.md](docs/TEAM.md) and [`mcp/`](mcp/).

## Layout

```
claude_usage_tracker.py   entry point (shim)
ctu/                      the engine: usage, statusline, forecast, alerts, sessions, alltime,
                          team (Supabase pool), remote (relay), server, tray, windows, cli
ctu/ui/                   the desktop UI (dashboard, overlay, flyout) — no build step
relay/                    Cloudflare Worker: phone relay + claude.ai connector
mcp/                      local MCP server for Claude Code / Claude Desktop
android/                  companion app + home-screen widget
supabase/                 team pool schema
tests/                    pytest (engine) and tests/ui (node --test)
```

## Development

```bash
python -m pytest -q                        # engine
cd tests/ui && npm test                    # UI formatting + runway chart
python tests/ui/mock_server.py --variant will_hit   # the UI on fixtures, no engine needed
```

## Code signing

Windows builds can be signed with free code signing provided by
[SignPath.io](https://signpath.io), with a certificate from the
[SignPath Foundation](https://signpath.org).
