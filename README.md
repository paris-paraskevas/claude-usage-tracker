<p align="center"><img src="https://raw.githubusercontent.com/paris-paraskevas/claude-usage-tracker/main/docs/icon.png" width="84" alt="Claude Usage Tracker"></p>

# Claude Usage Tracker

A small Windows desktop widget that tracks your Claude plan limits — the **5-hour**
and **weekly** usage windows, the exact numbers the `/usage` command shows — with a
live always-on-top widget, a glass dashboard, a tray icon, and a toast notification
every time usage crosses a 20% mark.

It reads the OAuth token Claude Code already stores on your machine and calls the
same endpoint `/usage` uses. **Read-only** — it never modifies your credentials and
talks to nothing but that one Anthropic endpoint.

<p align="center">
  <img src="https://raw.githubusercontent.com/paris-paraskevas/claude-usage-tracker/main/docs/widget.png" alt="Always-on-top mini widget" width="420"><br>
  <em>The always-on-top mini widget (sample data)</em>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/paris-paraskevas/claude-usage-tracker/main/docs/dashboard.png" alt="Dashboard" width="600"><br>
  <em>The full dashboard — gauges, live reset countdowns, burn-rate, history, overage credits, and Open Sessions (sample data)</em>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/paris-paraskevas/claude-usage-tracker/main/docs/sessions.png" alt="Open Sessions panel" width="620"><br>
  <em>Open Sessions — per-project context-window fill % (toggle to Tokens), with active indicators (sample data)</em>
</p>

## Features

- **Always-on-top overlays** — a frameless, draggable, **resizable** mini **widget** (5h,
  weekly, **context %**) with Refresh / Check-for-updates buttons and a **dropdown** to pick
  which session's context to track; **and** a **minimal HUD bar** — a translucent, one-line,
  FPS-counter-style strip (`dir  Ctx: 45%  5h: 11%  7d: 3%`) whose controls appear on hover.
  Toggle either from the tray; they can be shown together. Drag a corner to resize; size is remembered.
- **Full dashboard** — animated ring gauges, live reset countdowns, a **burn-rate /
  time-to-limit projection** ("≈7%/h · hits 100% in ~1h 10m"), a usage history
  sparkline, overage credits, and per-model (Opus/Sonnet) scoped weekly limits.
- **Open Sessions** — per-project **context-window fill %** and last-5h token usage,
  read from your local Claude Code logs (token counts only, never content), with
  **Context % / Tokens** tabs and active indicators — see which terminal is burning
  your usage.
- **All-time stats** — a second dashboard tab that mines your whole local history:
  an **Overview** (sessions, messages, total tokens, active days, current/longest
  streak, peak hour, favorite model, a contribution **heatmap**, and a "you've burned
  N× more tokens than *War and Peace*" line) and a **Models** view (tokens-over-time
  stacked chart + per-model input/output split), all with a **7d / 30d / All** toggle.
  Folded incrementally from your local logs (each file read once, then only new bytes),
  so it's accurate without rescanning gigabytes — token counts only, never content.
- **Proactive alerts + traffic-light verdict** — toasts when a window is on track to
  run out *before* it resets, when the active context hits 90% (time to `/compact`),
  or when overage credits near the cap; plus a one-glance **green / amber / red**
  verdict in the widget and dashboard, and a daily update check with a **one-click in-app
  Update** that upgrades in place and restarts — the `.exe` runs the signed installer,
  and pip/pipx installs upgrade themselves via the app's own Python (no `pipx` on PATH
  needed, no trip to GitHub). **Refresh** and **Check for updates** are one click away in
  the dashboard, widget, and tray menu too.
- **Live tray icon** — two bars (left = 5h, right = weekly) that fill and change
  colour with usage.
- **20% notifications** — a Windows toast each time the 5h or weekly window crosses
  20 / 40 / 60 / 80 / 100%. The first reading is recorded silently, so you only get
  pinged on *future* crossings, never a burst at startup.
- **One-click sign-in** — when your login expires, a **Sign in to Claude** action (in
  the tray menu and on the dashboard banner) runs `claude auth login` — Claude Code's
  own sign-in — so you can refresh it without opening a terminal. The app never writes
  your credentials itself; it just triggers Claude Code's flow.
- **Auto-start on login**, single-instance, graceful rate-limit (HTTP 429) back-off,
  and automatic pickup of account/token changes (it re-reads your login each poll).

### Colour scale

Bars and gauges share one scale (low → high):

| Range | 0–20% | 20–60% | 60–80% | 80–90% | 90–99% | 100% |
|-------|:-----:|:------:|:------:|:------:|:------:|:----:|
| Colour | green | blue | orange | red | black | yellow |

The 90–99% "black" band gets a red glow/outline so it reads as danger rather than
empty.

<p align="center">
  <img src="https://raw.githubusercontent.com/paris-paraskevas/claude-usage-tracker/main/docs/tray-icons.png" alt="Tray icon at various usage levels" width="560"><br>
  <em>Tray icon at 5/40, 45/72, 85/94, 95/100, and 0/0 percent</em>
</p>

## Install

Requires Windows 10/11, **Claude Code installed and logged in** (so
`~/.claude/.credentials.json` exists), and the Edge **WebView2** runtime
(preinstalled on Windows 11; otherwise a free
[download](https://developer.microsoft.com/microsoft-edge/webview2/)). Then pick one:

### Option A — Installer (easiest, no Python needed)

Download **`ClaudeUsageTracker-Setup.exe`** from the
[latest release](https://github.com/paris-paraskevas/claude-usage-tracker/releases/latest)
and run it. A standard Windows wizard: accept the license, then tick the shortcuts
you want — **Desktop / Start Menu / start at sign-in**.

> **Windows SmartScreen** ("Windows protected your PC") appears because the installer
> isn't code-signed — normal for small open-source apps. Click **More info → Run anyway**.

### Option B — pipx (updatable from the command line)

```bash
pipx install claude-usage-tracker
claude-usage-tracker --install      # interactive: Desktop / Start Menu / Startup
pipx upgrade claude-usage-tracker   # later, to update
```

### Option C — from source

```bash
git clone https://github.com/paris-paraskevas/claude-usage-tracker.git
cd claude-usage-tracker
python install.py
```

After any of these, the mini widget appears top-right and a tray icon by the clock.
**To pin it to the taskbar** (Windows blocks apps from doing this themselves):
right-click the running app's taskbar icon → *Pin to taskbar*.

**Updating:** the app checks daily and shows an **Update to vX** item in the tray. For
the installer build it downloads and runs the new Setup.exe for you (closes, upgrades,
relaunches); pipx installs use `pipx upgrade claude-usage-tracker`.

## Usage

Launch "Claude Usage Tracker" from the Start Menu/Desktop, or run it directly:

```bash
.venv\Scripts\pythonw.exe claude_usage_tracker.py
```

Tray menu: **Show/Hide widget**, **Open dashboard** (native window), **Open in
browser**, **Refresh now**, open config/log, **Quit**. The widget has a `×` to hide
it; drag it anywhere.

CLI:

```bash
.venv\Scripts\python.exe claude_usage_tracker.py --once          # print status once
.venv\Scripts\python.exe claude_usage_tracker.py --once --debug  # + raw API JSON
.venv\Scripts\pythonw.exe claude_usage_tracker.py --widget       # just the widget
.venv\Scripts\pythonw.exe claude_usage_tracker.py --window       # just the dashboard window
.venv\Scripts\python.exe claude_usage_tracker.py --uninstall-autostart
```

## Configuration

Edit `config.json` (created on first run, in the app's data dir), then restart:

| Key | Default | Meaning |
|-----|---------|---------|
| `poll_interval_seconds` | `60` | How often to check usage. |
| `threshold_step` | `20` | Ping every N percent. |
| `windows` | `["five_hour", "seven_day"]` | Which limits to notify on. |
| `notify_at_100` | `true` | Ping when a limit hits 100%. |
| `notify_on_start` | `true` | One summary toast at launch. |
| `dashboard_port` | `8787` | Local dashboard port. |
| `show_widget_on_start` | `true` | Show the mini widget at launch. |
| `widget_width` / `widget_height` | `392` / `150` | Widget size in pixels. |
| `alltime_days` | `30` | Days shown in the All-time daily-usage chart. |

## How it works

`GET https://api.anthropic.com/api/oauth/usage` with the bearer token from
`~/.claude/.credentials.json` (`claudeAiOauth.accessToken`) and the
`anthropic-beta: oauth-2025-04-20` header. The response carries `five_hour`,
`seven_day`, scoped per-model weekly limits, overage `spend`, and reset timestamps —
the same data the CLI's `/usage` renders.

Token refresh is handled by Claude Code itself; if your login expires, the tracker
shows an error state until you run any `claude` command to refresh it. The token is
only ever read — never written, logged, or sent anywhere but that endpoint.

## Privacy

Read-only and local. It reads your Claude login token and session logs from
`~/.claude` and displays the numbers; it sends nothing anywhere except the Anthropic
usage endpoint (using your own token). No telemetry, no analytics, no data collection.

## Code signing

Windows builds are signed with free code signing provided by
[SignPath.io](https://signpath.io), with a free code signing certificate from the
[SignPath Foundation](https://signpath.org).

## Layout

```
claude_usage_tracker.py   the whole app (tray, server, dashboard+widget HTML, poller)
install.py / uninstall.py shortcut setup / teardown
requirements.txt          pystray, Pillow, winotify, pywebview
docs/                     screenshots
```

Runtime files (`config.json`, `state.json`, `history.json`, `*.log`) live next to the
script (or in `%LOCALAPPDATA%\ClaudeUsageTracker` when packaged) and are git-ignored.
