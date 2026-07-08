"""
Claude Usage Tracker — a Windows desktop widget for Claude plan limits.

Shows your 5-hour and weekly usage (the same numbers `/usage` shows) in a
live system-tray icon, fires a toast every time usage crosses a 20% mark, and
serves a small dark-glass dashboard with animated gauges, live reset
countdowns, a burn-rate / time-to-limit projection, usage history, and overage
credits.

Data source: GET https://api.anthropic.com/api/oauth/usage, authenticated with
the OAuth token Claude Code already stores in ~/.claude/.credentials.json.
Read-only — it never writes to your credentials file and talks to nothing but
that one Anthropic endpoint.

    pythonw claude_usage_tracker.py            # tray widget (normal use)
    python  claude_usage_tracker.py --once     # print status once and exit
    python  claude_usage_tracker.py --once --debug
    python  claude_usage_tracker.py --window --port 8787   # dashboard window
    python  claude_usage_tracker.py --install-autostart
    python  claude_usage_tracker.py --uninstall-autostart
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import supabase_pool  # Supabase account-pool client (stdlib + keyring). See docs/SUPABASE-MIGRATION.md.

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

APP_NAME = "Claude Usage Tracker"


__version__ = "0.2.1"


def _data_dir() -> Path:
    """Per-user data dir (config/state/history/log/icon).

    Always %LOCALAPPDATA%\\ClaudeUsageTracker — works the same whether running
    from a source checkout, a pipx install, or frozen, and never writes into
    site-packages or the repo.
    """
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ClaudeUsageTracker"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return base


APP_DIR = _data_dir()
CREDS_PATH = Path(os.path.expanduser("~")) / ".claude" / ".credentials.json"
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "state.json"
HISTORY_PATH = APP_DIR / "history.json"
SNAPSHOT_PATH = APP_DIR / "last_snapshot.json"
ALLTIME_PATH = APP_DIR / "alltime.json"   # persisted lifetime token tallies + per-file read offsets
# Written by the Claude Code statusline (scripts/statusline.py) on every render —
# the app's primary, API-free source of live 5h/weekly usage.
STATUSLINE_SNAPSHOT = Path(os.path.expanduser("~")) / ".claude" / "usage-snapshot.json"
PROJECTS_DIR = Path(os.path.expanduser("~")) / ".claude" / "projects"
LOG_PATH = APP_DIR / "claude_usage_tracker.log"
ICON_PATH = APP_DIR / "app_icon.png"
ICO_PATH = APP_DIR / "app.ico"
REMOTE_PATH = APP_DIR / "remote.json"   # remote-sync identity (accountId/readToken/e2eeKey) — secrets, opt-in
TEAM_PATH = APP_DIR / "team.json"       # team-mode identity (team/member ids + bearer tokens) — secrets, opt-in
PORT_PATH = APP_DIR / "server_port"     # the tray writes its live dashboard port here for the --session-hook
CLAUDE_SETTINGS = Path(os.path.expanduser("~")) / ".claude" / "settings.json"   # where the idle hook installs

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

# Headers mirror what the Claude Code CLI sends for OAuth requests.
BASE_HEADERS = {
    "anthropic-beta": "oauth-2025-04-20",
    "anthropic-version": "2023-06-01",
    "User-Agent": "claude-cli/2.0.0 (external, cli)",
    "Accept": "application/json",
}

DEFAULT_CONFIG = {
    "ui_refresh_seconds": 10,            # how often the loop ticks (UI refresh + gates the relay sync/report below)
    "statusline_stale_seconds": 300,     # treat statusline data older than this as stale
    "api_extras_interval_seconds": 1800, # how often to refresh overage/scoped extras from the API
    "api_fallback_interval_seconds": 300,# min gap between API polls when no statusline data
    "sessions_interval_seconds": 45,     # how often to rescan local session logs
    "sessions_top_n": 8,
    "alltime_interval_seconds": 600,     # how often to fold new session bytes into lifetime totals
    "alltime_top_n": 8,                  # projects shown on the All-time tab
    "alltime_days": 30,                  # span of the daily-usage chart
    "predictive_alerts": True,           # burn-rate / context-full / overage toasts
    "danger_alerts": True,               # loud per-percent warnings in the 95-100% zone
    "update_check": True,                # daily check for a newer version on PyPI
    "threshold_step": 20,            # ping every N percent
    "windows": ["five_hour", "seven_day"],
    "notify_at_100": True,
    "notify_on_start": True,
    "request_timeout_seconds": 20,
    "dashboard_port": 8787,
    "history_cap": 2880,             # ~2 days at 60s
    "open_as_window": True,          # tray default action: native window vs browser
    "show_widget_on_start": True,    # show the always-on-top mini widget at launch
    "widget_width": 392,
    "widget_height": 216,
    "show_bar_on_start": False,      # the minimal one-line HUD bar overlay
    "bar_width": 340,
    "bar_height": 30,
    "bar_fields": ["dir", "ctx", "5h", "7d", "status"],   # which fields the HUD bar shows, in order
    "status_check": True,                       # poll status.anthropic.com
    "status_interval_seconds": 300,
    "status_components": [],                    # component names to watch ([] = overall status)
    "remote_enabled": False,                    # opt-in: relay an E2EE snapshot to your phone
    "remote_relay_url": "https://claude-usage-relay.businessofzeus.workers.dev",  # default hosted relay; override in Settings
    "remote_sync_seconds": 10,                  # how often to push the snapshot to the relay. The snapshot now
                                                # lives in D1 (not KV), whose free tier is 100k writes/day, so
                                                # ~10s (~8.6k/day) is fine. Bounded below by ui_refresh_seconds
                                                # (the loop tick). Raise it to spare phone battery/network.
    "notify_session_waiting": False,            # opt-in: toast/push when a Claude Code session finishes a turn awaiting you
    "remote_transcript": False,                 # opt-in: mirror the active conversation's text to your phone (E2EE)
    "team_report_seconds": 10,                  # how often a team member pushes its usage row to D1 (docs/TEAM.md);
                                                # bounded below by ui_refresh_seconds (the loop tick)
    "team_share_token": True,                   # escrow the short-lived OAuth access token so the relay's
                                                # 23:59 cron can capture the ledger with this machine off
    "team_tz": "Europe/Athens",                 # the team's wall clock for daily/month-end ledger rows
}

# Settings the dashboard/settings UI may change at runtime (allowlist for POST /api/config).
CONFIG_ALLOW = {
    "show_widget_on_start", "show_bar_on_start", "bar_fields",
    "widget_width", "widget_height", "bar_width", "bar_height",
    "open_as_window", "predictive_alerts", "update_check", "danger_alerts",
    "status_check", "status_components", "remote_enabled", "remote_relay_url",
    "notify_session_waiting", "remote_transcript",
    "team_report_seconds", "team_share_token", "team_tz",
}

LABELS = {
    "five_hour": "5h",
    "seven_day": "Weekly",
    "seven_day_opus": "Weekly · Opus",
    "seven_day_sonnet": "Weekly · Sonnet",
}

WINDOW_LEN = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}

_instance_guard = None   # holds the single-instance socket for the process lifetime
STORE_LOCK = threading.Lock()
STORE: dict = {"snapshot": {"ok": False, "error": "starting", "windows": []}}
CONFIG_LOCK = threading.Lock()   # serialize config read-modify-write (tray is the only writer)
CONTROL: dict = {}   # {"refresh","check_update","update","set_config","toggle_overlay","remote_action"}
                     # — wired by the running TrayApp so the dashboard/widget drive it over HTTP
REMOTE_PUSH = None   # optional callable(title, msg) set by TrayApp to mirror toasts to the phone


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    line = f"{_now_local():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
            tail = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
            LOG_PATH.write_text("\n".join(tail) + "\n", encoding="utf-8")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    try:
        if sys.stdout and sys.stdout.isatty():
            print(line)
    except Exception:
        pass


def _now_local() -> datetime:
    return datetime.now().astimezone()


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        else:
            save_json(CONFIG_PATH, DEFAULT_CONFIG)   # atomic; safe under multi-process startup
    except Exception as exc:
        log(f"config load failed, using defaults: {exc}")
    return cfg


def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"read {path.name} failed: {exc}")
    return default


def save_json(path: Path, data) -> None:
    try:
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        log(f"write {path.name} failed: {exc}")


# ---------------------------------------------------------------------------
# OS / process helpers (single-instance guard, child-process cleanup)
# ---------------------------------------------------------------------------

def bind_guard(port: int):
    """Bind a localhost port as a single-instance lock; raises OSError if taken."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    except OSError:
        pass
    s.bind(("127.0.0.1", port))
    return s


def make_kill_on_close_job():
    """Win32 Job Object with KILL_ON_JOB_CLOSE: child processes assigned to it
    are killed by the OS when this process exits — even on crash/Task-Manager
    kill — so the widget/window children can never orphan."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [("a", ctypes.c_uint64), ("b", ctypes.c_uint64),
                        ("c", ctypes.c_uint64), ("d", ctypes.c_uint64),
                        ("e", ctypes.c_uint64), ("f", ctypes.c_uint64)]

        class EXT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = EXT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        return job
    except Exception as exc:
        log(f"job object unavailable: {exc}")
        return None


def assign_to_job(job, proc) -> None:
    if not job or proc is None:
        return
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.AssignProcessToJobObject(job, int(proc._handle))
    except Exception as exc:
        log(f"assign to job failed: {exc}")


# ---------------------------------------------------------------------------
# Credentials (read-only)
# ---------------------------------------------------------------------------

class TokenState:
    OK = "ok"
    EXPIRED = "expired"
    MISSING = "missing"


def read_oauth() -> dict:
    try:
        data = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
        return data.get("claudeAiOauth") or {}
    except Exception:
        return {}


_account_cache = {"mtime": 0.0, "data": None}


def read_account():
    """Logged-in account (email / display name / org) from ~/.claude.json, cached by
    mtime so the large file is only parsed when the account actually changes."""
    p = Path(os.path.expanduser("~")) / ".claude.json"
    try:
        mt = p.stat().st_mtime
        if mt != _account_cache["mtime"]:
            a = json.loads(p.read_text(encoding="utf-8")).get("oauthAccount") or {}
            _account_cache["data"] = {"email": a.get("emailAddress"),
                                      "name": a.get("displayName"),
                                      "org": a.get("organizationName")}
            _account_cache["mtime"] = mt
        return _account_cache["data"]
    except Exception:
        return _account_cache.get("data")


def read_token() -> tuple[str | None, str]:
    """Return (access_token, state). Never writes to the credentials file."""
    oauth = read_oauth()
    token = oauth.get("accessToken")
    expires_at = oauth.get("expiresAt")  # epoch milliseconds
    if not token:
        return None, TokenState.MISSING
    if isinstance(expires_at, (int, float)) and expires_at / 1000.0 <= time.time() + 60:
        return token, TokenState.EXPIRED
    return token, TokenState.OK


# ---------------------------------------------------------------------------
# API call + parsing
# ---------------------------------------------------------------------------

class FetchResult:
    def __init__(self, ok, windows=None, extra=None, raw=None, error=None,
                 token_state=TokenState.OK):
        self.ok = ok
        self.windows = windows or {}     # key -> {"pct": float, "resets_at": datetime|None}
        self.extra = extra               # overage credits dict or None
        self.raw = raw
        self.error = error
        self.token_state = token_state


def _coerce_pct(value) -> float | None:
    """Clamp a 0-100 utilization value (the endpoint already reports percent)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(100.0, float(value)))


def _parse_reset(value) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            ts = value / 1000.0 if value > 1e12 else float(value)
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
        s = str(value).strip()
        if s.isdigit():
            return _parse_reset(int(s))
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()
    except Exception:
        return None


def parse_windows(data: dict) -> dict:
    out: dict = {}
    if not isinstance(data, dict):
        return out
    candidates = [data]
    for k in ("usage", "rate_limits", "limits", "data"):
        if isinstance(data.get(k), dict):
            candidates.append(data[k])
    for key in LABELS:
        for cand in candidates:
            w = cand.get(key)
            if not isinstance(w, dict):
                continue
            pct = None
            for pk in ("utilization", "percentage", "percent", "used_pct", "pct"):
                if pk in w:
                    pct = _coerce_pct(w[pk])
                    if pct is not None:
                        break
            if pct is None and "used" in w and "limit" in w:
                try:
                    pct = max(0.0, min(100.0, float(w["used"]) / float(w["limit"]) * 100.0))
                except Exception:
                    pct = None
            reset = None
            for rk in ("resets_at", "reset_at", "resets_at_iso", "reset", "next_reset_at"):
                if rk in w:
                    reset = _parse_reset(w[rk])
                    if reset:
                        break
            primary = key in ("five_hour", "seven_day")
            if pct is not None and (primary or reset is not None or pct > 0):
                out[key] = {"pct": pct, "resets_at": reset}
            break
    return out


def parse_extra(data: dict):
    """Overage credits, normalized to currency units. Prefers the `spend` block."""
    def amount(d):
        if isinstance(d, dict) and "amount_minor" in d and "exponent" in d:
            try:
                return d["amount_minor"] / (10 ** d["exponent"])
            except Exception:
                return None
        return None

    spend = data.get("spend") if isinstance(data, dict) else None
    if isinstance(spend, dict) and spend.get("enabled"):
        u = spend.get("used") or {}
        lim = spend.get("limit") or {}
        used, limit = amount(u), amount(lim)
        pct = spend.get("percent")
        if pct is None:
            pct = (used / limit * 100.0) if (used is not None and limit) else 0.0
        cur = u.get("currency") or lim.get("currency") or ""
        return {"enabled": True, "used": used, "limit": limit, "currency": cur, "pct": float(pct or 0.0)}
    eu = data.get("extra_usage") if isinstance(data, dict) else None
    if isinstance(eu, dict) and eu.get("is_enabled"):
        return {"enabled": True, "used": None, "limit": None,
                "currency": eu.get("currency", ""), "pct": _coerce_pct(eu.get("utilization")) or 0.0}
    return None


def fetch_usage(timeout: int) -> FetchResult:
    token, tstate = read_token()
    if tstate == TokenState.MISSING:
        return FetchResult(False, error="No Claude login found", token_state=tstate)
    if tstate == TokenState.EXPIRED:
        return FetchResult(False, error="Login expired", token_state=tstate)

    headers = dict(BASE_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(USAGE_URL, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return FetchResult(False, error=f"Auth rejected (HTTP {exc.code})",
                               token_state=TokenState.EXPIRED)
        if exc.code == 429:
            return FetchResult(False, error="Rate limited by API (429)")
        return FetchResult(False, error=f"HTTP {exc.code}")
    except Exception as exc:
        return FetchResult(False, error=f"{type(exc).__name__}: {exc}")
    return FetchResult(True, windows=parse_windows(data), extra=parse_extra(data), raw=data)


def fetch_profile_org(timeout: int = 10):
    """The logged-in account's organization uuid from /api/oauth/profile, or None."""
    token, tstate = read_token()
    if not token or tstate != TokenState.OK:
        return None
    headers = dict(BASE_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/profile",
                                 headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return ((data or {}).get("organization") or {}).get("uuid")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Severity, formatting, projections
# ---------------------------------------------------------------------------

# Usage color scale (low -> high):
#   0-20 green · 20-60 blue · 60-80 orange · 80-90 red · 90-99 near-black · 100 yellow
USAGE_BANDS = [
    (100, "#c94f38", "max"),
    (90,  "#cf6049", "crit"),
    (80,  "#d4694f", "high"),
    (60,  "#cda24e", "warn"),
    (0,   "#5e9e72", "ok"),
]


def usage_style(pct: float) -> tuple[str, str]:
    """Return (hex_color, level) for a 0-100 utilization value."""
    for threshold, hexv, level in USAGE_BANDS:
        if pct >= threshold:
            return (hexv, level)
    return ("#5e9e72", "ok")


def usage_rgba(pct: float) -> tuple[int, int, int, int]:
    h = usage_style(pct)[0].lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)


def fmt_reset(dt: datetime | None) -> str:
    if dt is None:
        return "?"
    now = _now_local()
    if (dt - now).total_seconds() < 0:
        return "due"
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if (dt - now).total_seconds() < 7 * 86400:
        return dt.strftime("%a %H:%M")
    return dt.strftime("%b %d %H:%M")


def status_line(windows: dict) -> str:
    parts = []
    for key in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"):
        if key in windows:
            w = windows[key]
            parts.append(f"{LABELS[key]} {w['pct']:.0f}%->{fmt_reset(w['resets_at'])}")
    return "  ".join(parts) if parts else "no data"


def project(ts, pcts, cur, reset_ms, now_s, lookback=1800):
    """Return (rate_per_hour, eta_seconds_to_100 | None) from recent samples."""
    xs, ys = [], []
    for t, p in zip(ts, pcts):
        if t >= now_s - lookback:
            xs.append(t)
            ys.append(p)
    if len(xs) < 2 or (xs[-1] - xs[0]) < 300:
        return (None, None)
    slope = (ys[-1] - ys[0]) / (xs[-1] - xs[0])      # percent per second
    rate_h = slope * 3600.0
    if slope <= 0.0015:                              # ~0.1%/min floor => "steady"
        return (rate_h, None)
    eta = (100.0 - cur) / slope
    if eta <= 0:
        return (rate_h, 0)
    if reset_ms and (now_s + eta) > (reset_ms / 1000.0):
        return (rate_h, None)                        # window resets before the limit
    return (rate_h, eta)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def load_history() -> dict:
    h = load_json(HISTORY_PATH, {"t": [], "five_hour": [], "seven_day": []})
    for k in ("t", "five_hour", "seven_day"):
        h.setdefault(k, [])
    return h


def append_history(hist: dict, windows: dict, now_s: float, cap: int) -> None:
    hist["t"].append(int(now_s))
    hist["five_hour"].append(round(windows.get("five_hour", {}).get("pct", 0.0), 2))
    hist["seven_day"].append(round(windows.get("seven_day", {}).get("pct", 0.0), 2))
    for k in ("t", "five_hour", "seven_day"):
        if len(hist[k]) > cap:
            hist[k] = hist[k][-cap:]


# ---------------------------------------------------------------------------
# Snapshot (the JSON the dashboard consumes)
# ---------------------------------------------------------------------------

def _parse_iso(s):
    try:
        s = str(s)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def project_name(cwd: str | None, fallback: str) -> str:
    """Platform-independent basename of a session's cwd (the cwd uses Windows '\\' even when a
    log is scanned elsewhere); `fallback` when there's no usable cwd. The one place every
    session-log reader derives a project name."""
    if cwd:
        base = cwd.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
        if base:
            return base
    return fallback


def _usage_of(o: dict) -> dict | None:
    """The token-usage dict on a session-log record — under `message.usage` or top-level
    `usage` — or None if absent/malformed."""
    msg = o.get("message")
    u = msg.get("usage") if isinstance(msg, dict) else o.get("usage")
    return u if isinstance(u, dict) else None


def _read_appended(path: Path, offset: int):
    """Stream parsed JSON records appended to `path` since byte `offset`, skipping blank or
    unparseable lines. Streams line-by-line (a first-run backfill can be hundreds of MB, so we
    never materialize it). The caller handles stat/rotation and persists the new offset
    (= the file size it read up to)."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def scan_sessions(cache: dict, now: float, window_s: int = 5 * 3600,
                  active_s: int = 600, top_n: int = 8):
    """Per-project Claude Code token usage over the last `window_s`, read
    incrementally from ~/.claude/projects/*/*.jsonl (token counts + timestamps
    only — never message content; nothing leaves the machine).

    `cache` is mutated: {path: {size, mtime, cwd, events:[(ts,tokens)]}}.
    Returns a list of {name, tokens, share, active, idle_s} sorted by tokens.
    """
    cutoff = now - window_s
    try:
        files = list(PROJECTS_DIR.glob("*/*.jsonl"))
    except Exception:
        return []
    seen = set()
    for f in files:
        try:
            st = f.stat()
        except Exception:
            continue
        key = str(f)
        seen.add(key)
        if st.st_mtime < cutoff and key not in cache:
            continue  # untouched within the window and not already tracked
        ent = cache.get(key) or {"size": 0, "mtime": 0, "cwd": None, "events": []}
        if st.st_size < ent["size"]:                       # rotated / truncated
            ent = {"size": 0, "mtime": 0, "cwd": ent.get("cwd"), "events": []}
        if st.st_size > ent["size"]:
            try:
                for o in _read_appended(f, ent["size"]):
                    if ent["cwd"] is None and isinstance(o.get("cwd"), str):
                        ent["cwd"] = o["cwd"]
                    u = _usage_of(o)
                    if not u:
                        continue
                    inp = int(u.get("input_tokens", 0) or 0)
                    out = int(u.get("output_tokens", 0) or 0)
                    cc = int(u.get("cache_creation_input_tokens", 0) or 0)
                    cr = int(u.get("cache_read_input_tokens", 0) or 0)
                    ts = _parse_iso(o.get("timestamp")) or st.st_mtime
                    # 5h burn excludes cheap cache-reads; context fill is the full prompt.
                    tok = inp + out + cc
                    if tok > 0:
                        ent["events"].append((ts, tok))
                    ctx = inp + cc + cr
                    if ctx > 0:
                        ent["ctx"], ent["ctx_ts"] = ctx, ts
                        ent["max_ctx"] = max(ent.get("max_ctx", 0), ctx)
                ent["size"] = st.st_size
            except Exception:
                pass
        ent["mtime"] = st.st_mtime
        ent["events"] = [(t, k) for (t, k) in ent["events"] if t >= cutoff]
        cache[key] = ent
    for k in list(cache.keys()):                            # forget deleted files
        if k not in seen:
            del cache[k]

    # Aggregate per project (a folder may have several session files / terminals).
    agg = {}
    for key, ent in cache.items():
        toks = sum(k for (_, k) in ent["events"])
        if toks <= 0:
            continue
        name = project_name(ent.get("cwd"), Path(key).parent.name)
        a = agg.setdefault(name, {"name": name, "tokens": 0, "last": 0,
                                  "ctx": 0, "ctx_ts": 0, "max_ctx": 0})
        a["tokens"] += toks
        a["last"] = max(a["last"], ent.get("mtime", 0))
        if ent.get("ctx_ts", 0) >= a["ctx_ts"]:        # newest session drives the context fill
            a["ctx"], a["ctx_ts"] = ent.get("ctx", 0), ent.get("ctx_ts", 0)
        a["max_ctx"] = max(a["max_ctx"], ent.get("max_ctx", 0))
    rows = list(agg.values())
    total = sum(r["tokens"] for r in rows) or 1
    for r in rows:
        r["share"] = round(r["tokens"] / total * 100, 1)
        r["active"] = (now - r["last"]) <= active_s
        r["idle_s"] = int(now - r["last"])
        # A session that ever exceeded 200k must be on the 1M window; else assume 200k.
        window = 1_000_000 if r["max_ctx"] > 200_000 else 200_000
        r["context_pct"] = round(min(100.0, r["ctx"] / window * 100.0), 1) if r["ctx"] else 0.0
        r["context_tokens"] = r["ctx"]
        for k in ("ctx", "ctx_ts", "max_ctx"):
            r.pop(k, None)
    rows.sort(key=lambda r: r["tokens"], reverse=True)
    return rows[:top_n]


# ---------------------------------------------------------------------------
# All-time stats (lifetime usage mined from every local session log)
# ---------------------------------------------------------------------------

ALLTIME_VERSION = 2

MODEL_NAMES = {
    "claude-opus-4-8": "Opus 4.8", "claude-opus-4-7": "Opus 4.7",
    "claude-opus-4-6": "Opus 4.6", "claude-opus-4-5": "Opus 4.5",
    "claude-sonnet-4-6": "Sonnet 4.6", "claude-sonnet-4-5": "Sonnet 4.5",
    "claude-haiku-4-5": "Haiku 4.5", "claude-fable-5": "Fable 5",
}

# "you've used Nx more tokens than <work>" — rough token estimates (words x 1.3).
COMPARISONS = [
    ("the U.S. Constitution", 10_000),
    ("The Little Prince", 22_000),
    ("The Great Gatsby", 63_000),
    ("1984", 120_000),
    ("Pride and Prejudice", 165_000),
    ("Dune", 244_000),
    ("Moby-Dick", 268_000),
    ("War and Peace", 750_000),
    ("the Lord of the Rings trilogy", 760_000),
    ("the Harry Potter series", 1_400_000),
]


def pretty_model(raw) -> str:
    """'claude-opus-4-8' -> 'Opus 4.8'. Falls back gracefully for unknown ids
    (and date-suffixed ones like 'claude-haiku-4-5-20251001' -> 'Haiku 4.5')."""
    s = str(raw or "").strip()
    if s in MODEL_NAMES:
        return MODEL_NAMES[s]
    low = s.lower()
    for fam in ("opus", "sonnet", "haiku", "fable"):
        if fam in low:
            nums = [c for c in low.split(fam, 1)[1].replace("_", "-").split("-")
                    if c.isdigit() and len(c) <= 2]   # version parts, not a date stamp
            return (fam.capitalize() + " " + ".".join(nums[:2])).strip()
    if not s or s.startswith("<"):
        return "Other"
    return s.replace("claude-", "").replace("-", " ").strip().title() or "Other"


def _empty_alltime_cache() -> dict:
    return {"v": ALLTIME_VERSION, "files": {}, "models": {}, "projects": {},
            "days": {}, "day_models": {}, "hours": {}, "sessions": {},
            "first_ts": None, "last_ts": None}


def _acc(d: dict, key: str, inp, out, cw, cr) -> None:
    r = d.get(key)
    if r is None:
        r = d[key] = {"in": 0, "out": 0, "cw": 0, "cr": 0, "msgs": 0}
    r["in"] += inp; r["out"] += out; r["cw"] += cw; r["cr"] += cr; r["msgs"] += 1


def _at_tokens(r) -> int:
    # headline metric: real work = input + output + cache writes; cheap cache reads excluded
    return r["in"] + r["out"] + r["cw"]


def scan_all_time(cache: dict, now: float, top_n: int = 8, days: int = 30) -> dict:
    """Lifetime Claude Code usage from ~/.claude/projects/*/*.jsonl.

    Each session log is read incrementally — only the bytes appended since the
    last scan, tracked per file in `cache["files"]` — so the first call backfills
    the whole history and every later call is cheap. Token counts, models, and
    timestamps only; message content is never read. `cache` is mutated and
    persisted between runs. Returns a display dict for the All-time tab.
    """
    if not isinstance(cache, dict) or cache.get("v") != ALLTIME_VERSION:
        cache.clear()
        cache.update(_empty_alltime_cache())
    files = cache["files"]
    try:
        paths = list(PROJECTS_DIR.glob("*/*.jsonl"))
    except Exception:
        paths = []

    for f in paths:
        try:
            st = f.stat()
        except Exception:
            continue
        key = str(f)
        consumed = files.get(key, 0)
        if st.st_size == consumed:
            continue                          # nothing new since last scan
        if st.st_size < consumed:             # rotated / truncated -> re-read from the top
            consumed = 0
        cwd = None
        try:
            for o in _read_appended(f, consumed):
                if cwd is None and isinstance(o.get("cwd"), str):
                    cwd = o["cwd"]
                u = _usage_of(o)
                if not u:
                    continue
                inp = int(u.get("input_tokens", 0) or 0)
                out = int(u.get("output_tokens", 0) or 0)
                cw = int(u.get("cache_creation_input_tokens", 0) or 0)
                cr = int(u.get("cache_read_input_tokens", 0) or 0)
                if inp == out == cw == cr == 0:
                    continue
                msg = o.get("message")
                model = pretty_model(msg.get("model") if isinstance(msg, dict) else None)
                name = project_name(cwd, Path(key).parent.name)
                _acc(cache["models"], model, inp, out, cw, cr)
                _acc(cache["projects"], name, inp, out, cw, cr)
                ts = _parse_iso(o.get("timestamp"))
                if ts:
                    dt = datetime.fromtimestamp(ts)
                    ds = dt.strftime("%Y-%m-%d")
                    _acc(cache["days"], ds, inp, out, cw, cr)
                    dm = cache["day_models"].setdefault(ds, {})
                    dr = dm.get(model)
                    if dr is None:
                        dr = dm[model] = {"in": 0, "out": 0, "cw": 0}
                    dr["in"] += inp; dr["out"] += out; dr["cw"] += cw
                    hk = str(dt.hour)
                    cache["hours"][hk] = cache["hours"].get(hk, 0) + inp + out + cw
                    sid = o.get("sessionId")
                    if isinstance(sid, str):
                        prev = cache["sessions"].get(sid, 0)
                        if ts > prev:
                            cache["sessions"][sid] = ts
                    cache["first_ts"] = ts if cache["first_ts"] is None else min(cache["first_ts"], ts)
                    cache["last_ts"] = ts if cache["last_ts"] is None else max(cache["last_ts"], ts)
            files[key] = st.st_size
        except Exception:
            log("all-time scan error on a file:\n" + traceback.format_exc())
    live = {str(p) for p in paths}
    for k in list(files.keys()):              # drop offsets for deleted files (totals stay — it's cumulative)
        if k not in live:
            del files[k]

    return _alltime_display(cache, now, top_n)


def _d(ds):
    return datetime.strptime(ds, "%Y-%m-%d").date()


def _comparison(total_tokens, rank):
    """Pick a 'Nx more tokens than <work>' line. `rank` (0=all,1=30d,2=7d) nudges
    the choice so different periods cite different works."""
    cands = sorted([(n, t) for (n, t) in COMPARISONS if total_tokens >= t * 2],
                   key=lambda x: x[1], reverse=True)
    if not cands:
        return None
    name, tok = cands[min(rank, len(cands) - 1)]
    return {"x": round(total_tokens / tok), "name": name}


def _segs(model_map):
    segs = [{"name": m, "tok": r["in"] + r["out"] + r["cw"]} for m, r in model_map.items()]
    segs = [s for s in segs if s["tok"] > 0]
    segs.sort(key=lambda x: x["tok"], reverse=True)
    return segs


def _series(cache, today, ndays):
    """Stacked-bar bins over time: daily for 7d/30d, weekly for all-time."""
    dm = cache["day_models"]
    bins = []
    if ndays is not None:
        for i in range(ndays - 1, -1, -1):
            d = today - timedelta(days=i)
            segs = _segs(dm.get(d.strftime("%Y-%m-%d"), {}))
            bins.append({"label": d.strftime("%b ") + str(d.day),
                         "total": sum(s["tok"] for s in segs), "segs": segs})
        return bins
    first = cache.get("first_ts")
    if not first:
        return []
    start = datetime.fromtimestamp(first).date()
    start = start - timedelta(days=start.weekday())     # align to Monday
    wk = start
    while wk <= today:
        acc = {}
        for j in range(7):
            for m, r in dm.get((wk + timedelta(days=j)).strftime("%Y-%m-%d"), {}).items():
                acc[m] = acc.get(m, 0) + r["in"] + r["out"] + r["cw"]
        segs = [{"name": m, "tok": t} for m, t in acc.items() if t > 0]
        segs.sort(key=lambda x: x["tok"], reverse=True)
        bins.append({"label": wk.strftime("%b ") + str(wk.day), "total": sum(s["tok"] for s in segs), "segs": segs})
        wk += timedelta(days=7)
    return bins[-26:]                                   # keep it readable


def _period(cache, now, today, ndays, rank):
    cutoff = None if ndays is None else (today - timedelta(days=ndays - 1))
    tokens = msgs = active = 0
    for ds, r in cache["days"].items():
        if cutoff and _d(ds) < cutoff:
            continue
        t = _at_tokens(r)
        if t > 0:
            active += 1
        tokens += t
        msgs += r["msgs"]
    model_tot = {}
    for ds, mm in cache["day_models"].items():
        if cutoff and _d(ds) < cutoff:
            continue
        for m, r in mm.items():
            a = model_tot.setdefault(m, {"in": 0, "out": 0, "cw": 0})
            a["in"] += r["in"]; a["out"] += r["out"]; a["cw"] += r["cw"]
    models = [{"name": m, "in": r["in"], "out": r["out"], "tokens": r["in"] + r["out"] + r["cw"]}
              for m, r in model_tot.items()]
    models = [x for x in models if x["tokens"] > 0]
    g = sum(x["tokens"] for x in models) or 1
    for x in models:
        x["share"] = round(x["tokens"] / g * 100, 1)
    models.sort(key=lambda x: x["tokens"], reverse=True)
    if ndays is None:
        sessions = len(cache["sessions"])
    else:
        cut_ts = now - ndays * 86400
        sessions = sum(1 for ts in cache["sessions"].values() if ts >= cut_ts)
    return {
        "tokens": tokens, "messages": msgs, "sessions": sessions, "active_days": active,
        "fav_model": models[0]["name"] if models else "—",
        "models": models[:8],
        "compare": _comparison(tokens, rank),
        "series": _series(cache, today, ndays),
    }


def _heatmap(cache, today):
    toks = {ds: _at_tokens(r) for ds, r in cache["days"].items()}
    mx = max(toks.values()) if toks else 0
    first = cache.get("first_ts")
    if not first:
        return {"days": [], "max": 0}
    start = datetime.fromtimestamp(first).date()
    start = start - timedelta(days=(start.weekday() + 1) % 7)   # align to a Sunday
    floor = today - timedelta(days=371)                          # keep it a tidy ~1-year block
    floor = floor - timedelta(days=(floor.weekday() + 1) % 7)
    if start < floor:
        start = floor
    out, d = [], start
    while d <= today:
        t = toks.get(d.strftime("%Y-%m-%d"), 0)
        lvl = 0
        if t > 0 and mx > 0:
            frac = t / mx
            lvl = 1 if frac <= .25 else 2 if frac <= .5 else 3 if frac <= .75 else 4
        out.append({"d": d.strftime("%Y-%m-%d"), "tok": t, "lvl": lvl})
        d += timedelta(days=1)
    return {"days": out, "max": mx}


def _streaks(cache, today):
    active = set(ds for ds, r in cache["days"].items() if _at_tokens(r) > 0)
    first = cache.get("first_ts")
    if not first:
        return (0, 0)
    longest = run = 0
    d = datetime.fromtimestamp(first).date()
    while d <= today:
        run = run + 1 if d.strftime("%Y-%m-%d") in active else 0
        longest = max(longest, run)
        d += timedelta(days=1)
    cur, d = 0, today
    if today.strftime("%Y-%m-%d") not in active:        # today may have no activity yet
        d = today - timedelta(days=1)
    while d.strftime("%Y-%m-%d") in active:
        cur += 1
        d -= timedelta(days=1)
    return (cur, longest)


def _peak_hour(cache):
    h = cache.get("hours") or {}
    if not h:
        return None
    hr = int(max(h.items(), key=lambda kv: kv[1])[0])
    return f"{hr % 12 or 12} {'AM' if hr < 12 else 'PM'}"


def _alltime_display(cache: dict, now: float, top_n: int = 8) -> dict:
    today = datetime.fromtimestamp(now).date()
    total = {"in": 0, "out": 0, "cw": 0, "cr": 0, "msgs": 0}
    for r in cache["models"].values():
        for k in total:
            total[k] += r[k]
    projects = [{"name": n, "tokens": _at_tokens(r)} for n, r in cache["projects"].items() if _at_tokens(r) > 0]
    projects.sort(key=lambda x: x["tokens"], reverse=True)
    pg = sum(p["tokens"] for p in projects) or 1
    for p in projects:
        p["share"] = round(p["tokens"] / pg * 100, 1)
    cur, longest = _streaks(cache, today)
    return {
        "ready": cache.get("first_ts") is not None,
        "total": total,
        "first_ts": cache.get("first_ts"),
        "last_ts": cache.get("last_ts"),
        "streak_current": cur,
        "streak_longest": longest,
        "peak_hour": _peak_hour(cache),
        "heatmap": _heatmap(cache, today),
        "projects": projects[:top_n],
        "project_count": len(projects),
        "periods": {
            "7": _period(cache, now, today, 7, 2),
            "30": _period(cache, now, today, 30, 1),
            "all": _period(cache, now, today, None, 0),
        },
    }


def read_statusline_snapshot():
    """Read live 5h/weekly usage written by the Claude Code statusline (no API call).

    Returns {"windows": {...}, "context": {...}, "ts": float} or None.
    """
    d = load_json(STATUSLINE_SNAPSHOT, None)
    if not isinstance(d, dict):
        return None
    ts = d.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    windows = {}
    for key in ("five_hour", "seven_day"):
        w = d.get(key) or {}
        pct = w.get("used_percentage")
        if not isinstance(pct, (int, float)) or isinstance(pct, bool):
            continue
        ra = w.get("resets_at")
        dt = None
        if isinstance(ra, (int, float)):
            try:
                dt = datetime.fromtimestamp(ra, tz=timezone.utc).astimezone()
            except Exception:
                dt = None
        windows[key] = {"pct": max(0.0, min(100.0, float(pct))), "resets_at": dt}
    if not windows:
        return None
    return {"windows": windows, "context": d.get("context_window"),
            "cwd": d.get("cwd"), "model": d.get("model"), "ts": float(ts)}


def fmt_secs(s) -> str:
    s = max(0, int(s or 0))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


VERDICTS = {
    "ok": ("#5e9e72", "All clear"),
    "caution": ("#cda24e", "Ease up"),
    "stop": ("#d4694f", "Near limit"),
    "over": ("#c94f38", "At limit"),
}
_VERDICT_ORDER = ("ok", "caution", "stop", "over")


def compute_verdict(windows) -> dict:
    """One traffic-light status from the 5h + weekly windows (pct + burn-rate ETA)."""
    level = "ok"
    for w in windows:
        if w.get("key") not in ("five_hour", "seven_day"):
            continue
        pct, eta = w.get("pct", 0), w.get("eta_seconds")
        if pct >= 100:
            l = "over"
        elif pct >= 95:
            l = "stop"
        elif pct >= 80 or eta is not None:
            l = "caution"
        else:
            l = "ok"
        if _VERDICT_ORDER.index(l) > _VERDICT_ORDER.index(level):
            level = l
    color, text = VERDICTS[level]
    return {"level": level, "color": color, "text": text}


def _vtuple(v):
    try:
        return tuple(int(x) for x in str(v).split(".")[:3])
    except Exception:
        return (0,)


RELEASES_URL = "https://github.com/paris-paraskevas/claude-usage-tracker/releases/latest"


def check_github_latest():
    """(latest_version, installer_url) from the latest GitHub release, or (None, None).
    One call gives both the version and the Setup.exe URL; safe to call ~daily."""
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/paris-paraskevas/claude-usage-tracker/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "claude-usage-tracker"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.load(resp)
        ver = (d.get("tag_name") or "").lstrip("v")
        url = next((a.get("browser_download_url") for a in d.get("assets", [])
                    if str(a.get("name", "")).lower().endswith(".exe")), None)
        return (ver or None), url
    except Exception:
        return None, None


STATUS_URL = "https://status.anthropic.com/api/v2/summary.json"   # -> status.claude.com (Statuspage)


def fetch_status():
    """Anthropic/Claude service status (Statuspage v2). Returns
    {indicator, description, components:[{name,status}], url} or None. Slow-poll only."""
    try:
        req = urllib.request.Request(STATUS_URL, headers={
            "User-Agent": "claude-usage-tracker", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.load(resp)
        st = d.get("status", {}) or {}
        comps = [{"name": c.get("name", ""), "status": c.get("status", "operational")}
                 for c in d.get("components", []) if not c.get("group")]
        return {"indicator": st.get("indicator", "none"),
                "description": st.get("description", ""),
                "components": comps,
                "url": (d.get("page", {}) or {}).get("url", "https://status.anthropic.com")}
    except Exception:
        return None


def _extract_text(content) -> str:
    """Readable text from a Claude message's `content` (string or block list)."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if isinstance(b, str):
            parts.append(b)
        elif isinstance(b, dict):
            t = b.get("type")
            if t == "text" and isinstance(b.get("text"), str):
                parts.append(b["text"])
            elif t == "tool_use":
                parts.append(f"[ran {b.get('name', 'tool')}]")
            # tool_result blocks (role=user) are skipped — not conversation text
    return "\n".join(p for p in parts if p).strip()


def _read_one_transcript(f: Path, max_msgs: int, max_chars: int,
                         tail_bytes: int = 400_000) -> dict | None:
    """One session file -> {name, cwd, messages:[{role,text,ts}]} (or None if it has no
    conversation text). Reads `cwd` from the first lines (it's recorded early) and the
    messages from only the file's tail, so a huge session log stays cheap to mirror."""
    try:
        size = f.stat().st_size
    except Exception:
        return None
    cwd, msgs = None, []
    try:
        with open(f, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(8):                       # cwd is on an early line
                ln = fh.readline()
                if not ln:
                    break
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                if isinstance(o.get("cwd"), str):
                    cwd = o["cwd"]
                    break
            if size > tail_bytes:                    # only mirror the recent tail of big logs
                fh.seek(size - tail_bytes)
                fh.readline()                        # drop the partial first line
            else:
                fh.seek(0)
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                if cwd is None and isinstance(o.get("cwd"), str):
                    cwd = o["cwd"]
                msg = o.get("message")
                if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
                    continue
                text = _extract_text(msg.get("content"))
                if not text:
                    continue
                msgs.append({"role": msg["role"], "text": text[:max_chars],
                             "ts": _parse_iso(o.get("timestamp"))})
    except Exception:
        return None
    if not msgs:
        return None
    name = project_name(cwd, f.parent.name)
    # session_id = the .jsonl filename stem; the phone sends it back to resume THIS conversation.
    return {"name": name, "cwd": cwd, "session_id": f.stem, "messages": msgs[-max_msgs:]}


def read_transcripts(limit: int = 6, max_msgs: int = 30, max_chars: int = 2500) -> list[dict]:
    """Recent conversations (text only), newest first, so the phone can PICK which session
    to view/chat in. This is the ONLY place the app reads message *content*; it runs solely
    when `remote_transcript` is enabled and the payload is end-to-end encrypted before it
    leaves the machine. One entry per project dir (newest file wins); each is
    {name, cwd, active, messages:[{role,text,ts}]}."""
    try:
        files = sorted(PROJECTS_DIR.glob("*/*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return []
    now, out, seen = time.time(), [], set()
    for f in files:
        if len(out) >= max(1, limit):
            break
        t = _read_one_transcript(f, max_msgs, max_chars)
        if not t:
            continue
        key = t.get("cwd") or str(f)                 # collapse multiple terminals in one project
        if key in seen:
            continue
        seen.add(key)
        try:
            t["active"] = (now - f.stat().st_mtime) <= 600
        except Exception:
            t["active"] = False
        out.append(t)
    return out


def build_snapshot(r: FetchResult, hist: dict, cfg: dict) -> dict:
    now_s = time.time()
    oauth = read_oauth()
    snap = {
        "ok": r.ok,
        "error": r.error,
        "token_state": r.token_state,
        "updated_at": int(now_s),
        "subscription": oauth.get("subscriptionType", ""),
        "account": read_account(),
        "windows": [],
        "extra": r.extra,
        "history": {},
    }
    order = ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"]
    for key in order:
        if key not in r.windows:
            continue
        w = r.windows[key]
        reset_ms = int(w["resets_at"].timestamp() * 1000) if w["resets_at"] else None
        color, level = usage_style(w["pct"])
        item = {"key": key, "label": LABELS[key], "pct": round(w["pct"], 1),
                "resets_at": reset_ms, "color": color, "level": level,
                "rate_per_hour": None, "eta_seconds": None}
        if key in ("five_hour", "seven_day"):
            rate, eta = project(hist["t"], hist[key], w["pct"], reset_ms, now_s)
            item["rate_per_hour"] = round(rate, 2) if (rate is not None and rate > 0) else None
            item["eta_seconds"] = None if eta is None else int(eta)
        snap["windows"].append(item)

    # last ~240 points for the sparkline
    n = min(240, len(hist["t"]))
    if n:
        snap["history"] = {
            "t": hist["t"][-n:],
            "five_hour": hist["five_hour"][-n:],
            "seven_day": hist["seven_day"][-n:],
        }
    snap["verdict"] = compute_verdict(snap["windows"])
    return snap


# ---------------------------------------------------------------------------
# Tray icon image
# ---------------------------------------------------------------------------

def make_icon_image(windows: dict, error: bool = False):
    """Two vertical bars: left = 5h, right = weekly. Height = usage, color = band."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if error:
        d.arc([6, 6, 58, 58], 0, 360, fill=(248, 81, 73, 255), width=6)
        d.line([32, 18, 32, 38], fill=(248, 81, 73, 255), width=7)
        d.ellipse([28, 44, 36, 52], fill=(248, 81, 73, 255))
        return img

    top, bot = 7, 57
    for key, x0, x1 in (("five_hour", 11, 29), ("seven_day", 35, 53)):
        pct = max(0.0, min(100.0, windows.get(key, {}).get("pct", 0.0)))
        # Track with a light border so any fill colour (incl. near-black) reads.
        d.rounded_rectangle([x0, top, x1, bot], radius=4,
                            fill=(255, 255, 255, 26), outline=(255, 255, 255, 100), width=2)
        if pct > 0:
            ytop = bot - (bot - top) * pct / 100.0
            y1 = bot - 2
            # Keep at least a 1px sliver for tiny non-zero pct (just after a window
            # reset) and never let the top dip below the bottom — otherwise Pillow
            # raises "y1 must be greater than or equal to y0" and the icon stops updating.
            y0 = min(max(top + 2, ytop), y1 - 1)
            box = [x0 + 2, y0, x1 - 2, y1]
            if y1 - y0 >= 8:
                d.rounded_rectangle(box, radius=3, fill=usage_rgba(pct))
            else:
                d.rectangle(box, fill=usage_rgba(pct))
    return img


def make_app_icon(size: int = 256):
    """Claude-style coral tile with a cream sunburst — the app/window/toast icon.

    Rendered at 4x and downscaled (LANCZOS) for crisp, anti-aliased edges, with
    a subtle vertical gradient on the tile for depth.
    """
    from PIL import Image, ImageDraw

    ss = 4
    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # Rounded-rect tile filled with a soft coral gradient.
    pad, radius = int(S * 0.045), int(S * 0.225)
    top, bot = (228, 138, 105), (196, 101, 72)
    grad = Image.new("RGB", (1, S))
    for y in range(S):
        t = y / (S - 1)
        grad.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    grad = grad.resize((S, S))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([pad, pad, S - pad, S - pad], radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)

    # 12-point sunburst from 6 rotated cream ellipses + a solid centre.
    cx = cy = S / 2.0
    half_w, reach, cream = S * 0.049, S * 0.31, (245, 243, 236, 255)
    burst = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    for k in range(6):
        petal = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(petal).ellipse([cx - half_w, cy - reach, cx + half_w, cy + reach], fill=cream)
        burst = Image.alpha_composite(burst, petal.rotate(k * 30.0, resample=Image.BICUBIC, center=(cx, cy)))
    ImageDraw.Draw(burst).ellipse([cx - S * 0.08, cy - S * 0.08, cx + S * 0.08, cy + S * 0.08], fill=cream)

    return Image.alpha_composite(img, burst).resize((size, size), Image.LANCZOS)


def ensure_app_icon() -> None:
    try:
        if ICON_PATH.exists() and ICO_PATH.exists():
            return
        base = make_app_icon(256)
        base.save(ICON_PATH)
        base.save(ICO_PATH, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    except Exception as exc:
        log(f"icon generation failed: {exc}")


# ---------------------------------------------------------------------------
# Remote sync (optional, opt-in, end-to-end encrypted) — see docs/REMOTE.md
#
# The desktop is the only producer. It encrypts the snapshot with a key the relay
# never sees (libsodium secretbox), PUTs the ciphertext to the relay, and the phone
# fetches + decrypts it. Pairing ships the key to the phone via a QR code shown on
# the (localhost-only) dashboard. All of this no-ops gracefully if `pynacl` isn't
# installed or the feature is disabled, so the core app never depends on it.
# ---------------------------------------------------------------------------

_remote_id_cache = None


def remote_available() -> bool:
    """True if the optional crypto dep (pynacl) is importable."""
    return importlib.util.find_spec("nacl") is not None


def _b64u(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def load_remote_identity(create: bool = False):
    """{account_id, read_token, e2ee_key(b64)} from remote.json. Generates and persists
    it when create=True (and pynacl is available). Returns None otherwise."""
    global _remote_id_cache
    if _remote_id_cache is not None:
        return _remote_id_cache
    data = load_json(REMOTE_PATH, None)
    if isinstance(data, dict) and data.get("account_id") and data.get("read_token") and data.get("e2ee_key"):
        _remote_id_cache = data
        return data
    if not create or not remote_available():
        return None
    import os
    import base64
    ident = {"v": 1, "account_id": _b64u(os.urandom(16)),
             "read_token": _b64u(os.urandom(32)),
             "e2ee_key": base64.b64encode(os.urandom(32)).decode()}
    save_json(REMOTE_PATH, ident)
    _remote_id_cache = ident
    log("remote: generated a new pairing identity")
    return ident


def rotate_remote_identity():
    """Mint a fresh identity (new read_token + e2ee_key). Old relay data becomes
    unreadable and the phone must re-pair."""
    global _remote_id_cache
    _remote_id_cache = None
    try:
        REMOTE_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    return load_remote_identity(create=True)


def unpair_remote() -> None:
    """Forget the local identity. Relayed ciphertext expires on the relay's TTL."""
    global _remote_id_cache
    _remote_id_cache = None
    try:
        REMOTE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def remote_encrypt(obj):
    """Return the wire blob {v, nonce, ct, ts} for `obj`, or None if unavailable."""
    import base64
    from nacl.secret import SecretBox
    from nacl.utils import random as nrandom
    ident = load_remote_identity(create=True)
    if not ident:
        return None
    box = SecretBox(base64.b64decode(ident["e2ee_key"]))
    enc = box.encrypt(json.dumps(obj).encode("utf-8"), nrandom(SecretBox.NONCE_SIZE))
    return {"v": 1, "nonce": base64.b64encode(enc.nonce).decode(),
            "ct": base64.b64encode(enc.ciphertext).decode(), "ts": int(time.time())}


def remote_pair_uri(cfg: dict):
    """`cutpair1:<base64url(json{u,a,t,k})>` — the QR payload the phone scans."""
    import base64
    ident = load_remote_identity(create=True)
    url = (cfg.get("remote_relay_url") or "").rstrip("/")
    if not ident or not url:
        return None
    payload = {"u": url, "a": ident["account_id"], "t": ident["read_token"], "k": ident["e2ee_key"]}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "cutpair1:" + raw


def remote_pair_qr_png(cfg: dict):
    """PNG bytes of the pairing QR, or None."""
    uri = remote_pair_uri(cfg)
    if not uri:
        return None
    try:
        import io
        import qrcode
        buf = io.BytesIO()
        qrcode.make(uri).save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        log(f"remote: pair QR failed: {exc}")
        return None


def _relay_call(method: str, cfg: dict, path: str, body=None, timeout: int = 8):
    """Authenticated relay request. Returns the HTTP status code, or None on error."""
    url = (cfg.get("remote_relay_url") or "").rstrip("/")
    ident = load_remote_identity(create=False)
    if not url or not ident:
        return None
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + ident["read_token"])
    req.add_header("User-Agent", f"claude-usage-tracker/{__version__}")  # avoid Cloudflare bot block (err 1010)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as exc:
        log(f"remote: {method} {path} failed: {exc}")
        return None


def remote_sync_snapshot(snap: dict, cfg: dict) -> bool:
    """Encrypt + PUT the snapshot to the relay. Returns True on 204."""
    ident = load_remote_identity(create=False)
    if not ident:
        return False
    blob = remote_encrypt(snap)
    if not blob:
        return False
    return _relay_call("PUT", cfg, f"/v1/acct/{ident['account_id']}/snapshot", blob) == 204


def remote_push(cfg: dict, title: str, body: str, tag: str = "") -> None:
    """Encrypt + POST a push payload; the relay fans it out via FCM (E2EE)."""
    ident = load_remote_identity(create=False)
    if not ident:
        return
    blob = remote_encrypt({"title": title, "body": body, "tag": tag})
    if blob:
        _relay_call("POST", cfg, f"/v1/acct/{ident['account_id']}/push", blob)


def remote_decrypt(blob: dict):
    """Decrypt a {nonce, ct} blob the phone sent, with our shared e2ee key. Returns obj|None."""
    import base64
    from nacl.secret import SecretBox
    ident = load_remote_identity(create=False)
    if not ident or not isinstance(blob, dict):
        return None
    try:
        box = SecretBox(base64.b64decode(ident["e2ee_key"]))
        plain = box.decrypt(base64.b64decode(blob["ct"]), base64.b64decode(blob["nonce"]))
        return json.loads(plain.decode("utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Team mode (optional, opt-in) — see docs/TEAM.md
#
# An admin-owned relay aggregates a Claude Team plan's members. Members push a
# compact PLAINTEXT row (usage numbers only — never sessions, paths, or content)
# and, if `team_share_token` is on, escrow their short-lived OAuth access token
# so the relay's nightly cron can capture the 23:59 ledger row (daily + the
# month-end freeze) even while this machine is off. The refresh token NEVER
# leaves this machine. Unlike phone sync, team rows are not E2EE: the relay is
# the admin's own Worker and the numbers are exactly what the dashboard shows.
# ---------------------------------------------------------------------------

def _sha256_hex(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def device_month_tokens(cache, month: str) -> int:
    """This machine's Claude Code tokens for a calendar month, from the all-time
    cache's per-day buckets. Same headline metric as the All-time tab (in+out+cw;
    cheap cache reads excluded). Day keys are local dates."""
    if not isinstance(cache, dict):
        return 0
    total = 0
    for ds, r in (cache.get("days") or {}).items():
        if isinstance(ds, str) and ds.startswith(month + "-"):
            try:
                total += _at_tokens(r)
            except Exception:
                pass
    return total


def build_team_report(snap: dict, dev=None, account=None) -> dict:
    """The compact plaintext row a reporter shares with the team: window percents, reset
    times, overage euros, THIS device's month tokens, and which pooled Claude ACCOUNT
    (email + display name + org uuid) the numbers belong to. Nothing else."""
    a = account or {}
    row = {"acct": a.get("acct"), "name": a.get("name"), "org": a.get("org"),
           "fh_pct": None, "sd_pct": None, "fh_resets_at": None, "sd_resets_at": None,
           "extra": None, "ts": int(time.time()),
           "did": (dev or {}).get("did"), "device": (dev or {}).get("device"),
           "tok_month": (dev or {}).get("tok_month")}
    for w in snap.get("windows") or []:
        iso = None
        if w.get("resets_at"):
            try:
                iso = datetime.fromtimestamp(w["resets_at"] / 1000.0, tz=timezone.utc).isoformat()
            except Exception:
                iso = None
        if w.get("key") == "five_hour":
            row["fh_pct"], row["fh_resets_at"] = w.get("pct"), iso
        elif w.get("key") == "seven_day":
            row["sd_pct"], row["sd_resets_at"] = w.get("pct"), iso
    e = snap.get("extra")
    if isinstance(e, dict) and e.get("enabled"):
        row["extra"] = {"enabled": True, "used": e.get("used"), "limit": e.get("limit"),
                        "currency": e.get("currency", ""), "pct": e.get("pct")}
    return row


def team_month_spend(samples: list, baseline=None) -> float:
    """Calendar-month spend from a member's day-by-day `extra.used` samples (sorted by
    date), summing day-over-day increases. `used` accumulates since Anthropic's billing
    anchor (mid-month), so a drop between samples means the cycle reset: the new value is
    fresh accumulation and counts whole. `baseline` is the previous month's last sample;
    without one, the first sample only seeds the diff (its own accumulation is unattributable).
    Error bound: spend between the last pre-reset sample and the reset moment is lost —
    at most one day's burn, at the anchor day."""
    spend = 0.0
    prev = baseline
    for v in samples:
        if not isinstance(v, (int, float)):
            continue
        if prev is None:
            pass                       # unknown history: seed only
        elif v >= prev:
            spend += v - prev
        else:
            spend += v                 # cycle reset in the gap: v is all new spend
        prev = v
    return round(spend, 2)


def _prev_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def _day_account_row(devmap):
    """The account-authoritative row among a member's device rows for one day:
    the cron's `account` row if present, else the newest push. Mirrors the relay's
    pickAccountRow()."""
    if not isinstance(devmap, dict) or not devmap:
        return None
    if isinstance(devmap.get("account"), dict):
        return devmap["account"]
    best = None
    for r in devmap.values():
        if isinstance(r, dict) and (best is None or (r.get("ts") or 0) > (best.get("ts") or 0)):
            best = r
    return best


def _ledger_samples(led: dict, mid: str) -> list:
    """A member's account-level extra.used values for a ledger month, date order."""
    vals = []
    for date in sorted((led.get("days") or {})):
        row = _day_account_row((led["days"][date] or {}).get(mid))
        e = (row or {}).get("extra") or {}
        if isinstance(e.get("used"), (int, float)):
            vals.append(e["used"])
    return vals


def member_month_tokens(led: dict, mid: str) -> int:
    """Tokens a member burnt this month across devices: per device take the LAST
    cumulative tok_month value seen in the month, then sum devices. The cron's
    `account` rows carry no tokens and are skipped."""
    last = {}
    for date in sorted((led.get("days") or {})):
        for did, row in ((led["days"][date] or {}).get(mid) or {}).items():
            if did == "account" or not isinstance(row, dict):
                continue
            if isinstance(row.get("tok_month"), (int, float)):
                last[did] = int(row["tok_month"])
    return sum(last.values())


def team_overview_merge(ov: dict, led, prev_led=None) -> dict:
    """Attach month spend/tokens per ACCOUNT and the KPI aggregates to a relay pool
    overview. Pure — the dashboard JS only formats what this returns."""
    out = dict(ov)
    accounts = [dict(a0) for a0 in (ov.get("accounts") or [])]
    near = []
    org_spend = 0.0
    spend = team_ledger_computed(led, prev_led) if led else {}
    for a0 in accounts:
        a0["month_spend"] = spend.get(a0.get("acct"))
        a0["month_tokens"] = member_month_tokens(led, a0.get("acct")) if led else 0
        if isinstance(a0["month_spend"], (int, float)):
            org_spend += a0["month_spend"]
        row = a0.get("account") or {}
        for key, label in (("fh_pct", "5h"), ("sd_pct", "weekly")):
            p = row.get(key)
            if isinstance(p, (int, float)) and p >= 80:
                near.append({"name": a0.get("name"), "window": label, "pct": p})
    near.sort(key=lambda x: -x["pct"])
    out["accounts"] = accounts
    out["kpis"] = {"org_spend": round(org_spend, 2), "account_count": len(accounts), "near": near}
    return out


def supabase_team_overview():
    """Build the merged pool overview from Supabase (read_overview + this/prev-month ledger),
    reusing the existing merge fns. RLS scopes every read to the caller's team, so any signed-in
    member sees their team's pool (no admin gate). Returns the merged dict or None."""
    if not supabase_pool.signed_in():
        return None
    tz = load_config().get("team_tz", "Europe/Athens")
    today = _now_local()
    tstr = today.strftime("%Y-%m-%d")
    ystr = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    ov = supabase_pool.read_overview(tz, tstr, ystr)
    month = tstr[:7]
    led = supabase_pool.read_ledger(month)
    prev = supabase_pool.read_ledger(_prev_month(month))
    return team_overview_merge(ov, led, prev)


def team_overview_compact(merged):
    """Strip a merged pool overview to the small projection the phone renders — org totals +
    per-account name / window percents / month €. Keeps the E2EE snapshot small. Output keeps the
    `members`/`member_count` keys for the phone's existing contract; each entry is a pooled account."""
    if not isinstance(merged, dict):
        return None
    k = merged.get("kpis") or {}
    members = []
    for a in merged.get("accounts") or []:
        row = a.get("account") or {}
        members.append({
            "name": a.get("name"),
            "fh_pct": row.get("fh_pct"),
            "sd_pct": row.get("sd_pct"),
            "month_spend": a.get("month_spend"),
            "month_tokens": a.get("month_tokens"),
            "currency": (row.get("extra") or {}).get("currency"),
        })
    return {"org_spend": k.get("org_spend"), "member_count": k.get("account_count"),
            "near": k.get("near") or [], "tz": merged.get("tz"), "members": members}


def _ledger_baseline(prev_led, mid: str):
    """The value to diff the month's first sample against: the previous month's
    frozen final if the cron caught it, else its last daily row."""
    if not prev_led:
        return None
    f = (((prev_led.get("finals") or {}).get(mid) or {}).get("extra")) or {}
    if isinstance(f.get("used"), (int, float)):
        return f["used"]
    prior = _ledger_samples(prev_led, mid)
    return prior[-1] if prior else None


def team_ledger_computed(led: dict, prev_led=None) -> dict:
    """Per-member calendar-month spend for a relay ledger response."""
    mids = set(led.get("accounts") or {})
    for d in (led.get("days") or {}).values():
        mids.update(d)
    return {mid: team_month_spend(_ledger_samples(led, mid), _ledger_baseline(prev_led, mid))
            for mid in mids}


class TeamSync:
    """Opt-in account-pool push for the poll loop: a throttled ~10s upsert of the current
    Claude account's usage row to Supabase, as the signed-in user (RLS-scoped). Enabled ==
    a valid Supabase session exists. Token escrow is gone (the Supabase model stores no
    Claude tokens); month-end finals are captured by pg_cron. See docs/SUPABASE-MIGRATION.md."""

    def __init__(self):
        self._last_report = 0.0
        self._acct_email = None         # currently-pooled Claude account; a change = login switch
        self._acct_org = None           # its org uuid (cached; re-fetched only on switch)
        self.last_ok: bool | None = None

    @staticmethod
    def enabled(cfg: dict) -> bool:
        return supabase_pool.configured() and supabase_pool.has_session()

    def due(self, now: float, interval: float) -> bool:
        if now - self._last_report >= interval:
            self._last_report = now
            return True
        return False

    def reset_throttle(self) -> None:
        self._last_report = 0.0

    def sync(self, snap: dict, cfg: dict, alltime_cache=None) -> None:
        try:
            if not supabase_pool.signed_in():
                return
            acct = read_account() or {}
            email = (acct.get("email") or "").strip().lower()
            if not email:
                return  # no Claude account logged in -- nothing to pool
            if email != self._acct_email:
                # Login switched to a different pooled account: refresh its org uuid, push promptly.
                self._acct_email = email
                self._acct_org = fetch_profile_org()
                self.reset_throttle()
            try:
                host = socket.gethostname()[:32] or "device"
            except Exception:
                host = "device"
            month = _now_local().strftime("%Y-%m")
            dev = {"did": supabase_pool.device_id(), "device": host,
                   "tok_month": device_month_tokens(alltime_cache, month)}
            account = {"acct": email, "name": acct.get("name"), "org": self._acct_org}
            row = build_team_report(snap, dev, account)
            self.last_ok = supabase_pool.push(row, dev, account,
                                              _now_local().strftime("%Y-%m-%d"))
        except Exception:
            self.last_ok = False
            log("team: sync error:\n" + traceback.format_exc())


class RemoteSync:
    """Owns the opt-in phone-relay side effects and their shared state, so the poll loop
    doesn't have to: throttled snapshot sync (plus the optional E2EE conversation mirror) and
    push. Pulled out of TrayApp so the throttle decisions are unit-testable in isolation."""

    def __init__(self):
        self._last_sync = 0.0
        self.last_ok: bool | None = None        # surfaced in the snapshot's remote.last_sync_ok

    @staticmethod
    def enabled(cfg: dict) -> bool:
        return bool(cfg.get("remote_enabled") and cfg.get("remote_relay_url") and remote_available())

    def due(self, now: float, interval: float) -> bool:
        """True (and arms the next window) when a throttled sync is due."""
        if now - self._last_sync >= interval:
            self._last_sync = now
            return True
        return False

    def reset_throttle(self) -> None:
        self._last_sync = 0.0                    # force the next due() to fire (manual "sync now")

    def sync(self, snap: dict, cfg: dict) -> None:
        try:
            if cfg.get("remote_transcript"):     # opt-in: mirror conversations to the phone (E2EE)
                ts = read_transcripts()
                if ts:
                    # copy — never leak content into the local STORE snapshot. `transcripts`
                    # (list, pickable on the phone) + `transcript` (active one) for older builds.
                    snap = {**snap, "transcripts": ts, "transcript": ts[0]}
            self.last_ok = remote_sync_snapshot(snap, cfg)
        except Exception:
            self.last_ok = False
            log("remote: sync error:\n" + traceback.format_exc())

    def push(self, cfg: dict, title: str, msg: str) -> None:
        if not self.enabled(cfg):
            return
        threading.Thread(target=remote_push, args=(cfg, title, msg, title[:40]), daemon=True).start()


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify(title: str, msg: str, duration: str = "short") -> None:
    try:
        from winotify import Notification, audio
        kwargs = {"app_id": APP_NAME, "title": title, "msg": msg, "duration": duration}
        if ICON_PATH.exists():
            kwargs["icon"] = str(ICON_PATH)
        toast = Notification(**kwargs)
        try:
            toast.set_audio(audio.Default, loop=False)
        except Exception:
            pass
        toast.show()
    except Exception as exc:
        log(f"toast failed: {exc}")
    try:
        if REMOTE_PUSH:
            REMOTE_PUSH(title, msg)        # mirror the toast to the paired phone (if enabled)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Threshold ping logic
# ---------------------------------------------------------------------------

def bucket_of(pct: float, step: int) -> int:
    return 100 if pct >= 100 else int(pct // step) * step


def check_thresholds(windows: dict, state: dict, cfg: dict) -> None:
    step = int(cfg.get("threshold_step", 20)) or 20
    for key in cfg.get("windows", ["five_hour", "seven_day"]):
        if key not in windows:
            continue
        w = windows[key]
        pct = w["pct"]
        reset_iso = w["resets_at"].isoformat() if w["resets_at"] else None
        prev = state.get(key, {})
        cur_bucket = bucket_of(pct, step)
        # New window or first sighting -> baseline silently (no catch-up spam).
        if prev.get("bucket") is None or reset_iso != prev.get("resets_at"):
            state[key] = {"bucket": cur_bucket, "resets_at": reset_iso}
            continue
        if cur_bucket > prev["bucket"] and cur_bucket >= step:
            label = LABELS.get(key, key)
            if cur_bucket >= 100 and cfg.get("notify_at_100", True):
                notify(f"Claude {label} limit reached", f"{pct:.0f}% used · resets {fmt_reset(w['resets_at'])}")
            elif cur_bucket < 100:
                notify(f"Claude {label} at {cur_bucket}%", f"{pct:.0f}% used · resets {fmt_reset(w['resets_at'])}")
            state[key] = {"bucket": cur_bucket, "resets_at": reset_iso}
        else:
            state[key]["resets_at"] = reset_iso


def check_danger(windows: dict, state: dict, cfg: dict) -> None:
    """Loud warning at each percent in the danger zone (95-100%), once each per reset
    period, for the 5h and weekly windows. Uses a persisted high-water mark so it never
    re-fires or fires on the way down."""
    if not cfg.get("danger_alerts", True):
        return
    d = state.setdefault("_danger", {})
    for key in ("five_hour", "seven_day"):
        if key not in windows:
            continue
        w = windows[key]
        pct = w["pct"]
        reset_iso = w["resets_at"].isoformat() if w["resets_at"] else None
        rec = d.get(key)
        if not rec or rec.get("resets_at") != reset_iso:
            rec = d[key] = {"resets_at": reset_iso, "max": 0}
        p = min(int(pct), 100)
        if p >= 95 and p > rec["max"]:
            rec["max"] = p
            label = LABELS.get(key, key)
            if p >= 100:
                notify(f"\U0001F6A8 Claude {label} is at 100%",
                       f"Limit reached · resets {fmt_reset(w['resets_at'])}", duration="long")
            else:
                notify(f"⚠️ Claude {label} at {p}%",
                       f"Almost out — {pct:.0f}% used · resets {fmt_reset(w['resets_at'])}", duration="long")


def check_alerts(snap: dict, state: dict, cfg: dict) -> None:
    """Proactive toasts beyond the 20% marks: projected burnout, context-full,
    and overage-credit warnings. Deduped so each fires once per episode."""
    if not cfg.get("predictive_alerts", True):
        return
    al = state.setdefault("_alerts", {})

    # Burn-rate: projected to hit 100% before the window resets (once per reset-period).
    for w in snap.get("windows", []):
        key = w.get("key")
        if key not in ("five_hour", "seven_day"):
            continue
        eta, reset_ms = w.get("eta_seconds"), w.get("resets_at")
        akey = "burn_" + key
        if eta is not None and eta > 0 and w.get("pct", 0) < 95:
            if al.get(akey) != reset_ms:
                notify(f"Claude {LABELS.get(key, key)} on track to run out",
                       f"~{fmt_secs(eta)} to 100% at this pace — before it resets")
                al[akey] = reset_ms

    # Active session's context window almost full (hysteresis: re-arm under 80%).
    ctx = (snap.get("context") or {}).get("used_percentage")
    if isinstance(ctx, (int, float)) and not isinstance(ctx, bool):
        if ctx >= 90 and not al.get("context"):
            cwd = snap.get("cwd") or ""
            name = project_name(cwd, "session")
            notify("Context almost full", f"{name} at {ctx:.0f}% — consider /compact")
            al["context"] = True
        elif ctx < 80:
            al["context"] = False

    # Overage credits almost gone (hysteresis: re-arm under 85%).
    ex = snap.get("extra")
    if isinstance(ex, dict) and ex.get("enabled"):
        p = ex.get("pct", 0)
        if p >= 90 and not al.get("overage"):
            cur = ex.get("currency", "")
            tail = (f" ({cur} {ex['used']:.2f}/{ex['limit']:.2f})"
                    if ex.get("used") is not None and ex.get("limit") is not None else "")
            notify("Overage credits almost gone", f"{p:.0f}% used{tail}")
            al["overage"] = True
        elif p < 85:
            al["overage"] = False


# ---------------------------------------------------------------------------
# Dashboard (served at http://127.0.0.1:<port>/)
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Claude Usage</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect x='5' y='5' width='54' height='54' rx='14' fill='%23D97757'/><circle cx='32' cy='32' r='11' fill='%23F0EEE6'/></svg>">

<style>
  *{box-sizing:border-box;margin:0;padding:0}
  :root{
    /* Warm dark palette — see docs/superpowers/specs/2026-07-06-dashboard-bento-redesign-design.md */
    --bg:#100e0c; --panel:#1a1613; --panel2:#221e1a;
    --card:#1e1a16; --card2:#26211c;
    --line:#332e28; --line2:#2a2621;
    --ink:#f2ede5; --dim:#a99f93; --faint:#786f65;
    --accent:#d97757; --accent2:#7f93b0;
    --ok:#5e9e72; --warn:#cda24e; --high:#d4694f; --hot:#d4694f;
    --mono:ui-monospace,"Cascadia Mono","Cascadia Code","SF Mono","JetBrains Mono",Consolas,"Liberation Mono",monospace;
    --sans:ui-sans-serif,"Segoe UI",system-ui,-apple-system,sans-serif;
  }
  html,body{height:100%}
  body{font:14px/1.55 var(--sans);color:var(--ink);background:var(--bg);
    padding:clamp(16px,3vw,30px) clamp(14px,3vw,28px) 28px;-webkit-font-smoothing:antialiased}
  .wrap{max-width:940px;margin:0 auto}

  /* header */
  header{display:flex;align-items:center;gap:11px;margin-bottom:20px;flex-wrap:wrap}
  .mark{width:30px;height:30px;border-radius:7px;background:var(--accent);color:#1c0f08;
    display:grid;place-items:center;font:700 15px/1 var(--mono)}
  .htxt h1{font-size:16px;font-weight:600;letter-spacing:-.1px}
  .htxt .sub{color:var(--faint);font:11px/1.4 var(--mono);margin-top:1px}
  .badge{font:11px/1 var(--mono);color:var(--dim);border:1px solid var(--line);
    padding:5px 9px;border-radius:6px;text-transform:uppercase;letter-spacing:.3px}
  .vpill{margin-left:auto;font:600 11px/1 var(--sans);letter-spacing:.3px;padding:6px 10px;border-radius:6px;
    border:1px solid transparent;text-transform:uppercase}
  .live{display:flex;align-items:center;gap:6px;color:var(--faint);font:11px/1 var(--mono);text-transform:uppercase;letter-spacing:.5px}
  .live .dot{width:7px;height:7px;border-radius:50%;background:var(--ok)}

  .tabpane[hidden]{display:none}

  /* nav bar (redesign) — 4 destinations, replaces .tabs */
  .navbar{display:inline-flex;gap:3px;margin-bottom:20px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:5px}
  .navbar button{display:inline-flex;align-items:center;gap:7px;background:none;border:0;color:var(--dim);
    font:550 12.5px/1 var(--sans);padding:9px 15px;border-radius:8px;cursor:pointer}
  .navbar button:hover{color:var(--ink)}
  .navbar button.on{background:var(--card2);color:var(--ink)}
  .navbar button .gl{font-size:14px;line-height:1}
  .navbar button.on .gl{color:var(--accent)}
  .navbar button[hidden]{display:none}

  /* bento (redesign Home) */
  .bento{display:grid;grid-template-columns:1.5fr 1fr 1fr;grid-auto-rows:auto;gap:12px}
  .bento .card{padding:16px}
  .bento .hero{grid-column:1;grid-row:1 / span 2;display:flex;flex-direction:column;gap:6px;
    background:linear-gradient(160deg,var(--card2),var(--card))}
  .bento .span2{grid-column:1 / -1}
  .clab{font:10px/1.4 var(--mono);color:var(--faint);text-transform:uppercase;letter-spacing:1.2px;margin-bottom:9px}
  .hnum{font:600 40px/1 var(--mono);letter-spacing:-1.5px;font-variant-numeric:tabular-nums}
  .hnum small{font-size:22px;font-weight:600}
  .cval{font:600 24px/1 var(--mono);letter-spacing:-.5px;font-variant-numeric:tabular-nums}
  .hsub{color:var(--faint);font:11px/1.5 var(--mono);margin-top:2px}
  .hsess{display:flex;align-items:center;gap:10px;padding:5px 0}
  .hsess .nm{width:0;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink)}
  .hsess .pc{color:var(--dim);font:12px/1 var(--mono);font-variant-numeric:tabular-nums}
  @media(max-width:720px){ .bento{grid-template-columns:1fr} .bento .hero{grid-column:1;grid-row:auto} .bento .span2{grid-column:1} }

  /* cards */
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px}
  .panel{padding:18px}
  .ptitle{font:11px/1 var(--mono);color:var(--faint);text-transform:uppercase;letter-spacing:1px;
    margin-bottom:16px;display:flex;justify-content:space-between;align-items:center;gap:10px}
  .legend{display:flex;gap:12px;flex-wrap:wrap}
  .legend span{display:inline-flex;align-items:center;gap:5px;font:11px/1 var(--mono);color:var(--dim);text-transform:none;letter-spacing:0}
  .legend i{width:8px;height:8px;border-radius:2px;display:inline-block}

  .burn .hot{color:var(--high)} .burn .ok{color:var(--ok)}

  /* rows / charts */
  .row{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
  @media(max-width:640px){.row{grid-template-columns:1fr}}
  svg.spark{width:100%;height:clamp(96px,15vw,124px);display:block;overflow:visible}

  /* bars (overage / scoped) */
  .mini{display:flex;align-items:center;gap:10px;margin-top:12px}
  .mini .lbl{width:100px;font:12px/1 var(--mono);color:var(--dim)}
  .bar{flex:1;height:8px;border-radius:3px;background:var(--panel2);overflow:hidden}
  .bar>i{display:block;height:100%;border-radius:3px;transition:width .8s cubic-bezier(.22,1,.36,1)}
  .mini .num{font:12px/1 var(--mono);width:44px;text-align:right;color:var(--dim)}
  .credits .cval{font:600 22px/1.1 var(--mono);margin-bottom:10px;font-variant-numeric:tabular-nums}
  .csub{color:var(--dim);font:11px/1.4 var(--mono)}
  .credits .csub{margin-top:9px}

  /* team */
  .tmkpis{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
  .tmkpi{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:12px 14px}
  .tmkpi b{display:block;font:600 22px/1.2 var(--mono);color:var(--ink);margin-top:4px;font-variant-numeric:tabular-nums}
  .tmkpi.warn b{color:#d4694f}
  .tmrow{padding:11px 0;border-top:1px solid var(--line)}
  .tmrow:first-child{border-top:0;padding-top:2px}
  .tmtop{display:flex;align-items:center;gap:14px}
  .tmname{width:150px;min-width:0}
  .tmname b{font:600 13px/1.3 var(--sans);display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .tmwin{flex:1;min-width:110px}
  .tmwin .wcap{display:flex;justify-content:space-between;font:10px/1.4 var(--mono);color:var(--dim)}
  .tmwin .bar{height:7px;margin-top:3px}
  .tmspend{width:150px;text-align:right;font-variant-numeric:tabular-nums}
  .tmspend b{font:600 15px/1.3 var(--mono);color:var(--ink)}
  .tmdevs{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 0 164px}
  .tmdev{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:3px 8px;font:10px/1.5 var(--mono);color:var(--dim)}
  .tmdev b{color:var(--ink);font-weight:600}
  .tmtable{width:100%;border-collapse:collapse;font:12px/1.5 var(--mono)}
  .tmtable th{text-align:left;color:var(--faint);font-weight:500;padding:6px 10px 6px 0;border-bottom:1px solid var(--line)}
  .tmtable td{padding:7px 10px 7px 0;border-bottom:1px solid var(--line);color:var(--ink);font-variant-numeric:tabular-nums}
  .tmtable td.r,.tmtable th.r{text-align:right}
  .tminput{flex:1;min-width:180px;background:var(--bg);border:1px solid var(--line);color:var(--ink);border-radius:7px;padding:8px 10px;font:12px/1 var(--mono)}
  .tmcode{margin-top:10px;word-break:break-all;user-select:all;background:var(--panel2);border-radius:7px;padding:9px 10px;font:11px/1.5 var(--mono);color:var(--ink)}

  /* sessions */
  .srt{display:flex;align-items:center;gap:10px}
  .stabs{display:inline-flex;gap:2px;background:var(--panel2);border-radius:6px;padding:2px}
  .stab{background:none;border:0;color:var(--dim);font:11px/1 var(--sans);padding:5px 9px;border-radius:5px;cursor:pointer}
  .stab:hover{color:var(--ink)} .stab.on{background:rgba(255,255,255,.07);color:var(--ink)}
  .srow{display:flex;align-items:center;gap:10px;margin:10px 0;font-size:12.5px}
  .sdot{width:7px;height:7px;border-radius:50%;flex:none;background:var(--faint)}
  .sdot.on{background:var(--ok)}
  .sname{width:clamp(84px,24%,190px);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink)}
  .sbar{flex:1;height:7px;border-radius:3px;background:var(--panel2);overflow:hidden}
  .sbar>i{display:block;height:100%;border-radius:3px;transition:width .7s cubic-bezier(.22,1,.36,1)}
  .snum{width:50px;text-align:right;color:var(--dim);font:12px/1 var(--mono)}
  .sempty{color:var(--faint);font-size:12px;padding:8px 0}

  /* all-time */
  .big{font:600 clamp(30px,7vw,44px)/1.02 var(--mono);letter-spacing:-1.5px;font-variant-numeric:tabular-nums}
  .big span{font-family:var(--sans);letter-spacing:0}
  .statgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(116px,1fr));gap:10px;margin-top:18px}
  .stat{background:var(--bg);border:1px solid var(--line2);border-radius:9px;padding:12px 13px}
  .stat .n{font:600 17px/1 var(--mono);font-variant-numeric:tabular-nums}
  .stat .k{color:var(--faint);font:10px/1 var(--mono);text-transform:uppercase;letter-spacing:.6px;margin-top:5px}
  .atrow{display:flex;align-items:center;gap:10px;margin:11px 0;font-size:12.5px}
  .atrow .anm{width:clamp(78px,30%,168px);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink)}
  .atrow .abar{flex:1;height:8px;border-radius:3px;background:var(--panel2);overflow:hidden}
  .atrow .abar>i{display:block;height:100%;border-radius:3px;transition:width .7s cubic-bezier(.22,1,.36,1)}
  .atrow .anum{width:58px;text-align:right;color:var(--dim);font:12px/1 var(--mono)}

  /* footer / error */
  footer{margin-top:18px;text-align:center;color:var(--faint);font:11px/1.7 var(--mono)}
  .err{padding:13px 15px;border:1px solid rgba(212,105,79,.4);background:rgba(212,105,79,.08);
    border-radius:9px;color:#e9b3a6;margin-bottom:16px;font-size:13px;display:none}
  .err.show{display:flex;align-items:center;flex-wrap:wrap;gap:8px}
  .err .signin{background:var(--accent);color:#1c0f08;border:0;font:600 12px/1 var(--sans);
    padding:7px 13px;border-radius:6px;cursor:pointer;margin-left:auto}
  .err .signin:hover{filter:brightness(1.08)} .err .signin:disabled{opacity:.6;cursor:default}
  /* sign-in card (auth-expired) */
  .err.authcard{background:transparent;border:0;padding:0}
  .signincard{display:flex;gap:14px;width:100%;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px}
  .sc-mark{width:34px;height:34px;border-radius:8px;background:var(--accent);color:#1c0f08;display:grid;place-items:center;font:700 16px/1 var(--mono);flex:none}
  .sc-body{flex:1;min-width:0}
  .sc-title{font:600 15px/1.2 var(--sans);color:var(--ink)}
  .sc-sub{color:var(--dim);font:12px/1.5 var(--sans);margin-top:3px}
  .sc-row{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
  .sc-row input{flex:1;min-width:160px;background:var(--bg);border:1px solid var(--line);color:var(--ink);border-radius:7px;padding:9px 11px;font:13px/1 var(--sans)}
  .sc-btn{background:var(--accent);color:#1c0f08;border:0;font:600 13px/1 var(--sans);padding:9px 18px;border-radius:7px;cursor:pointer}
  .sc-btn:hover{filter:brightness(1.08)} .sc-btn:disabled{opacity:.6;cursor:default}
  .sc-status{color:var(--faint);font:11px/1.5 var(--mono);margin-top:10px}
  /* all-time: sub-tabs, model legend, heatmap */
  .atbar{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap}
  #at-overview>*,#at-models-view>*{margin-bottom:14px}
  #at-overview .statgrid{margin-top:0}
  .mrow{display:flex;align-items:center;gap:10px;margin:11px 0;font-size:12.5px}
  .mrow .mdot{width:9px;height:9px;border-radius:2px;flex:none}
  .mrow .mname{width:108px;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .mrow .mio{flex:1;text-align:right;color:var(--dim);font:12px/1 var(--mono)}
  .mrow .mshare{width:54px;text-align:right;font:600 12px/1 var(--mono)}
  .heatmap{display:grid;grid-auto-flow:column;grid-template-rows:repeat(7,1fr);gap:3px;overflow-x:auto;padding-bottom:3px}
  .hm{width:12px;height:12px;border-radius:2px;background:#2a2a31;box-shadow:inset 0 0 0 1px rgba(255,255,255,.035)}
  .hm.l1{background:#5a3526}.hm.l2{background:#8a4a33}.hm.l3{background:#b56043}.hm.l4{background:#d97757}
  /* header controls: refresh + update */
  .ic{background:var(--panel);border:1px solid var(--line);color:var(--dim);width:30px;height:30px;
    border-radius:7px;cursor:pointer;font-size:14px;line-height:1;display:grid;place-items:center}
  .ic:hover{color:var(--ink);border-color:rgba(255,255,255,.18)}
  .ic.spin{animation:spin .8s linear}
  @keyframes spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}
  .updcta{font:600 11px/1 var(--sans);text-transform:uppercase;letter-spacing:.3px;padding:7px 11px;
    border-radius:6px;background:var(--accent);color:#1c0f08;text-decoration:none;white-space:nowrap}
  .linkbtn{background:none;border:0;color:var(--accent);font:inherit;cursor:pointer;padding:0}
  .linkbtn:hover{text-decoration:underline}
  footer a{color:var(--accent)}
  /* settings tab */
  .setrow{display:flex;align-items:center;gap:12px;margin:13px 0;font-size:13px;flex-wrap:wrap}
  .setlbl{width:96px;color:var(--ink);font-weight:600}
  .sbtn{background:var(--panel2);border:1px solid var(--line);color:var(--ink);font:600 12px/1 var(--sans);
    padding:8px 14px;border-radius:7px;cursor:pointer}
  .sbtn:hover{border-color:rgba(255,255,255,.22)} .sbtn:active{transform:translateY(1px)}
  .sbtn.on{background:var(--accent);color:#1c0f08;border-color:transparent}
  .chk{display:inline-flex;align-items:center;gap:6px;color:var(--dim);font-size:12.5px;cursor:pointer}
  .chk input,.fields input{accent-color:var(--accent)}
  .fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px}
  .fields label{display:inline-flex;align-items:center;gap:8px;color:var(--ink);font-size:13px;cursor:pointer;
    background:var(--bg);border:1px solid var(--line2);border-radius:8px;padding:9px 11px}
  .setacts{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .statuspill{display:inline-flex;align-items:center;gap:6px;font:11px/1 var(--mono);color:var(--dim);
    text-decoration:none;padding:5px 9px;border:1px solid var(--line);border-radius:6px;white-space:nowrap;
    max-width:200px;overflow:hidden;text-overflow:ellipsis}
  .statuspill:hover{color:var(--ink);border-color:rgba(255,255,255,.18)}
  .statuspill i{width:7px;height:7px;border-radius:50%;flex:none}
  .strow{display:flex;align-items:center;gap:11px;margin:12px 0;font-size:13px}
  .strow .sdotb{width:9px;height:9px;border-radius:50%;flex:none}
  .strow .sname2{color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .strow .sword{margin-left:auto;font:700 12px/1 var(--mono)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="mark">C</div>
    <div class="htxt">
      <h1>Claude Usage</h1>
      <div class="sub" id="updated">connecting…</div>
    </div>
    <div class="badge" id="tier">—</div>
    <div class="vpill" id="vpill"></div>
    <div class="live"><span class="dot" id="livedot"></span><span id="livetxt">live</span></div>
    <a class="statuspill" id="statuspill" target="_blank" rel="noopener" hidden title="Anthropic status"></a>
    <button class="updcta" id="updcta" hidden></button>
    <button class="ic" id="btn-refresh" title="Re-check usage now">↻</button>
  </header>

  <div class="err" id="err"></div>

  <div class="navbar">
    <button class="on" data-t="home"><span class="gl">⌂</span> Home</button>
    <button data-t="team" id="nav-team"><span class="gl">👥</span> Team</button>
    <button data-t="history"><span class="gl">▤</span> History</button>
    <button data-t="settings"><span class="gl">⚙</span> Settings</button>
  </div>

  <div id="tab-home" class="tabpane">
  <div class="bento">
    <div class="card hero" id="home-hero">
      <div class="clab" id="hero-lab">5-hour limit</div>
      <div class="hnum" id="hero-num">––<small>%</small></div>
      <div class="bar"><i id="hero-bar" style="width:0"></i></div>
      <div class="hsub"><b id="hero-cd">—</b> <span id="hero-abs"></span></div>
      <div class="hsub" id="hero-burn"></div>
      <div style="margin-top:auto;padding-top:16px" class="clab" id="hero2-lab">Weekly</div>
      <div class="hsub"><b id="hero2-val" style="color:var(--ink)">—</b> · <span id="hero2-cd">—</span></div>
      <div class="bar"><i id="hero2-bar" style="width:0"></i></div>
    </div>
    <div class="card">
      <div class="clab">Extra usage · this month</div>
      <div id="extra"></div>
      <div id="scoped"></div>
    </div>
    <div class="card">
      <div class="clab">Context · active</div>
      <div id="home-ctx"><div class="hsub">—</div></div>
    </div>
    <div class="card" id="sesscard">
      <div class="clab" style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        <span>Sessions · last 5h <span class="legend" id="sesssub" style="text-transform:none;letter-spacing:0"></span></span>
        <span class="stabs"><button class="stab on" data-m="context">Ctx</button><button class="stab" data-m="tokens">Tok</button></span></div>
      <div id="sessions"></div>
    </div>
    <div class="card" id="home-team-card">
      <div class="clab">Team</div>
      <div id="home-team"><div class="hsub">—</div></div>
    </div>
    <div class="card span2">
      <div class="clab" style="display:flex;justify-content:space-between">Usage · last 30 days
        <span class="legend"><span><i style="background:#d97757"></i>5h</span><span><i style="background:#7f93b0"></i>weekly</span></span></div>
      <svg class="spark" id="spark" preserveAspectRatio="none"></svg>
    </div>
    <div class="card span2">
      <div class="clab">Anthropic status</div>
      <div id="home-status"><div class="hsub">—</div></div>
    </div>
  </div>
  </div><!-- /tab-home -->

  <div id="tab-history" class="tabpane" hidden>
    <div class="atbar">
      <div class="stabs" id="at-view">
        <button class="stab on" data-v="overview">Overview</button>
        <button class="stab" data-v="models">Models</button>
      </div>
      <div class="stabs" id="at-period">
        <button class="stab" data-p="7">7d</button>
        <button class="stab" data-p="30">30d</button>
        <button class="stab on" data-p="all">All</button>
      </div>
    </div>

    <div id="at-overview">
      <div class="statgrid" id="at-stats"><div class="sempty">calculating from your session history…</div></div>
      <div class="card panel">
        <div class="ptitle"><span>Activity</span><span class="legend">level <i style="background:#5a3526"></i><i style="background:#8a4a33"></i><i style="background:#b56043"></i><i style="background:#d97757"></i></span></div>
        <div id="at-heatmap" class="heatmap"></div>
        <div class="csub" id="at-compare" style="margin-top:13px"></div>
      </div>
      <div class="card panel">
        <div class="ptitle"><span>By project</span><span class="legend" id="at-projmore">all-time</span></div>
        <div id="at-projects"></div>
      </div>
    </div>

    <div id="at-models-view" hidden>
      <div class="card panel">
        <div class="ptitle">Tokens over time</div>
        <svg class="spark" id="at-series" preserveAspectRatio="none"></svg>
      </div>
      <div class="card panel">
        <div class="ptitle">By model</div>
        <div id="at-models"></div>
      </div>
    </div>
  </div>

  <div id="tab-team" class="tabpane" hidden>
    <div class="card panel" id="tm-login">
      <div class="ptitle">Team · Claude account pool</div>
      <div class="csub" style="margin-bottom:14px">Sign in with your work email to join your team's shared account pool — see every pooled
        Claude account's 5-hour / weekly load and monthly extra-usage spend in one place. Only <b>usage numbers</b> are shared,
        never sessions, projects, or conversation content. Your team is your email <b>domain</b>.</div>
      <label class="chk" style="display:block;margin-bottom:12px"><input type="checkbox" id="tm-consent"> I understand my Claude account email, extra-usage € and token counts are stored in a central EU database (not end-to-end encrypted) and are visible to teammates on my email domain.</label>
      <div class="setrow"><span class="setlbl">Email</span>
        <input type="email" id="tm-email" class="tminput" placeholder="you@yourcompany.com" spellcheck="false" autocomplete="email">
        <button class="sbtn" id="tm-sendcode">Send code</button></div>
      <div id="tm-codewrap" hidden>
        <div class="setrow" style="margin-top:10px"><span class="setlbl">Code</span>
          <input type="text" id="tm-otp" class="tminput" placeholder="digit code from your email" spellcheck="false" inputmode="numeric" autocomplete="one-time-code"></div>
        <div class="setrow" style="margin-top:10px"><span class="setlbl">Username</span>
          <input type="text" id="tm-username" class="tminput" placeholder="how teammates see you" spellcheck="false">
          <button class="sbtn" id="tm-signin">Sign in</button></div>
      </div>
      <div class="csub" id="tm-err" style="margin-top:10px"></div>
    </div>

    <div class="card panel" id="tm-me" hidden>
      <div class="ptitle"><span>Signed in</span><span class="legend" id="tm-mstate"></span></div>
      <div class="csub" id="tm-minfo"></div>
      <div class="setacts" style="margin-top:12px"><button class="sbtn" id="tm-logout">Sign out</button></div>
    </div>

    <div id="tm-adminview" hidden>
      <div class="card panel" id="tm-connector" hidden style="margin-bottom:12px">
        <div class="ptitle"><span>claude.ai connector</span></div>
        <div class="setrow"><span class="setlbl">Connector token</span>
          <button class="sbtn" id="tm-minttoken">Mint connector token</button>
          <span class="csub" id="tm-copytoken-msg">admin only — mints a fresh token to paste at the claude.ai connector consent screen</span></div>
      </div>
      <div class="card panel">
        <div class="ptitle"><span>Accounts · live</span>
          <span class="srt"><span class="legend" id="tm-asof"></span><button class="sbtn" id="tm-reload">Refresh</button></span></div>
        <div class="tmkpis">
          <div class="tmkpi"><span class="lbl csub">org spend this month</span><b id="tm-kpi-spend">—</b><div class="csub" id="tm-kpi-spend-sub"></div></div>
          <div class="tmkpi" id="tm-kpi-near-card"><span class="lbl csub">near limits now</span><b id="tm-kpi-near">—</b><div class="csub" id="tm-kpi-near-sub"></div></div>
        </div>
        <div id="tm-members"><div class="csub">loading…</div></div>
      </div>
      <div class="card panel">
        <div class="ptitle"><span>Monthly ledger · extra usage €</span>
          <span class="stabs"><button class="stab" id="tm-prevm" title="previous month">‹</button><span class="stab on" id="tm-month"></span><button class="stab" id="tm-nextm" title="next month">›</button></span></div>
        <div id="tm-ledger"><div class="csub">loading…</div></div>
        <div class="setacts" style="margin-top:12px"><button class="sbtn" id="tm-csv">Export CSV</button></div>
        <div class="csub" style="margin-top:10px">Month spend = day-over-day increases of each member's credit meter (robust to the
          mid-month billing-cycle reset; the first tracked month starts counting at its first sample). <b>frozen</b> = the 23:59
          month-end row is in. Rows marked <b>push</b> came from the member's last report of the day instead of the cron.</div>
      </div>
    </div>
  </div>

  <div id="tab-settings" class="tabpane" hidden>
    <div class="card panel">
      <div class="ptitle">Overlays</div>
      <div class="setrow"><span class="setlbl">Minimal bar</span>
        <button class="sbtn" id="set-toggle-bar">Show</button>
        <label class="chk"><input type="checkbox" id="set-bar-start"> open at startup</label></div>
      <div class="setrow"><span class="setlbl">Widget</span>
        <button class="sbtn" id="set-toggle-widget">Show</button>
        <label class="chk"><input type="checkbox" id="set-widget-start"> open at startup</label></div>
    </div>
    <div class="card panel">
      <div class="ptitle">Alerts</div>
      <div class="setrow"><span class="setlbl">Session waiting</span>
        <label class="chk"><input type="checkbox" id="set-sesswait"> notify me (desktop + phone) every time Claude finishes and is waiting for me</label></div>
      <div class="csub" style="margin-top:6px">Off by default. Installs a Claude Code <b>Stop</b> hook in <code>~/.claude/settings.json</code> (backed up; removable) that fires the moment Claude finishes a turn — so you get pinged on your phone the instant a session needs you.</div>
    </div>
    <div class="card panel">
      <div class="ptitle">Minimal bar — fields shown</div>
      <div id="set-fields" class="fields"></div>
    </div>
    <div class="card panel">
      <div class="ptitle"><span>Anthropic status</span><a class="legend" target="_blank" rel="noopener" href="https://status.anthropic.com">status.anthropic.com ↗</a></div>
      <div class="big" id="status-head" style="font-size:clamp(20px,4vw,28px);margin-bottom:14px">—</div>
      <div id="status-list"></div>
      <div class="ptitle" style="margin-top:18px">Components to watch</div>
      <div id="set-status" class="fields"></div>
      <div class="csub" style="margin-top:8px">None selected = overall status. Shown on the dashboard, widget, and bar.</div>
    </div>
    <div class="card panel">
      <div class="ptitle"><span>Remote (phone) · Android</span><span class="legend" id="rm-state"></span></div>
      <div id="rm-unavail" class="csub" hidden>Remote needs the encryption library (it ships by default now). Update to the latest version and restart — or, if you installed with pip, run <code>pip install "claude-usage-tracker[remote]"</code>.</div>
      <div class="setrow"><span class="setlbl">Enable</span>
        <label class="chk"><input type="checkbox" id="rm-enabled"> relay an end-to-end-encrypted snapshot to your phone</label></div>
      <div class="setrow"><span class="setlbl">Relay URL</span>
        <input type="text" id="rm-url" placeholder="https://your-worker.workers.dev" spellcheck="false"
          style="flex:1;min-width:220px;background:var(--bg);border:1px solid var(--line);color:var(--ink);border-radius:7px;padding:8px 10px;font:12px/1 var(--mono)">
        <button class="sbtn" id="rm-save">Save</button></div>
      <div id="rm-pairwrap" hidden>
        <div class="csub" style="margin:6px 0 10px">Scan with the Android app to pair. The QR contains your encryption key — keep it private.</div>
        <img id="rm-qr" alt="pairing QR" width="200" height="200" style="background:#fff;border-radius:10px;padding:8px;image-rendering:pixelated">
        <div class="setacts" style="margin-top:12px">
          <button class="sbtn" id="rm-sync">Sync now</button>
          <button class="sbtn" id="rm-rotate">Rotate key</button>
          <button class="sbtn" id="rm-unpair">Unpair</button>
        </div>
        <div class="setrow" style="margin-top:12px"><span class="setlbl">Conversation</span>
          <label class="chk"><input type="checkbox" id="rm-transcript"> mirror the active conversation to your phone — sends message <b>text</b> (E2EE)</label></div>
      </div>
      <div class="csub" style="margin-top:8px">Android only (no iOS yet). The relay only stores ciphertext it can't read. See <code>docs/REMOTE.md</code>.</div>
    </div>
    <div class="card panel">
      <div class="ptitle">Actions</div>
      <div class="setacts">
        <button class="sbtn" id="set-refresh">Refresh now</button>
        <button class="sbtn" id="set-check">Check for updates</button>
        <button class="sbtn" id="set-login">Sign in to Claude</button>
      </div>
    </div>
  </div>

  <footer>
    <button class="linkbtn" id="btn-checkupd">Check for updates</button>
    <span id="updres"></span><br>
    Live usage from Claude Code's statusline data · read-only · no endpoint polling
  </footer>
</div>

<script>
const $=id=>document.getElementById(id);
let WIN={};   // key -> {resets_at, color}
let HEROKEY=null, SECKEY=null, HOME_TEAM_TS=0;   // Home hero windows + team-mini fetch throttle
let LASTH=null;   // last history payload, for resize reflow
function esc(s){ return (s||"").replace(/[&<>]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
async function doSignin(email, terminal){
  const b=$("signin"), st=$("signin-status");
  if(typeof email!=="string"){ const inp=$("signin-email"); email=inp?inp.value.trim():""; }
  if(b){ b.disabled=true; b.textContent=terminal?"Opening terminal…":"Opening browser…"; }
  if(st){ st.textContent=terminal?"A terminal will open — finish signing in there.":
    "Your browser is opening claude.ai — finish signing in there. This page updates automatically."; }
  try{ await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({email:email||undefined,terminal:!!terminal})}); }catch(e){}
  // Claude Code captures the token + writes the creds; the next poll picks it up.
  setTimeout(()=>{ if(b){ b.disabled=false; b.textContent="Sign in"; } },5000);
}
const REL="https://github.com/paris-paraskevas/claude-usage-tracker/releases/latest";
async function doRefresh(){
  const b=$("btn-refresh"); if(b){ b.classList.remove("spin"); void b.offsetWidth; b.classList.add("spin"); }
  try{ await fetch("/api/refresh",{method:"POST"}); }catch(e){}
  setTimeout(refresh,500);   // give the poll loop a beat, then pull the fresh snapshot
}
async function doUpdate(){
  const c=$("updcta"); if(c){ c.disabled=true; c.textContent="Updating…"; }
  const r=$("updres"); if(r)r.textContent="updating — the app will restart…";
  try{ await fetch("/api/update",{method:"POST"}); }catch(e){}
}
async function doCheckUpdate(){
  const r=$("updres"); if(r)r.textContent="checking…";
  try{
    const d=await (await fetch("/api/check-update",{method:"POST"})).json();
    if(d.update){ r.innerHTML="v"+d.latest+" available — <button class='linkbtn' onclick='doUpdate()'>update now</button>"; }
    else if(d.latest){ r.textContent="up to date (v"+d.current+")"; }
    else{ r.textContent="couldn't reach GitHub"; }
  }catch(e){ if(r)r.textContent="couldn't reach GitHub"; }
}
function fdur(s){
  if(s==null)return"—";
  s=Math.max(0,Math.floor(s));
  const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60),sec=s%60;
  if(d>0)return d+"d "+h+"h";
  if(h>0)return h+"h "+String(m).padStart(2,"0")+"m";
  if(m>0)return m+"m "+String(sec).padStart(2,"0")+"s";
  return sec+"s";
}
function fabs(ms){
  if(!ms)return"";
  const dt=new Date(ms),now=new Date();
  const t=dt.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
  if(dt.toDateString()===now.toDateString())return"at "+t+" today";
  return"at "+dt.toLocaleDateString([], {weekday:"short"})+" "+t;
}
function tickCountdowns(){
  const now=Date.now();
  for(const k in WIN){
    const w=WIN[k]; const el=$("cd-"+k); if(!el)continue;
    if(!w.resets_at){el.textContent="—";continue;}
    el.textContent="resets in "+fdur((w.resets_at-now)/1000);
  }
  // Home hero + secondary countdowns (redesign)
  [["hero-cd",HEROKEY],["hero2-cd",SECKEY]].forEach(([id,k])=>{
    const el=$(id); if(!el||!k||!WIN[k])return;
    el.textContent=WIN[k].resets_at?("resets in "+fdur((WIN[k].resets_at-now)/1000)):"—";
  });
}
function bcol(p){ return p==null?"var(--faint)":(p>=80?"var(--hot)":(p>=60?"var(--warn)":"var(--ok)")); }
function burnText(w){
  if(!w)return"";
  if(w.eta_seconds!=null&&w.eta_seconds>=0&&w.rate_per_hour>0)
    return "▲ "+w.rate_per_hour.toFixed(1)+"%/h · full in ~"+fdur(w.eta_seconds);
  if(w.rate_per_hour!=null&&w.rate_per_hour>0.1) return "▲ "+w.rate_per_hour.toFixed(1)+"%/h · resets before limit";
  if(w.rate_per_hour!=null) return "steady · no recent burn";
  return "gathering rate…";
}
// Home bento: hero = the hotter of 5h/weekly; the other rides as a secondary bar.
function renderHome(d){
  if(!d)return;
  const wins=d.windows||[];
  const five=wins.find(w=>w.key==="five_hour"), week=wins.find(w=>w.key==="seven_day");
  const both=[five,week].filter(Boolean);
  if(both.length){
    const hero=both.reduce((a,b)=>(b.pct>a.pct?b:a));
    const sec=both.find(w=>w!==hero)||null;
    HEROKEY=hero.key; SECKEY=sec?sec.key:null;
    WIN[hero.key]={resets_at:hero.resets_at,color:hero.color};
    if(sec)WIN[sec.key]={resets_at:sec.resets_at,color:sec.color};
    $("hero-lab").textContent=hero.label+" limit";
    $("hero-num").innerHTML=Math.round(hero.pct)+"<small>%</small>";
    $("hero-num").style.color=hero.color;
    $("hero-bar").style.width=Math.min(100,hero.pct)+"%"; $("hero-bar").style.background=hero.color;
    $("hero-abs").textContent=fabs(hero.resets_at);
    $("hero-burn").innerHTML=burnText(hero);
    if(sec){
      $("hero2-lab").textContent=sec.label;
      $("hero2-val").textContent=Math.round(sec.pct)+"% used";
      $("hero2-bar").style.width=Math.min(100,sec.pct)+"%"; $("hero2-bar").style.background=sec.color;
    }
    tickCountdowns();
  }
  // Context
  const c=d.context||{}, cp=(c.used_percentage!=null?c.used_percentage:null);
  $("home-ctx").innerHTML = cp==null
    ? "<div class='hsub'>no active session</div>"
    : "<div class='cval' style='color:"+bcol(cp)+"'>"+Math.round(cp)+"%</div><div class='bar'><i style='width:"+Math.min(100,cp)+"%;background:"+bcol(cp)+"'></i></div>"+
      (c.total_input_tokens?"<div class='hsub'>"+fmtTok(c.total_input_tokens)+" tokens</div>":"");
  // Status
  const sv=statusView(d), st=$("home-status");
  if(sv){ st.innerHTML="<div style='display:flex;align-items:center;gap:8px'><span class='sdot2' style='background:"+sv.color+"'></span><b>"+esc(sv.word)+"</b></div>"+
    (sv.text?"<div class='hsub'>"+esc(sv.text)+"</div>":""); }
  else { st.innerHTML="<div class='hsub'>status unavailable</div>"; }
  // Team mini
  fillHomeTeam();
  const t=(d.team)||{};
  if(t.in_team&&t.role==="admin"&&Date.now()-HOME_TEAM_TS>60000){ HOME_TEAM_TS=Date.now(); loadTeamOverview(); }
}
function fillHomeTeam(){
  const box=$("home-team"); if(!box)return;
  const t=(LASTD&&LASTD.team)||{};
  if(!t.in_team){ box.innerHTML="<div class='hsub'>Not in a team — set one up in the Team tab.</div>"; return; }
  if(t.role!=="admin"){ box.innerHTML="<div class='cval'>Member</div><div class='hsub'>of "+esc(t.name||"your team")+"</div>"; return; }
  const k=(TMOV&&TMOV.kpis)||null;
  if(!k){ box.innerHTML="<div class='hsub'>Open the Team tab for spend &amp; limits.</div>"; return; }
  const near=k.near||[];
  box.innerHTML="<div class='cval'>"+tmMoney(k.org_spend,tmCur(TMOV))+"</div>"+
    "<div class='hsub'>"+(k.account_count||0)+" accounts"+
    (near.length?" · <span style='color:var(--hot)'>"+near.length+" near limit</span>":" · all clear")+"</div>";
}
function renderSpark(h){
  const svg=$("spark"); svg.innerHTML="";
  const ns="http://www.w3.org/2000/svg";
  const W=svg.clientWidth||520,H=118,top=10,base=H-16;
  svg.setAttribute("viewBox","0 0 "+W+" "+H);
  const yv=p=>base-(p/100)*(base-top);
  function line(x1,y1,x2,y2,stroke){const l=document.createElementNS(ns,"line");
    l.setAttribute("x1",x1);l.setAttribute("y1",y1);l.setAttribute("x2",x2);l.setAttribute("y2",y2);
    l.setAttribute("stroke",stroke);svg.appendChild(l);}
  function text(x,y,t,anchor){const e=document.createElementNS(ns,"text");
    e.setAttribute("x",x);e.setAttribute("y",y);e.setAttribute("fill","#6c6b66");e.setAttribute("font-family","ui-monospace,Consolas,monospace");e.setAttribute("font-size","9");
    if(anchor)e.setAttribute("text-anchor",anchor);e.textContent=t;svg.appendChild(e);}
  [0,50,100].forEach(p=>{line(26,yv(p),W,yv(p),"rgba(255,255,255,.05)");text(22,yv(p)+3,p,"end");});
  if(!h||!h.t||h.t.length<2){
    text(W/2,H/2,"collecting data…","middle");return;
  }
  const t0=h.t[0],t1=h.t[h.t.length-1],span=Math.max(1,t1-t0);
  const x=t=>30+((t-t0)/span)*(W-34);
  [["#d97757",h.five_hour],["#7f93b0",h.seven_day]].forEach(c=>{
    const arr=c[1]; let d="", prev=null;
    for(let i=0;i<h.t.length;i++){
      // start a new segment across a window reset (a sharp drop) so we never draw a vertical cliff
      const cmd=(prev===null || arr[i] < prev-20)?"M":"L";
      d+=cmd+x(h.t[i]).toFixed(1)+" "+yv(arr[i]).toFixed(1)+" ";
      prev=arr[i];
    }
    const pa=document.createElementNS(ns,"path");pa.setAttribute("d",d.trim());pa.setAttribute("fill","none");pa.setAttribute("stroke",c[0]);pa.setAttribute("stroke-width","2.5");pa.setAttribute("stroke-linejoin","round");pa.setAttribute("stroke-linecap","round");svg.appendChild(pa);
    const dot=document.createElementNS(ns,"circle");dot.setAttribute("cx",x(t1));dot.setAttribute("cy",yv(arr[arr.length-1]));dot.setAttribute("r","3.4");dot.setAttribute("fill",c[0]);svg.appendChild(dot);
  });
}
function renderExtra(e){
  const box=$("extra");
  if(!e||!e.enabled){box.innerHTML="<div class='csub'>No overage credits enabled.</div>";return;}
  const cur=e.currency||"";
  const head=(e.used!=null&&e.limit!=null)?(cur+" "+e.used.toFixed(2)+" <span style='color:var(--dim);font-size:13px;font-weight:400'>of "+cur+" "+e.limit.toFixed(2)+"</span>"):(e.pct.toFixed(1)+"% used");
  const col=e.pct>=80?"#d4694f":"#5e9e72";
  box.innerHTML="<div class='credits'><div class='cval'>"+head+"</div>"+
    "<div class='bar'><i style='width:"+Math.min(100,e.pct)+"%;background:"+col+"'></i></div>"+
    "<div class='csub' style='margin-top:8px'>"+e.pct.toFixed(0)+"% of monthly overage cap</div></div>";
}
function renderScoped(wins){
  const box=$("scoped"); box.innerHTML="";
  const scoped=wins.filter(w=>w.key.startsWith("seven_day_"));
  if(!scoped.length)return;
  scoped.forEach(w=>{
    box.insertAdjacentHTML("beforeend",
      "<div class='mini'><div class='lbl'>"+w.label.replace('Weekly · ','')+"</div>"+
      "<div class='bar'><i style='width:"+Math.min(100,w.pct)+"%;background:"+w.color+"'></i></div>"+
      "<div class='num'>"+Math.round(w.pct)+"%</div></div>");
  });
}
function fmtTok(n){ n=n||0; if(n>=1e6)return (n/1e6).toFixed(n>=1e7?0:1)+"M"; if(n>=1e3)return Math.round(n/1e3)+"k"; return ""+n; }
function bandColor(p){ if(p>=80)return"#d4694f"; if(p>=60)return"#cda24e"; return"#5e9e72"; }
let SESS=[], SMODE="context";
function renderSessions(list){
  if(list) SESS=list;
  const box=$("sessions"), sub=$("sesssub");
  if(!SESS.length){ box.innerHTML="<div class='sempty'>no Claude sessions in the last 5h</div>"; sub.textContent=""; return; }
  const act=SESS.filter(s=>s.active).length;
  sub.innerHTML = act ? ("<span style='color:#5e9e72'>"+act+" active</span>") : "";
  const maxTok=Math.max.apply(null, SESS.map(s=>s.tokens).concat([1]));
  box.innerHTML=SESS.map(s=>{
    const nm=(s.name||"?").replace(/[<>&]/g,""); let w, val, col;
    if(SMODE==="context"){ const p=s.context_pct||0; w=Math.max(3,Math.min(100,p)); val=Math.round(p)+"%"; col=bandColor(p); }
    else { w=Math.max(3, s.tokens/maxTok*100); val=fmtTok(s.tokens); col="#7f93b0"; }
    return "<div class='srow'><span class='sdot "+(s.active?"on":"")+"'></span>"+
      "<span class='sname' title='"+nm+"'>"+nm+"</span>"+
      "<div class='sbar'><i style='width:"+w+"%;background:"+col+"'></i></div>"+
      "<span class='snum'>"+val+"</span></div>";
  }).join("");
}
function fmtTokFull(n){ n=n||0; if(n>=1e9)return (n/1e9).toFixed(2)+"B"; if(n>=1e6)return (n/1e6).toFixed(1)+"M"; if(n>=1e3)return (n/1e3).toFixed(1)+"k"; return ""+Math.round(n); }
const MODELCOL={Opus:"#d97757",Sonnet:"#7f93b0",Haiku:"#6e9c95",Fable:"#b08a6a"};
function modelColor(name){ for(const k in MODELCOL){ if((name||"").indexOf(k)===0)return MODELCOL[k]; } return "#6c6b66"; }
let AT=null, ATVIEW="overview", ATPERIOD="all";
function atBar(name,tokens,max,col){
  const nm=(name||"?").replace(/[<>&]/g,"");
  const w=Math.max(3,tokens/(max||1)*100);
  return "<div class='atrow'><span class='anm' title='"+nm+"'>"+nm+"</span>"+
    "<div class='abar'><i style='width:"+w+"%;background:"+col+"'></i></div>"+
    "<span class='anum'>"+fmtTokFull(tokens)+"</span></div>";
}
function modelRow(x){
  return "<div class='mrow'><span class='mdot' style='background:"+modelColor(x.name)+"'></span>"+
    "<span class='mname'>"+esc(x.name)+"</span>"+
    "<span class='mio'>"+fmtTokFull(x.in)+" in · "+fmtTokFull(x.out)+" out</span>"+
    "<span class='mshare'>"+(x.share!=null?x.share.toFixed(1):"0")+"%</span></div>";
}
function renderHeatmap(hm){
  const box=$("at-heatmap"); if(!box)return; box.innerHTML="";
  if(!hm||!hm.days)return;
  hm.days.forEach(c=>{ const el=document.createElement("div");
    el.className="hm"+(c.lvl?(" l"+c.lvl):"");
    el.title=c.d+(c.tok?(" · "+fmtTokFull(c.tok)+" tokens"):"");
    box.appendChild(el); });
}
function renderSeries(bins){
  const svg=$("at-series"); if(!svg)return; svg.innerHTML="";
  const ns="http://www.w3.org/2000/svg";
  const W=svg.clientWidth||520,H=152,top=12,base=H-20;
  svg.setAttribute("viewBox","0 0 "+W+" "+H);
  if(!bins||!bins.length)return;
  const max=Math.max.apply(null,bins.map(b=>b.total).concat([1]));
  const n=bins.length, step=(W-8)/n, bw=Math.max(2,step-3);
  const bl=document.createElementNS(ns,"line");bl.setAttribute("x1",4);bl.setAttribute("y1",base);bl.setAttribute("x2",W-4);bl.setAttribute("y2",base);bl.setAttribute("stroke","rgba(255,255,255,.10)");svg.appendChild(bl);
  bins.forEach((b,i)=>{ let y=base; const x=4+i*step;
    (b.segs||[]).forEach(s=>{ const h=(s.tok/max)*(base-top); if(h<=0)return;
      const r=document.createElementNS(ns,"rect");
      r.setAttribute("x",x.toFixed(1));r.setAttribute("y",(y-h).toFixed(1));
      r.setAttribute("width",bw.toFixed(1));r.setAttribute("height",h.toFixed(1));
      r.setAttribute("fill",modelColor(s.name));
      const t=document.createElementNS(ns,"title");t.textContent=b.label+" · "+s.name+" · "+fmtTokFull(s.tok);r.appendChild(t);
      svg.appendChild(r); y-=h; });
  });
  const lbl=(x,txt,anc)=>{const e=document.createElementNS(ns,"text");e.setAttribute("x",x);e.setAttribute("y",H-5);e.setAttribute("fill","#6c6b66");e.setAttribute("font-family","ui-monospace,Consolas,monospace");e.setAttribute("font-size","9");if(anc)e.setAttribute("text-anchor",anc);e.textContent=txt;svg.appendChild(e);};
  lbl(4,bins[0].label,"start"); if(n>1)lbl(W-4,bins[n-1].label,"end");
}
let LASTD={};
const BARFIELDS=[["dir","Directory"],["acct","Account"],["ctx","Context %"],["5h","5-hour %"],["7d","Weekly %"],["verdict","Verdict"],["status","Anthropic status"]];
async function postCfg(patch){ try{ await fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(patch)}); }catch(e){} }
async function postOverlay(id){ try{ await fetch("/api/overlay",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id})}); }catch(e){} }
async function postRemote(action){ try{ await fetch("/api/remote",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action})}); }catch(e){} }
function renderRemote(){
  const r=LASTD.remote||{}; const avail=r.available!==false;
  const un=$("rm-unavail"); if(un) un.hidden=avail;
  $("rm-enabled").checked=!!r.enabled; $("rm-enabled").disabled=!avail;
  if(document.activeElement!==$("rm-url")) $("rm-url").value=r.relay_url||"";
  const paired=!!(r.enabled && r.relay_url && avail);
  $("rm-pairwrap").hidden=!paired;
  if(paired && $("rm-qr").dataset.url!==r.relay_url){ $("rm-qr").dataset.url=r.relay_url; $("rm-qr").src="/api/pair-qr?ts="+Date.now(); }
  const st=$("rm-state");
  if(st) st.textContent = !avail ? "unavailable" : (r.enabled ? (r.last_sync_ok===true?"synced":(r.last_sync_ok===false?"sync error":"on")) : "off");
}
function syncOverlayLabels(){
  const a=LASTD.overlays_alive||{};
  const b=$("set-toggle-bar"), w=$("set-toggle-widget");
  if(b){ b.textContent=a.bar?"Hide":"Show"; b.classList.toggle("on",!!a.bar); }
  if(w){ w.textContent=a.widget?"Hide":"Show"; w.classList.toggle("on",!!a.widget); }
}
function renderSettings(){
  const ui=LASTD.ui||{};
  $("set-bar-start").checked=!!ui.show_bar_on_start;
  $("set-widget-start").checked=ui.show_widget_on_start!==false;
  const cur=(ui.bar_fields&&ui.bar_fields.length)?ui.bar_fields:["dir","ctx","5h","7d"];
  $("set-fields").innerHTML=BARFIELDS.map(f=>
    "<label><input type='checkbox' data-f='"+f[0]+"'"+(cur.indexOf(f[0])>=0?" checked":"")+"> "+f[1]+"</label>").join("");
  $("set-fields").querySelectorAll("input").forEach(c=>c.addEventListener("change",()=>{
    const picked=BARFIELDS.map(f=>f[0]).filter(k=>$("set-fields").querySelector("input[data-f='"+k+"']").checked);
    postCfg({bar_fields:picked.length?picked:["dir"]});
  }));
  syncOverlayLabels();
  renderStatusPicker();
  renderStatusPage();   // live Anthropic status now lives in Settings
  renderRemote();
  $("set-sesswait").checked=!!ui.notify_session_waiting;
  $("rm-transcript").checked=!!ui.remote_transcript;
}
const COMP_RANK={operational:0,under_maintenance:1,degraded_performance:2,partial_outage:3,major_outage:4};
const IND_SEV={none:0,minor:2,major:4,critical:4};
function sevColor(sev){ return sev<=0?"#5e9e72":(sev>=4?"#c94f38":"#cda24e"); }
function sevWord(sev){ return sev<=0?"Ok":(sev>=4?"Down":"Errors"); }
function prettyStatus(s){ return (s||"").replace(/_/g," ").replace(/^./,c=>c.toUpperCase()); }
function statusView(d){          // -> {color, word: Ok|Errors|Down, text, url}
  const s=d.status; if(!s)return null;
  const watch=((d.ui||{}).status_components)||[];
  let sev=0, text=s.description||"";
  if(watch.length && s.components){
    s.components.filter(c=>watch.indexOf(c.name)>=0).forEach(c=>{ sev=Math.max(sev,COMP_RANK[c.status]||0); });
  } else {
    sev=(IND_SEV[s.indicator]!=null?IND_SEV[s.indicator]:0);
  }
  return {color:sevColor(sev), word:sevWord(sev), text:text, url:s.url};
}
function renderStatusPage(){
  const s=LASTD.status, head=$("status-head"), box=$("status-list");
  if(!head||!box)return;
  if(!s){ head.textContent="status unavailable"; box.innerHTML=""; return; }
  const ov=statusView({status:s, ui:{}});
  head.innerHTML="<span style='color:"+ov.color+"'>"+ov.word+"</span> <span style='font-size:.55em;color:var(--dim);font-family:var(--sans)'>"+esc(s.description||"")+"</span>";
  box.innerHTML=(s.components||[]).map(c=>{ const sev=COMP_RANK[c.status]||0;
    return "<div class='strow'><span class='sdotb' style='background:"+sevColor(sev)+"'></span>"+
      "<span class='sname2'>"+esc(c.name)+"</span>"+
      "<span class='sword' style='color:"+sevColor(sev)+"' title='"+esc(prettyStatus(c.status))+"'>"+sevWord(sev)+"</span></div>";
  }).join("")||"<div class='sempty'>no components</div>";
}
function renderStatusPicker(){
  const box=$("set-status"); if(!box)return;
  const comps=((LASTD.status||{}).components)||[], watch=((LASTD.ui||{}).status_components)||[];
  if(!comps.length){ box.innerHTML="<div class='sempty'>status unavailable</div>"; return; }
  box.innerHTML=comps.map(c=>"<label><input type='checkbox' data-c=\""+esc(c.name)+"\""+(watch.indexOf(c.name)>=0?" checked":"")+"> "+esc(c.name)+"</label>").join("");
  box.querySelectorAll("input").forEach(i=>i.addEventListener("change",()=>{
    const picked=[...box.querySelectorAll("input")].filter(x=>x.checked).map(x=>x.dataset.c);
    postCfg({status_components:picked});
  }));
}
function renderAlltime(a){
  if(a)AT=a;
  if(!AT||!AT.ready)return;                       // keep the "calculating…" placeholder
  const p=AT.periods[ATPERIOD]||AT.periods.all;
  const fmtN=n=>(n||0).toLocaleString();
  const cards=[["Sessions",fmtN(p.sessions)],["Messages",fmtN(p.messages)],["Total tokens",fmtTokFull(p.tokens)],["Active days",fmtN(p.active_days)],
    ["Current streak",AT.streak_current+"d"],["Longest streak",AT.streak_longest+"d"],["Peak hour",AT.peak_hour||"—"],["Favorite model",p.fav_model||"—"]];
  $("at-stats").innerHTML=cards.map(c=>"<div class='stat'><div class='n'>"+esc(""+c[1])+"</div><div class='k'>"+c[0]+"</div></div>").join("");
  $("at-compare").textContent=p.compare?("You've burned ~"+p.compare.x.toLocaleString()+"× more tokens than "+p.compare.name+"."):"";
  renderHeatmap(AT.heatmap);
  const pj=AT.projects||[], pmax=Math.max.apply(null,pj.map(x=>x.tokens).concat([1]));
  $("at-projects").innerHTML=pj.map(x=>atBar(x.name,x.tokens,pmax,"#7f93b0")).join("")||"<div class='sempty'>no data yet</div>";
  $("at-projmore").textContent=(AT.project_count>pj.length)?("top "+pj.length+" of "+AT.project_count):"all-time";
  $("at-models").innerHTML=(p.models||[]).map(modelRow).join("")||"<div class='sempty'>no data yet</div>";
  if(ATVIEW==="models" && !$("tab-history").hidden) renderSeries(p.series);
}
async function refresh(){
  try{
    const d=await (await fetch("/api/usage",{cache:"no-store"})).json();
    LASTD=d;
    const wins=d.windows||[];
    const authBad=d.token_state==="expired"||d.token_state==="missing";
    const err=$("err");
    // Big banner only when there's nothing to show (or login needs refreshing);
    // otherwise keep the last data and show a subtle "stale" state.
    if(!d.ok && (authBad || !wins.length)){
      err.className="err show";
      if(authBad){
        err.className="err show authcard";
        err.innerHTML=
          "<div class='signincard'><div class='sc-mark'>C</div><div class='sc-body'>"+
          "<div class='sc-title'>Sign in to Claude</div>"+
          "<div class='sc-sub'>"+esc(d.error||"Your Claude login needs refreshing.")+" Signing in opens claude.ai in your browser.</div>"+
          "<div class='sc-row'><input id='signin-email' type='email' placeholder='you@email.com (optional)' autocomplete='email'>"+
          "<button class='sc-btn' id='signin'>Sign in</button></div>"+
          "<div class='sc-status' id='signin-status'>The tracker never sees your password — Claude Code handles it. "+
          "<button class='linkbtn' id='signin-term'>open a terminal instead</button></div>"+
          "</div></div>";
        $("signin").onclick=function(){ doSignin(undefined,false); };
        var _t=$("signin-term"); if(_t) _t.onclick=function(e){ e.preventDefault(); doSignin(undefined,true); };
      }else{
        err.textContent="⚠ "+(d.error||"waiting for data")+" — retrying…";
      }
    }else{ err.className="err"; }
    const acc=d.account||{};
    $("tier").textContent = acc.org || d.subscription || "plan";
    $("tier").title = acc.email || "";
    const vp=$("vpill"), v=d.verdict;
    if(v && v.text){ vp.style.display=""; vp.textContent=v.text; vp.style.color=v.color; vp.style.borderColor=v.color+"66"; vp.style.background=v.color+"1f"; }
    else { vp.style.display="none"; }
    if(d.ok){
      $("livedot").style.background="#5e9e72"; $("livetxt").textContent="live";
      $("updated").textContent="updated "+new Date(d.updated_at*1000).toLocaleTimeString();
    }else{
      $("livedot").style.background="#cda24e"; $("livetxt").textContent=wins.length?"stale":"offline";
      $("updated").textContent=wins.length?("paused · "+(d.error||"waiting for Claude Code activity")):(d.error||"—");
    }
    renderHome(d);
    LASTH=d.history; renderSpark(LASTH);
    renderExtra(d.extra);
    renderScoped(wins);
    renderSessions(d.sessions);
    renderAlltime(d.alltime);
    const up=d.update||{}, cta=$("updcta");
    if(cta){ if(up.available){ cta.hidden=false; if(!cta.disabled)cta.textContent="Update to v"+up.available; } else { cta.hidden=true; } }
    const sv=statusView(d), sp=$("statuspill");
    if(sp){ if(sv){ sp.hidden=false; sp.href=sv.url||"#"; sp.title="Anthropic status — "+sv.text; sp.innerHTML="<i style='background:"+sv.color+"'></i>"+esc(sv.word); } else { sp.hidden=true; } }
    if(!$("tab-settings").hidden)renderStatusPage();
    renderRemote();
    renderTeamState(d);
    tickCountdowns();
  }catch(e){ $("err").className="err show"; $("err").textContent="⚠ cannot reach the tracker service."; }
}
$("btn-refresh").onclick=doRefresh;
$("btn-checkupd").onclick=doCheckUpdate;
$("updcta").onclick=doUpdate;
setInterval(tickCountdowns,1000);
setInterval(refresh,5000);
refresh();
let _rz; window.addEventListener("resize",()=>{clearTimeout(_rz);_rz=setTimeout(()=>{renderSpark(LASTH); if(!$("tab-history").hidden)renderAlltime();},120);});
document.querySelectorAll("#sesscard .stab").forEach(b=>b.addEventListener("click",()=>{
  document.querySelectorAll("#sesscard .stab").forEach(x=>x.classList.remove("on"));
  b.classList.add("on"); SMODE=b.dataset.m; renderSessions();
}));
document.querySelectorAll("#at-view .stab").forEach(b=>b.addEventListener("click",()=>{
  document.querySelectorAll("#at-view .stab").forEach(x=>x.classList.remove("on")); b.classList.add("on");
  ATVIEW=b.dataset.v; $("at-overview").hidden=(ATVIEW!=="overview"); $("at-models-view").hidden=(ATVIEW!=="models");
  renderAlltime();
}));
document.querySelectorAll("#at-period .stab").forEach(b=>b.addEventListener("click",()=>{
  document.querySelectorAll("#at-period .stab").forEach(x=>x.classList.remove("on")); b.classList.add("on");
  ATPERIOD=b.dataset.p; renderAlltime();
}));
document.querySelectorAll(".navbar button").forEach(b=>b.addEventListener("click",()=>{
  document.querySelectorAll(".navbar button").forEach(x=>x.classList.remove("on"));
  b.classList.add("on");
  const t=b.dataset.t;
  $("tab-home").hidden=(t!=="home"); $("tab-history").hidden=(t!=="history");
  $("tab-team").hidden=(t!=="team"); $("tab-settings").hidden=(t!=="settings");
  if(t==="home"&&typeof renderHome==="function")renderHome(LASTD);
  if(t==="history")renderAlltime();   // (re)draw now that the pane has layout
  if(t==="settings")renderSettings();
  if(t==="team")renderTeamPage();
}));
$("set-toggle-bar").onclick=function(){ const sh=this.textContent==="Hide"; this.textContent=sh?"Show":"Hide"; this.classList.toggle("on",!sh); postOverlay("bar"); };
$("set-toggle-widget").onclick=function(){ const sh=this.textContent==="Hide"; this.textContent=sh?"Show":"Hide"; this.classList.toggle("on",!sh); postOverlay("widget"); };
$("set-bar-start").onchange=function(){ postCfg({show_bar_on_start:this.checked}); };
$("set-widget-start").onchange=function(){ postCfg({show_widget_on_start:this.checked}); };
$("set-refresh").onclick=doRefresh; $("set-check").onclick=doCheckUpdate; $("set-login").onclick=function(){ doSignin("",false); };
$("rm-enabled").onchange=function(){ postCfg({remote_enabled:this.checked}); setTimeout(refresh,300); };
$("rm-save").onclick=function(){ postCfg({remote_relay_url:$("rm-url").value.trim()}); $("rm-qr").dataset.url=""; setTimeout(refresh,400); };
$("rm-sync").onclick=function(){ postRemote("sync"); setTimeout(refresh,500); };
$("rm-rotate").onclick=function(){ if(confirm("Rotate the key? Your phone must re-pair.")){ postRemote("rotate"); $("rm-qr").dataset.url=""; setTimeout(refresh,500); } };
$("rm-unpair").onclick=function(){ if(confirm("Unpair and disable remote sync?")){ postRemote("unpair"); setTimeout(refresh,500); } };
$("set-sesswait").onchange=function(){ postCfg({notify_session_waiting:this.checked}); };
$("rm-transcript").onchange=function(){ postCfg({remote_transcript:this.checked}); };

/* ---- team tab ---- */
let TMMONTH=null, TMLED=null, TMOV=null, WHOAMI=null;
function tmMoney(v,cur){ return v==null?"—":((cur?cur+" ":"")+Number(v).toFixed(2)); }
function tmMonthNow(){ const d=new Date(); return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0"); }
function tmShiftMonth(m,dir){ let y=+m.slice(0,4),mo=+m.slice(5,7)+dir; if(mo<1){mo=12;y--;} if(mo>12){mo=1;y++;} return y+"-"+String(mo).padStart(2,"0"); }
function renderTeamState(){
  const w=WHOAMI||{};
  $("tm-login").hidden=!!w.signed_in;
  $("tm-me").hidden=!w.signed_in;
  $("tm-adminview").hidden=!w.signed_in;
  $("tm-connector").hidden=!w.is_admin;
  if(!w.signed_in)return;
  $("tm-mstate").textContent=w.is_admin?"admin":"member";
  $("tm-minfo").innerHTML="Signed in as <b>"+esc(w.email||"")+"</b> · team <b>"+esc(w.team||"")+"</b>";
}
function tmTok(n){ if(n==null)return "—"; if(n>=1e9)return (n/1e9).toFixed(1)+"B"; if(n>=1e6)return (n/1e6).toFixed(1)+"M"; if(n>=1e3)return (n/1e3).toFixed(1)+"k"; return String(n); }
function tmWin(label,p,reset){
  const pct=(p==null)?"—":Math.round(p)+"%";
  const c=p==null?"#3a352f":(p>=80?"#d4694f":(p>=60?"#cda24e":"#5e9e72"));
  const cap=label+" · "+pct+(reset?" · "+reset:"");
  return "<div class='tmwin'><div class='wcap'><span>"+cap+"</span></div>"+
         "<div class='bar'><i style='width:"+(p==null?0:Math.min(100,p))+"%;background:"+c+"'></i></div></div>";
}
function tmReset(iso){ if(!iso)return ""; const d=new Date(iso); if(isNaN(d))return "";
  const now=new Date(); return d.toDateString()===now.toDateString()
    ? d.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"})
    : d.toLocaleDateString([],{weekday:"short"}); }
function tmCur(d){ for(const mm of (d.accounts||[])){ const c=((mm.account||{}).extra||{}).currency; if(c)return c; } return ""; }
async function loadTeamOverview(){
  const box=$("tm-members");
  try{
    const d=await (await fetch("/api/team/overview",{cache:"no-store"})).json();
    TMOV=d;
    fillHomeTeam();   // keep the Home team-mini in sync with the freshest overview
    if(d.error){ box.innerHTML="<div class='csub'>"+esc(d.error)+"</div>"; return; }
    $("tm-asof").textContent="today "+esc(d.today||"")+" · "+esc(d.tz||"");
    const k=d.kpis||{};
    $("tm-kpi-spend").textContent=(k.org_spend!=null)?tmMoney(k.org_spend,tmCur(d)):"—";
    $("tm-kpi-spend-sub").textContent="across "+(k.account_count||0)+" account"+((k.account_count||0)===1?"":"s")+" since the 1st";
    const near=k.near||[];
    $("tm-kpi-near").textContent=near.length;
    $("tm-kpi-near-sub").textContent=near.slice(0,3).map(n=>n.name+" "+n.window).join(" · ")||"all clear";
    $("tm-kpi-near-card").className="tmkpi"+(near.length?" warn":"");
    if(!(d.accounts||[]).length){ box.innerHTML="<div class='csub'>No accounts in the pool yet — a teammate reports one by logging into it.</div>"; return; }
    // Freshest first: lowest 5h load on top so the team grabs the least-used account.
    const pool=(d.accounts||[]).slice().sort((a,b)=>{
      const pa=(a.account||{}).fh_pct, pb=(b.account||{}).fh_pct;
      return (pa==null?1e9:pa)-(pb==null?1e9:pb);
    });
    box.innerHTML=pool.map(m=>{
      const r=m.account||{}, e=r.extra||{}, lu=m.last_used||{};
      const at=lu.ts||r.ts;
      const seen=at?new Date(at*1000).toLocaleString([],{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}):"no data";
      const tag="last used "+esc(seen)+(lu.device?" · "+esc(lu.device):"")+(lu.by?" · by "+esc(lu.by):"")+((m.escrow||{}).present?" · escrow ✓":"");
      const devs=(m.devices||[]).map(dv=>"<span class='tmdev'>"+esc(dv.device||dv.did||"device")+" <b>"+tmTok(dv.tok_month)+"</b></span>").join("");
      const sum=(m.devices||[]).length>1?"<span class='csub' style='align-self:center'>"+tmTok(m.month_tokens)+" this month</span>":"";
      return "<div class='tmrow'><div class='tmtop'>"+
        "<div class='tmname'><b>"+esc(m.name)+"</b><span class='csub'>"+tag+"</span></div>"+
        tmWin("5h",r.fh_pct,tmReset(r.fh_resets_at))+
        tmWin("weekly",r.sd_pct,tmReset(r.sd_resets_at))+
        "<div class='tmspend'><b>"+(m.month_spend!=null?tmMoney(m.month_spend,e.currency):"—")+"</b>"+
        "<br><span class='csub'>since the 1st"+(e.enabled&&e.pct!=null?" · "+Math.round(e.pct)+"% of cap":"")+"</span></div>"+
        "</div>"+(devs?"<div class='tmdevs'>"+devs+sum+"</div>":"")+"</div>";
    }).join("");
  }catch(e){ box.innerHTML="<div class='csub'>relay unreachable</div>"; }
}
async function loadTeamLedger(){
  const box=$("tm-ledger");
  if(!TMMONTH)TMMONTH=tmMonthNow();
  $("tm-month").textContent=TMMONTH;
  try{
    const d=await (await fetch("/api/team/ledger?month="+TMMONTH,{cache:"no-store"})).json();
    TMLED=d;
    if(d.error){ box.innerHTML="<div class='csub'>"+esc(d.error)+"</div>"; return; }
    const names=d.accounts||{}, spend=d.computed_spend||{}, finals=d.finals||{}, days=d.days||{}, monthTok=d.month_tokens||{};
    const dates=Object.keys(days).sort();
    const mids=Object.keys(names); Object.keys(spend).forEach(m=>{ if(mids.indexOf(m)<0)mids.push(m); });
    if(!mids.length){ box.innerHTML="<div class='csub'>No data for "+esc(TMMONTH)+" yet.</div>"; return; }
    const lastRow=m=>{ for(let i=dates.length-1;i>=0;i--){ const a=tmAcct((days[dates[i]]||{})[m]); if(a)return a; } return finals[m]||null; };
    let html="<table class='tmtable'><tr><th>account</th><th class='r'>€ month</th><th class='r'>tokens</th><th class='r'>meter</th><th class='r'>cap</th><th class='r'>days</th><th>state</th></tr>";
    mids.sort((a,b)=>(spend[b]||0)-(spend[a]||0)).forEach(m=>{
      const fin=finals[m], lr=fin||lastRow(m)||{}, e=lr.extra||{};
      const nDays=dates.filter(dt=>{ const a=tmAcct((days[dt]||{})[m]); return a&&((a.extra||{}).used!=null); }).length;
      html+="<tr><td>"+esc(names[m]||m.slice(0,8))+"</td>"+
        "<td class='r'><b>"+tmMoney(spend[m],e.currency)+"</b></td>"+
        "<td class='r'>"+tmTok(monthTok[m])+"</td>"+
        "<td class='r'>"+tmMoney(e.used,e.currency)+"</td>"+
        "<td class='r'>"+tmMoney(e.limit,e.currency)+"</td>"+
        "<td class='r'>"+nDays+"</td>"+
        "<td>"+(fin?"frozen":esc(lr.src||"—"))+"</td></tr>";
    });
    box.innerHTML=html+"</table>";
  }catch(e){ box.innerHTML="<div class='csub'>relay unreachable</div>"; }
}
// The account-authoritative row among a member's device rows for one day: cron's
// `account` if present, else the newest push. Mirrors the relay + Python.
function tmAcct(devmap){ if(!devmap)return null; if(devmap.account)return devmap.account;
  let best=null; for(const k of Object.keys(devmap)){ const r=devmap[k]; if(r&&(!best||(r.ts||0)>(best.ts||0)))best=r; } return best; }
function tmCsv(){
  if(!TMLED||TMLED.error)return;
  const names=TMLED.accounts||{}, spend=TMLED.computed_spend||{}, finals=TMLED.finals||{}, days=TMLED.days||{}, monthTok=TMLED.month_tokens||{};
  const dates=Object.keys(days).sort();
  const lastRow=m=>{ for(let i=dates.length-1;i>=0;i--){ const a=tmAcct((days[dates[i]]||{})[m]); if(a)return a; } return finals[m]||null; };
  let csv="account,month,spend,currency,tokens,meter_end,cap,days_sampled,final_frozen\r\n";
  const mids=Object.keys(names); Object.keys(spend).forEach(m=>{ if(mids.indexOf(m)<0)mids.push(m); });
  mids.forEach(m=>{
    const lr=finals[m]||lastRow(m)||{}, e=lr.extra||{};
    csv+='"'+String(names[m]||m).replace(/"/g,'""')+'",'+TMLED.month+","+(spend[m]!=null?spend[m]:"")+","+(e.currency||"")+","+
      (monthTok[m]!=null?monthTok[m]:"")+","+
      (e.used!=null?e.used:"")+","+(e.limit!=null?e.limit:"")+","+dates.filter(dt=>!!(days[dt]||{})[m]).length+","+(finals[m]?"yes":"no")+"\r\n";
  });
  const a=document.createElement("a");
  a.href=URL.createObjectURL(new Blob([csv],{type:"text/csv"}));
  a.download="claude-team-ledger-"+TMLED.month+".csv"; a.click(); URL.revokeObjectURL(a.href);
}
async function tmPost(body){
  try{ return await (await fetch("/api/team",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})).json(); }
  catch(e){ return {ok:false,error:"tracker unreachable"}; }
}
// Pool accounts are read-only cards (they auto-discover as teammates log in); there is no
// per-card remove. Reporter enrolment stays in "add member" (join codes); the relay's
// /member DELETE endpoint remains for revoking a reporter out-of-band.
async function renderTeamPage(){
  try{ WHOAMI=await (await fetch("/api/team/whoami",{cache:"no-store"})).json(); }
  catch(e){ WHOAMI={signed_in:false}; }
  renderTeamState();
  if(WHOAMI.signed_in){ loadTeamOverview(); loadTeamLedger(); }
}
$("tm-sendcode").onclick=async function(){
  if(!$("tm-consent").checked){ $("tm-err").textContent="please accept the data notice first"; return; }
  const email=$("tm-email").value.trim(); if(!email){ $("tm-email").focus(); return; }
  this.disabled=true; const r=await tmPost({action:"login-start",email:email}); this.disabled=false;
  if(r.ok){ $("tm-codewrap").hidden=false; $("tm-err").textContent="code sent to "+email; $("tm-otp").focus(); }
  else $("tm-err").textContent="x "+(r.error||"could not send code");
};
$("tm-signin").onclick=async function(){
  const email=$("tm-email").value.trim(), code=$("tm-otp").value.trim(), username=$("tm-username").value.trim();
  if(!code){ $("tm-otp").focus(); return; }
  this.disabled=true; const r=await tmPost({action:"login-verify",email:email,code:code,username:username}); this.disabled=false;
  if(r.ok){ $("tm-err").textContent=""; $("tm-otp").value=""; renderTeamPage(); setTimeout(refresh,400); }
  else $("tm-err").textContent="x "+(r.error||"invalid code");
};
$("tm-logout").onclick=async function(){
  if(!confirm("Sign out of the account pool on this device?"))return;
  await tmPost({action:"logout"}); renderTeamPage(); setTimeout(refresh,400);
};
$("tm-reload").onclick=loadTeamOverview;
$("tm-minttoken").onclick=async function(){
  const msg=$("tm-copytoken-msg");
  const r=await tmPost({action:"connector-token"});
  if(!r.ok||!r.token){ msg.textContent="x "+(r.error||"mint failed"); return; }
  try{ await navigator.clipboard.writeText(r.token); msg.textContent="copied - paste it at the claude.ai consent screen (shown once)"; }
  catch(e){ msg.style.userSelect="all"; msg.textContent=r.token; }
};
$("tm-prevm").onclick=function(){ TMMONTH=tmShiftMonth(TMMONTH||tmMonthNow(),-1); loadTeamLedger(); };
$("tm-nextm").onclick=function(){ TMMONTH=tmShiftMonth(TMMONTH||tmMonthNow(),1); loadTeamLedger(); };
$("tm-csv").onclick=tmCsv;
setInterval(function(){ if(!$("tab-team").hidden&&WHOAMI&&WHOAMI.signed_in)loadTeamOverview(); },10000);
</script>
</body>
</html>"""


WIDGET_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Usage</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{height:100%}
  :root{--ink:#e7e6e3;--dim:#9b9a95;--faint:#6c6b66;--accent:#d97757;
    --mono:ui-monospace,"Cascadia Mono","Cascadia Code","SF Mono",Consolas,monospace;
    --sans:ui-sans-serif,"Segoe UI",system-ui,-apple-system,sans-serif}
  body{font:12px/1.4 var(--sans);color:var(--ink);
    background:#141416;border:1px solid rgba(255,255,255,.10);border-radius:12px;
    overflow:hidden;padding:clamp(9px,2.6vw,13px) clamp(10px,3vw,14px);
    user-select:none;-webkit-user-select:none;cursor:default;
    display:flex;flex-direction:column;gap:clamp(6px,1.6vh,10px)}
  .top{display:flex;align-items:center;gap:7px;color:var(--faint);font:10px/1 var(--mono);flex:none}
  .top .dot{width:6px;height:6px;border-radius:50%;background:var(--accent);flex:none}
  .top .ttl{text-transform:uppercase;letter-spacing:.4px;max-width:46%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .top .tier{color:var(--ink);font:600 12.5px/1 var(--sans);letter-spacing:0;
    max-width:52%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  select.tier{appearance:none;-webkit-appearance:none;border:0;background-color:transparent;cursor:pointer;
    padding:3px 18px 3px 6px;border-radius:6px;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='9' height='6' viewBox='0 0 9 6' fill='none' stroke='%239b9a95' stroke-width='1.25' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M1 1.5 4.5 4.5 8 1.5'/%3E%3C/svg%3E");
    background-repeat:no-repeat;background-position:right 6px center;background-size:9px 6px}
  select.tier:hover{background-color:rgba(255,255,255,.06);color:var(--accent)}
  select.tier:focus{outline:none;background-color:rgba(255,255,255,.06)}
  select.tier option{background:#1d1d20;color:var(--ink);font-weight:500}
  .top .verdict{margin-left:auto;font:700 11px/1 var(--sans);padding-right:6px;white-space:nowrap}
  .top .x{cursor:pointer;color:var(--faint);font-size:15px;line-height:1;padding:0 3px}
  .top .x:hover{color:var(--ink)}
  .sdot2{width:7px;height:7px;border-radius:50%;flex:none;display:inline-block}
  .wstat{display:inline-flex;align-items:center;gap:4px;font:600 10px/1 var(--mono);white-space:nowrap}
  #body{flex:1;display:flex;flex-direction:column;justify-content:center;gap:clamp(6px,1.8vh,11px);min-height:0}
  .row{display:flex;align-items:center;gap:10px}
  .lab{width:34px;color:var(--dim);font:600 11px/1 var(--mono);text-transform:uppercase}
  .bar{flex:1;height:8px;border-radius:4px;background:rgba(255,255,255,.10);overflow:hidden}
  .bar>i{display:block;height:100%;border-radius:4px;width:0;transition:width .7s cubic-bezier(.22,1,.36,1)}
  .pc{width:44px;text-align:right;font:600 clamp(14px,4.2vw,16px)/1 var(--mono);font-variant-numeric:tabular-nums}
  .cd{width:62px;text-align:right;color:var(--faint);font:10.5px/1 var(--mono);font-variant-numeric:tabular-nums}
  .acts{display:flex;gap:6px;flex:none}
  .btn{flex:1;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.10);color:var(--dim);
    font:600 11px/1 var(--sans);padding:8px;border-radius:7px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .btn:hover{color:var(--ink);border-color:rgba(255,255,255,.22)}
  .btn:active{transform:translateY(1px)} .btn:disabled{opacity:.7;cursor:default}
  .err{color:#e9b3a6;font:11px/1 var(--mono);padding:6px 2px}
  /* ---- minimal "bar" kind (FPS-overlay style) ---- */
  body.kind-bar{flex-direction:row;align-items:center;gap:0;padding:5px 11px;
    background:#0e0e10;border:1px solid rgba(255,255,255,.10);border-radius:9px}
  body.kind-bar .top,body.kind-bar #body,body.kind-bar .acts{display:none}
  #bar{display:none}
  body.kind-bar #bar{display:flex;align-items:center;gap:14px;width:100%;overflow:hidden;
    font:600 12px/1 var(--mono);white-space:nowrap;text-shadow:0 1px 2px rgba(0,0,0,.6)}
  #bar .f{display:inline-flex;gap:5px;align-items:baseline;color:var(--dim)}
  #bar .f b{font-weight:700;font-variant-numeric:tabular-nums}
  #bar .dir{color:var(--ink);font-weight:700;max-width:42%;overflow:hidden;text-overflow:ellipsis}
  #bar .acts2{margin-left:auto;display:flex;gap:8px;opacity:0;transition:opacity .15s}
  body.kind-bar:hover #bar .acts2{opacity:1}
  #bar .ic{cursor:pointer;color:var(--faint);font-size:13px;line-height:1}
  #bar .ic:hover{color:var(--ink)}
</style>
</head>
<body>
  <div class="top">
    <span class="dot" id="dot"></span><span class="ttl" id="acct">Claude usage</span>
    <select class="tier" id="tier" title="Track a session"></select><span class="verdict" id="verdict"></span><span class="wstat" id="wstatus" title="Anthropic status" style="display:none"></span><span class="x" onclick="closeWidget()" title="Hide">×</span>
  </div>
  <div id="body">
    <div class="row"><span class="lab">5h</span><div class="bar"><i id="b5"></i></div><span class="pc" id="pc5">–</span><span class="cd" id="cd5"></span></div>
    <div class="row"><span class="lab">Week</span><div class="bar"><i id="b7"></i></div><span class="pc" id="pc7">–</span><span class="cd" id="cd7"></span></div>
    <div class="row"><span class="lab">Ctx</span><div class="bar"><i id="bc"></i></div><span class="pc" id="pcc">–</span><span class="cd" id="cdc"></span></div>
  </div>
  <div class="acts">
    <button class="btn" id="w-refresh" onclick="refreshNow()">Refresh</button>
    <button class="btn" id="w-check" onclick="checkUpd()">Check for updates</button>
  </div>
  <div id="bar"></div>
<script>
const $=id=>document.getElementById(id);
let R={};
let SEL=""; try{ SEL=localStorage.getItem("trackSel")||""; }catch(_){}
const KIND=new URLSearchParams(location.search).get("kind")||"panel";
document.body.classList.add("kind-"+KIND);
function esc(s){ return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function bandColor(p){ if(p>=80)return"#d4694f"; if(p>=60)return"#cda24e"; return"#5e9e72"; }
function fmtTok(n){ n=n||0; if(n>=1e6)return (n/1e6).toFixed(1)+"M"; if(n>=1e3)return Math.round(n/1e3)+"k"; return ""+n; }
function closeWidget(){ try{ window.pywebview.api.close(); }catch(e){ try{window.close();}catch(_){} } }
async function refreshNow(){ const b=$("w-refresh"); if(b){b.disabled=true;b.textContent="Refreshing…";}
  try{ await fetch("/api/refresh",{method:"POST"}); }catch(e){}
  setTimeout(()=>{ refresh(); if(b){b.disabled=false;b.textContent="Refresh";} },700); }
async function checkUpd(){ const b=$("w-check"); if(!b)return; b.disabled=true; b.textContent="Checking…";
  try{ const d=await (await fetch("/api/check-update",{method:"POST"})).json();
    b.textContent=d.update?("v"+d.latest+" available"):(d.latest?"Up to date":"Check failed");
  }catch(e){ b.textContent="Check failed"; }
  setTimeout(()=>{ b.disabled=false; b.textContent="Check for updates"; },3500); }
function sdur(s){ if(s==null)return""; s=Math.max(0,Math.floor(s));
  const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  if(d>0)return"↻ "+d+"d "+h+"h"; if(h>0)return"↻ "+h+"h "+String(m).padStart(2,"0")+"m";
  if(m>0)return"↻ "+m+"m"; return"↻ "+(s%60)+"s"; }
function tick(){const now=Date.now();for(const k in R){const el=$("cd"+k);if(el)el.textContent=R[k]?sdur((R[k]-now)/1000):"";}}
function setRow(i,w){ const b=$("b"+i),pc=$("pc"+i);
  if(b){ b.style.width=Math.min(100,w.pct)+"%"; b.style.background=w.color; }
  if(pc){ pc.textContent=Math.round(w.pct)+"%"; pc.style.color=w.color; }
  R[i]=w.resets_at; }
function actDir(d){ return (d.cwd||"").replace(/[\\\/]+$/,"").split(/[\\\/]/).pop()||"active"; }
function buildOpts(d){          // populate the session picker; "" = follow the active terminal
  const sel=$("tier"), sessions=d.sessions||[];
  const sig=(d.cwd||"")+"|"+sessions.map(s=>s.name).join(",");
  if(sel._sig===sig)return; sel._sig=sig;
  let html="<option value=''>"+esc(actDir(d))+" · active</option>";
  sessions.forEach(s=>{ if(s.name) html+="<option value=\""+esc(s.name)+"\">"+esc(s.name)+"</option>"; });
  sel.innerHTML=html; sel.value=SEL;
  if(sel.value!==SEL){ SEL=""; sel.value=""; }   // pinned session aged out -> back to active
}
function curContext(d){         // context for the selected session, or the active terminal
  if(SEL){ const s=(d.sessions||[]).find(x=>x.name===SEL); if(s) return {pct:s.context_pct, tok:s.context_tokens}; }
  const c=d.context||{}; return {pct:(c.used_percentage!=null?c.used_percentage:null), tok:c.total_input_tokens};
}
function pctOf(wins,key){ const w=(wins||[]).find(x=>x.key===key); return w?w.pct:null; }
function bfld(label,pct){ return pct==null?"":"<span class='f'>"+label+" <b style='color:"+bandColor(pct)+"'>"+Math.round(pct)+"%</b></span>"; }
function refreshNow2(){ fetch("/api/refresh",{method:"POST"}).catch(()=>{}); setTimeout(refresh,500); }
const COMP_RANK={operational:0,under_maintenance:1,degraded_performance:2,partial_outage:3,major_outage:4};
const IND_SEV={none:0,minor:2,major:4,critical:4};
function sevColor(sev){ return sev<=0?"#5e9e72":(sev>=4?"#c94f38":"#cda24e"); }
function sevWord(sev){ return sev<=0?"Ok":(sev>=4?"Down":"Errors"); }
function statusView(d){          // -> {color, word: Ok|Errors|Down}
  const s=d.status; if(!s)return null;
  const watch=((d.ui||{}).status_components)||[];
  let sev=0;
  if(watch.length && s.components){
    s.components.filter(c=>watch.indexOf(c.name)>=0).forEach(c=>{ sev=Math.max(sev,COMP_RANK[c.status]||0); });
  } else { sev=(IND_SEV[s.indicator]!=null?IND_SEV[s.indicator]:0); }
  return {color:sevColor(sev), word:sevWord(sev)};
}
function renderBar(d){           // minimal one-line HUD (configurable fields, in order)
  const wins=d.windows||[], ui=d.ui||{};
  if(!d.ok && !wins.length){ $("bar").innerHTML="<span class='f dir'>"+esc(d.error||"unavailable")+"</span>"; return; }
  const cx=curContext(d), dir=SEL||actDir(d), acc=d.account||{}, v=d.verdict;
  const fields=(ui.bar_fields&&ui.bar_fields.length)?ui.bar_fields:["dir","ctx","5h","7d","status"];
  const part=f=>{
    if(f==="dir")    return "<span class='f dir' title='"+esc(d.cwd||"")+"'>"+esc(dir)+"</span>";
    if(f==="acct")   return "<span class='f dir'>"+esc(acc.org||acc.name||(acc.email||"").split("@")[0]||"")+"</span>";
    if(f==="ctx")    return bfld("Ctx:",cx.pct);
    if(f==="5h")     return bfld("5h:",pctOf(wins,"five_hour"));
    if(f==="7d"||f==="week") return bfld("7d:",pctOf(wins,"seven_day"));
    if(f==="verdict")return v&&v.text?"<span class='f' style='color:"+v.color+"'>"+esc(v.text)+"</span>":"";
    if(f==="status"){ const sv=statusView(d); return sv?"<span class='f' title='Anthropic status' style='color:"+sv.color+"'><span class='sdot2' style='background:"+sv.color+"'></span>"+sv.word+"</span>":""; }
    return "";
  };
  let html=fields.map(part).join("");
  html+="<span class='acts2'><span class='ic' onclick='refreshNow2()' title='Refresh'>↻</span>"+
        "<span class='ic' onclick='closeWidget()' title='Hide'>×</span></span>";
  $("bar").innerHTML=html;
}
async function refresh(){ try{
  const d=await (await fetch("/api/usage",{cache:"no-store"})).json();
  const wins=d.windows||[];
  if(KIND==="bar"){ renderBar(d); tick(); return; }
  if(!d.ok && !wins.length){ $("dot").style.background="#d4694f"; $("verdict").textContent=(d.error||"unavailable"); $("verdict").style.color="#d4694f"; return; }
  $("dot").style.background=d.ok?"#5e9e72":"#cda24e";
  const v=d.verdict;
  if(v && v.text){ $("verdict").textContent=v.text; $("verdict").style.color=v.color; if(d.ok)$("dot").style.background=v.color; }
  else { $("verdict").textContent=""; }
  const acc=d.account||{};
  $("acct").textContent = acc.org || acc.name || (acc.email||"").split("@")[0] || "Claude";
  $("acct").title = acc.email || "";
  buildOpts(d);
  $("tier").title = SEL || d.cwd || "";
  wins.forEach(w=>{ if(w.key==="five_hour")setRow("5",w); if(w.key==="seven_day")setRow("7",w); });
  const cx=curContext(d);
  if(cx.pct!=null){
    const p=cx.pct, col=bandColor(p);
    $("bc").style.width=Math.min(100,p)+"%"; $("bc").style.background=col;
    $("pcc").textContent=Math.round(p)+"%"; $("pcc").style.color=col;
    $("cdc").textContent=cx.tok?fmtTok(cx.tok):"";
  } else { $("bc").style.width="0%"; $("pcc").textContent="–"; $("pcc").style.color=""; $("cdc").textContent=""; }
  const sv=statusView(d), ws=$("wstatus");
  if(ws){ if(sv){ ws.style.display="inline-flex"; ws.style.color=sv.color;
      ws.innerHTML="<span class='sdot2' style='background:"+sv.color+"'></span>"+sv.word; } else ws.style.display="none"; }
  tick();
}catch(e){ if(KIND!=="bar"){ $("dot").style.background="#cda24e"; } } }
// Resize is handled natively by the OS (WS_THICKFRAME) — no JS resize math (DPI-safe).
$("tier").addEventListener("change",function(){ SEL=this.value; try{localStorage.setItem("trackSel",SEL);}catch(_){} refresh(); });
// Don't let pywebview's easy_drag move the window when interacting with a control
// (its drag listener is on window/bubble — stop the event before it reaches it).
document.addEventListener("mousedown",function(e){
  if(e.target.closest("select,button,input,a,.x,.ic")) e.stopPropagation();
},false);
setInterval(tick,1000); setInterval(refresh,5000); refresh();
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    timeout = 10  # drop slow/hung local clients

    def log_message(self, *args):
        pass  # silence default stderr logging

    def _send(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n <= 0 or n > 100_000:
                return None
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", DASHBOARD_HTML.encode("utf-8"))
        elif path == "/widget":
            self._send(200, "text/html; charset=utf-8", WIDGET_HTML.encode("utf-8"))
        elif path == "/api/usage":
            with STORE_LOCK:
                snap = STORE.get("snapshot", {})
            self._send(200, "application/json", json.dumps(snap).encode("utf-8"))
        elif path == "/api/pair-qr":
            png = remote_pair_qr_png(load_config())
            if png:
                self._send(200, "image/png", png)
            else:
                self._send(404, "text/plain", b"pairing unavailable")
        elif path == "/api/team/whoami":
            self._send(200, "application/json", json.dumps({
                "signed_in": supabase_pool.signed_in(), "email": supabase_pool.email(),
                "team": supabase_pool.team(), "is_admin": supabase_pool.is_admin()}).encode("utf-8"))
        elif path == "/api/team/overview":
            if not supabase_pool.signed_in():
                self._send(200, "application/json", b'{"error":"not_signed_in"}')
            else:
                merged = supabase_team_overview()
                out = merged if merged else {"error": "supabase unreachable"}
                self._send(200, "application/json", json.dumps(out).encode("utf-8"))
        elif path == "/api/team/ledger":
            from urllib.parse import parse_qs, urlsplit
            month = (parse_qs(urlsplit(self.path).query).get("month") or [""])[0]
            if not supabase_pool.signed_in():
                self._send(200, "application/json", b'{"error":"not_signed_in"}')
            elif not (len(month) == 7 and month[:4].isdigit() and month[4] == "-" and month[5:].isdigit()):
                self._send(200, "application/json", b'{"error":"bad_month"}')
            else:
                led = supabase_pool.read_ledger(month)
                led["computed_spend"] = team_ledger_computed(led, supabase_pool.read_ledger(_prev_month(month)))
                led["month_tokens"] = {acct: member_month_tokens(led, acct)
                                       for acct in (led.get("accounts") or {})}
                self._send(200, "application/json", json.dumps(led).encode("utf-8"))
        elif path == "/favicon.ico":
            self._send(204, "image/x-icon", b"")
        else:
            self._send(404, "text/plain", b"not found")

    def _origin_ok(self) -> bool:
        """Defeat web-based CSRF / DNS-rebinding against the local control plane. A genuine caller
        is either the dashboard's own same-origin fetch (Origin = our loopback host) or a
        non-browser client (urllib from the overlays / the session hook), which sends no Origin.
        A website the user visits sends Origin: https://evil.com, and a rebinding attack sends a
        foreign Host — both are rejected."""
        from urllib.parse import urlsplit
        local = ("127.0.0.1", "localhost", "::1")
        for hdr in ("Origin", "Referer"):
            v = self.headers.get(hdr)
            if v and urlsplit(v).hostname not in local:
                return False
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return not (host and host not in local)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        # These endpoints are state-changing (config, relay, update, sign-in). The socket is bound
        # to 127.0.0.1, but a web page the user visits could still POST here (the body is parsed
        # regardless of Content-Type), so also require a loopback Origin/Host — see _origin_ok.
        if self.client_address[0] not in ("127.0.0.1", "::1") or not self._origin_ok():
            self._send(403, "text/plain", b"forbidden")
            return
        if path == "/api/login":
            body = self._read_json() or {}
            email = (body.get("email") or None)
            visible = bool(body.get("terminal"))
            threading.Thread(target=launch_login, kwargs={"email": email, "visible": visible},
                             daemon=True).start()
            self._send(200, "application/json", b'{"ok":true}')
        elif path in ("/api/refresh", "/api/update"):
            fn = CONTROL.get("refresh" if path == "/api/refresh" else "update")
            if fn:
                try:
                    fn()
                except Exception:
                    pass
            self._send(200, "application/json", b'{"ok":true}')
        elif path == "/api/check-update":
            fn = CONTROL.get("check_update")
            try:
                res = fn() if fn else {"update": False, "current": __version__, "latest": None}
            except Exception:
                res = {"update": False, "current": __version__, "latest": None}
            self._send(200, "application/json", json.dumps(res).encode("utf-8"))
        elif path == "/api/config":
            patch = self._read_json()
            fn = CONTROL.get("set_config")
            if fn and isinstance(patch, dict):
                try:
                    fn(patch)
                except Exception:
                    pass
            self._send(200, "application/json", b'{"ok":true}')
        elif path == "/api/overlay":
            oid = (self._read_json() or {}).get("id", "")
            fn = CONTROL.get("toggle_overlay")
            if fn and oid in ("widget", "bar"):
                try:
                    fn(oid)
                except Exception:
                    pass
            self._send(200, "application/json", b'{"ok":true}')
        elif path == "/api/remote":
            action = (self._read_json() or {}).get("action", "")
            fn = CONTROL.get("remote_action")
            if fn and action in ("rotate", "unpair", "sync"):
                try:
                    fn(action)
                except Exception:
                    pass
            self._send(200, "application/json", b'{"ok":true}')
        elif path == "/api/session-waiting":
            fn = CONTROL.get("session_waiting")
            if fn:
                try:
                    fn(self._read_json() or {})
                except Exception:
                    pass
            self._send(200, "application/json", b'{"ok":true}')
        elif path == "/api/team":
            self._send(200, "application/json", json.dumps(self._team_action()).encode("utf-8"))
        else:
            self._send(404, "text/plain", b"not found")

    def _team_action(self) -> dict:
        """POST /api/team {action, ...} -- Supabase account-pool session control: login (email
        OTP), logout, set-username, and the admin connector-token mint. Runs only on this
        loopback control plane (already origin/host-guarded); the user JWT never enters the DOM."""
        body = self._read_json() or {}
        action = body.get("action", "")
        try:
            if action == "login-start":
                email = (body.get("email") or "").strip().lower()
                if not email:
                    return {"ok": False, "error": "email required"}
                ok, resp = supabase_pool.sign_in_start(email)
                return {"ok": True} if ok else {"ok": False, "error": f"could not send code: {resp}"}
            elif action == "login-verify":
                email = (body.get("email") or "").strip().lower()
                code = (body.get("code") or "").strip()
                username = (body.get("username") or "").strip()
                if not (email and code):
                    return {"ok": False, "error": "email and code required"}
                ok, _res = supabase_pool.sign_in_verify_code(email, code)
                if not ok:
                    return {"ok": False, "error": "invalid or expired code"}
                if username:
                    supabase_pool.set_username(username)
                self._team_changed()
                return {"ok": True, "team": supabase_pool.team(), "is_admin": supabase_pool.is_admin()}
            elif action == "set-username":
                username = (body.get("username") or "").strip()
                if not username:
                    return {"ok": False, "error": "username required"}
                return ({"ok": True} if supabase_pool.set_username(username)
                        else {"ok": False, "error": "not signed in"})
            elif action == "logout":
                supabase_pool.sign_out()
                self._team_changed()
                return {"ok": True}
            elif action == "connector-token":
                if not supabase_pool.is_admin():
                    return {"ok": False, "error": "admin only"}
                tok, err = supabase_pool.mint_connector_token(int(body.get("days") or 30))
                return {"ok": True, "token": tok} if tok else {"ok": False, "error": err or "mint failed"}
            else:
                return {"ok": False, "error": "unknown action"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _team_changed(self) -> None:
        """Nudge the poll loop after a team-session change (login/logout) so it re-reads state."""
        fn = CONTROL.get("team_changed")
        if fn:
            try:
                fn()
            except Exception:
                pass


def start_server(port: int) -> tuple[ThreadingHTTPServer | None, int]:
    for p in range(port, port + 10):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), DashboardHandler)
            httpd.daemon_threads = True
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            log(f"dashboard server on http://127.0.0.1:{p}/")
            return httpd, p
        except OSError:
            continue
    log("could not bind a dashboard port")
    return None, port


# ---------------------------------------------------------------------------
# Native window (optional, separate process) + browser fallback
# ---------------------------------------------------------------------------

def webview_available() -> bool:
    return importlib.util.find_spec("webview") is not None


def open_dashboard(port: int, prefer_window: bool) -> None:
    url = f"http://127.0.0.1:{port}/"
    if prefer_window and webview_available():
        try:
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--window", "--port", str(port)]
            else:
                cmd = [_pythonw(), str(Path(__file__).resolve()), "--window", "--port", str(port)]
            subprocess.Popen(cmd, close_fds=True)
            return
        except Exception as exc:
            log(f"window spawn failed, using browser: {exc}")
    webbrowser.open(url)


def set_app_user_model_id(appid: str = "ClaudeUsageTracker.App") -> None:
    """Give the process a distinct taskbar identity, so Windows uses the window's
    own icon for the taskbar button instead of falling back to pythonw.exe's."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)
    except Exception:
        pass


def set_window_icon(ico_path, toolwindow=False) -> bool:
    """Set this process's top-level window icon (titlebar + taskbar). When
    toolwindow=True, also flag the window WS_EX_TOOLWINDOW so it has no taskbar
    button / Alt-Tab entry (right for an always-on-top widget). Returns True
    once a visible top-level window was found and updated."""
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        # Correct restypes — handles/styles are pointer-sized; default c_int truncates on 64-bit.
        u32.LoadImageW.restype = ctypes.c_void_p
        u32.LoadImageW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_uint,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        u32.SendMessageW.restype = ctypes.c_void_p
        u32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        setclass = getattr(u32, "SetClassLongPtrW", None) or u32.SetClassLongW
        setclass.restype = ctypes.c_void_p
        setclass.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        getl = getattr(u32, "GetWindowLongPtrW", None) or u32.GetWindowLongW
        setl = getattr(u32, "SetWindowLongPtrW", None) or u32.SetWindowLongW
        getl.restype = ctypes.c_ssize_t
        getl.argtypes = [wintypes.HWND, ctypes.c_int]
        setl.restype = ctypes.c_ssize_t
        setl.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]

        IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
        big = u32.LoadImageW(None, str(ico_path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
        small = u32.LoadImageW(None, str(ico_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)

        pid = k32.GetCurrentProcessId()
        WM_SETICON, GW_OWNER, GCLP_HICON, GCLP_HICONSM = 0x80, 4, -14, -34
        GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
        GWL_STYLE, WS_THICKFRAME = -16, 0x00040000
        SWP_FRAMECHANGED = 0x0001 | 0x0002 | 0x0004 | 0x0020   # NOSIZE|NOMOVE|NOZORDER|FRAMECHANGED
        found = []
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _):
            wpid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid and u32.IsWindowVisible(hwnd) and not u32.GetWindow(hwnd, GW_OWNER):
                if big:
                    u32.SendMessageW(hwnd, WM_SETICON, 1, big)    # ICON_BIG
                    setclass(hwnd, GCLP_HICON, big)
                if small:
                    u32.SendMessageW(hwnd, WM_SETICON, 0, small)  # ICON_SMALL
                    setclass(hwnd, GCLP_HICONSM, small)
                if toolwindow:
                    ex = getl(hwnd, GWL_EXSTYLE)
                    setl(hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
                    st = getl(hwnd, GWL_STYLE)
                    setl(hwnd, GWL_STYLE, st | WS_THICKFRAME)   # native, DPI-correct edge resize
                    u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FRAMECHANGED)
                    u32.ShowWindow(hwnd, 0)   # SW_HIDE — required for the taskbar to drop it
                    u32.ShowWindow(hwnd, 5)   # SW_SHOW
                found.append(hwnd)
            return True

        u32.EnumWindows(proto(cb), 0)
        if found:
            log(f"window icon set on {len(found)} window(s)" + (" (tool window)" if toolwindow else ""))
            return True
        return False
    except Exception as exc:
        log(f"set window icon failed: {exc}")
        return False


def _apply_window_icon(toolwindow=False) -> None:
    for _ in range(25):               # the webview window can take a moment to appear
        time.sleep(0.4)
        if set_window_icon(ICO_PATH, toolwindow=toolwindow):
            return
    log("window icon: no top-level window found")


def run_window(port: int) -> int:
    global _instance_guard
    url = f"http://127.0.0.1:{port}/"
    try:
        _instance_guard = bind_guard(49223)   # one window at a time
    except OSError:
        return 0
    try:
        import webview
        ensure_app_icon()
        set_app_user_model_id()
        webview.create_window(APP_NAME, url, width=820, height=640,
                              min_size=(300, 360), background_color="#070a10")
        threading.Thread(target=_apply_window_icon, daemon=True).start()
        webview.start(icon=str(ICO_PATH))
    except Exception as exc:
        log(f"window mode failed, opening browser: {exc}")
        webbrowser.open(url)
    return 0


def run_overlay(port: int, kind: str = "panel") -> int:
    """Frameless always-on-top overlay. kind='panel' = the mini widget; kind='bar' =
    the minimal one-line HUD (translucent, FPS-counter style). Both render the same
    /api/usage data; the kind only changes the layout (CSS class) and the window chrome."""
    global _instance_guard
    cfg = load_config()
    bar = (kind == "bar")
    url = f"http://127.0.0.1:{port}/widget" + ("?kind=bar" if bar else "")
    try:
        _instance_guard = bind_guard(49225 if bar else 49224)   # one of each kind at a time
    except OSError:
        return 0
    try:
        import webview
        ensure_app_icon()
        set_app_user_model_id()

        if bar:
            w, h, minsz = max(int(cfg.get("bar_width", 360)), 200), max(int(cfg.get("bar_height", 40)), 30), (200, 30)
        else:
            w, h, minsz = max(int(cfg.get("widget_width", 392)), 280), max(int(cfg.get("widget_height", 216)), 150), (280, 150)
        pos = {}
        try:    # top-right corner with a small margin
            import ctypes
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            pos = {"x": max(0, sw - w - 24), "y": 28}
        except Exception:
            pass

        class Api:
            def close(self):
                for win in list(webview.windows):
                    try:
                        win.destroy()
                    except Exception:
                        pass

            def save_size(self, w, h):  # remember size via the tray (single config writer)
                kw, kh = ("bar_width", "bar_height") if bar else ("widget_width", "widget_height")
                lo = (200, 30) if bar else (280, 150)
                patch = {kw: max(lo[0], int(w)), kh: max(lo[1], int(h))}
                try:
                    urllib.request.urlopen(urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/config",
                        data=json.dumps(patch).encode("utf-8"), method="POST",
                        headers={"Content-Type": "application/json"}), timeout=3).read()
                except Exception:
                    try:
                        c = load_config(); c.update(patch); save_json(CONFIG_PATH, c)
                    except Exception:
                        pass

        api = Api()
        kw = dict(width=w, height=h, resizable=True, min_size=minsz,
                  frameless=True, easy_drag=True, on_top=True, js_api=api, **pos)
        kw["background_color"] = "#0e0e10" if bar else "#141416"   # solid, opaque (no grey bleed)
        window = webview.create_window(APP_NAME, url, **kw)
        # native resize (WS_THICKFRAME) doesn't call JS — persist the size from the resized event
        _rt = {"t": None}
        def _on_resized(*_a):
            try:
                if _rt["t"]:
                    _rt["t"].cancel()
            except Exception:
                pass
            _rt["t"] = threading.Timer(0.7, lambda: api.save_size(window.width, window.height))
            _rt["t"].daemon = True
            _rt["t"].start()
        try:
            window.events.resized += _on_resized
        except Exception:
            pass
        threading.Thread(target=_apply_window_icon, args=(True,), daemon=True).start()
        webview.start(icon=str(ICO_PATH))
    except Exception as exc:
        log(f"overlay ({kind}) failed: {exc}")
    return 0


# ---------------------------------------------------------------------------
# Autostart + shortcuts (Startup folder via PowerShell)
# ---------------------------------------------------------------------------

def _pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def _launch_target():
    """(target, args) for shortcuts: prefer the installed no-console gui launcher
    (pipx/pip exposes 'claude-usage-tracker-gui.exe'); else the dev venv's
    pythonw + this script."""
    import shutil
    gui = shutil.which("claude-usage-tracker-gui")
    if gui and gui.lower().endswith(".exe"):
        return gui, ""
    return _pythonw(), f'"{Path(__file__).resolve()}"'


def _is_installed_pkg() -> bool:
    """True if running as an installed package (pipx/pip venv → site-packages), not a
    source checkout. Source checkouts are git-managed and can't be pip-upgraded."""
    if getattr(sys, "frozen", False):
        return False
    return "/site-packages/" in str(Path(__file__).resolve()).replace("\\", "/").lower()


def _find_pipx():
    import shutil
    p = shutil.which("pipx")
    if p:
        return p
    for c in (Path.home() / ".local" / "bin" / "pipx.exe", Path.home() / ".local" / "bin" / "pipx"):
        if c.exists():
            return str(c)
    return None


def _claude_cli():
    """Locate the Claude Code CLI (for the sign-in flow) — PATH first, then the
    default install dir. Returns the path or None."""
    import shutil
    p = shutil.which("claude")
    if p:
        return p
    for cand in (Path.home() / ".local" / "bin" / "claude.exe",
                 Path.home() / ".local" / "bin" / "claude"):
        if cand.exists():
            return str(cand)
    return None


# Address-safe characters for a sign-in email. Anything else (quotes, spaces, &, |, <, >, ^…)
# is rejected so a value from the localhost /api/login endpoint can't inject shell metacharacters
# into the visible `cmd /k` launch path in launch_login().
_EMAIL_SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@._%+-")


def _safe_login_email(email: str | None) -> str | None:
    """An address-safe email, or None. Rejects anything containing shell metacharacters so a
    value from the localhost /api/login endpoint can't inject into the visible `cmd /k` path."""
    if email and not (set(email) - _EMAIL_SAFE_CHARS) and "@" in email:
        return email
    return None


def launch_login(email: str | None = None, visible: bool = False) -> bool:
    """Trigger Claude Code's own sign-in (`claude auth login`), which opens claude.ai in
    your browser. By default we launch it WITHOUT a console window (you only see the
    browser); pass visible=True for a terminal (the fallback). `email` pre-fills the
    login page. We never write the token ourselves — Claude Code owns
    ~/.claude/.credentials.json; the next poll picks up the refreshed login."""
    exe = _claude_cli()
    if not exe:
        notify("Claude Code not found",
               "Install Claude Code, then sign in — or run `claude auth login` in a terminal.")
        return False
    email = _safe_login_email(email)      # injection guard (the visible path shells out via cmd /k)
    args = [exe, "auth", "login"] + (["--email", email] if email else [])
    try:
        if os.name == "nt" and visible:
            CREATE_NEW_CONSOLE = 0x00000010
            cmd = "cmd /k " + " ".join(f'"{a}"' for a in args)   # keep quoted exe + args intact
            subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE, close_fds=True)
            notify(APP_NAME, "Opening Claude sign-in — complete it in the terminal window.")
        elif os.name == "nt":
            CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            subprocess.Popen(args, creationflags=CREATE_NO_WINDOW, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            notify(APP_NAME, "Opening Claude sign-in in your browser…")
        else:
            subprocess.Popen(args, close_fds=True)
            notify(APP_NAME, "Opening Claude sign-in in your browser…")
        return True
    except Exception as exc:
        log(f"login launch failed: {exc}")
        notify("Couldn't open sign-in", "Run `claude auth login` in a terminal.")
        return False


def _ps_q(s) -> str:
    return str(s).replace("'", "''")   # escape single quotes for PowerShell literals


def _make_shortcuts(target, args, locations) -> None:
    folders = "@(" + ",".join(f"[Environment]::GetFolderPath('{loc}')" for loc in locations) + ")"
    icon = str(ICO_PATH) if ICO_PATH.exists() else target
    ps = (
        "$ws=New-Object -ComObject WScript.Shell;"
        f"foreach($d in {folders}){{"
        f"$p=Join-Path $d '{_ps_q(APP_NAME)}.lnk';"
        "$s=$ws.CreateShortcut($p);"
        f"$s.TargetPath='{_ps_q(target)}';$s.Arguments='{_ps_q(args)}';"
        f"$s.WorkingDirectory='{_ps_q(Path(target).parent)}';$s.IconLocation='{_ps_q(icon)}';"
        f"$s.Description='{_ps_q(APP_NAME)}';$s.Save()}}"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], check=True)


def _remove_shortcuts() -> None:
    ps = (
        "foreach($d in @([Environment]::GetFolderPath('Desktop'),"
        "[Environment]::GetFolderPath('Programs'),[Environment]::GetFolderPath('Startup'))){"
        f"$p=Join-Path $d '{_ps_q(APP_NAME)}.lnk'; if(Test-Path $p){{Remove-Item $p -Force; \"removed $p\"}}}}"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])


def _ask(question, default=True) -> bool:
    suffix = "Y/n" if default else "y/N"
    try:
        a = input(f"  {question} [{suffix}] ").strip().lower()
    except EOFError:
        return default
    return default if not a else a.startswith("y")


def do_install() -> None:
    ensure_app_icon()
    target, args = _launch_target()
    print(f"\n{APP_NAME} setup")
    print(f"  launcher: {target}\n")
    locs = []
    if _ask("Add a Desktop shortcut?"):
        locs.append("Desktop")
    if _ask("Add to the Start Menu?"):
        locs.append("Programs")
    if _ask("Start automatically when you log in?"):
        locs.append("Startup")
    if locs:
        _make_shortcuts(target, args, locs)
        print(f"\n  shortcuts created: {', '.join(locs)}")
    else:
        print("\n  no shortcuts created.")
    print("\n  Note: Windows does not let an app pin itself to the taskbar.")
    print("  To pin it: launch the app, then right-click its taskbar icon -> Pin to taskbar.")
    if _ask("\n  Launch it now?", default=True):
        try:
            cmd = [target] + ([args.strip('"')] if args.strip() else [])
            subprocess.Popen(cmd, close_fds=True)
            print("  launched — look for the tray icon (by the clock) and the mini widget (top-right).")
        except Exception as exc:
            print(f"  launch failed: {exc}")
    print()


def do_uninstall() -> None:
    _remove_shortcuts()
    print("Removed Desktop / Start Menu / Startup shortcuts.")


def install_autostart() -> None:   # non-interactive: Startup shortcut only
    ensure_app_icon()
    target, args = _launch_target()
    _make_shortcuts(target, args, ["Startup"])
    print("Autostart installed (Startup shortcut).")


def uninstall_autostart() -> None:
    do_uninstall()


# ---------------------------------------------------------------------------
# Tray application
# ---------------------------------------------------------------------------

class TrayApp:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.state = load_json(STATE_PATH, {})
        self.history = load_history()
        self.last = FetchResult(False, error="starting…")
        self.icon = None
        self.port = int(cfg.get("dashboard_port", 8787))
        self.widget_proc = None
        self.bar_proc = None
        self._config_epoch = 0
        self._update_available = None
        self._update_url = None
        self._verdict = ""
        self._job = None
        self._children = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._started_notified = False
        self._remote = RemoteSync()       # owns relay sync/command/push + their shared state
        self._team = TeamSync()           # owns the opt-in team report/escrow pushes
        self._session_waiting_last = {}   # session_id -> last-notified ts (rate-limit)

    def build_menu(self):
        import pystray
        return pystray.Menu(
            pystray.MenuItem(lambda i: f"⬆ Update to v{self._update_available}", self._on_update,
                             visible=lambda i: bool(self._update_available)),
            pystray.MenuItem(lambda i: "Hide widget" if self._widget_alive() else "Show widget",
                             self._on_toggle_widget),
            pystray.MenuItem(lambda i: "Hide minimal bar" if self._bar_alive() else "Show minimal bar",
                             self._on_toggle_bar),
            pystray.MenuItem("Open dashboard", self._on_open, default=True),
            pystray.MenuItem("Open in browser", self._on_browser),
            pystray.MenuItem("Refresh now", self._on_refresh),
            pystray.MenuItem("Check for updates", self._on_check_update),
            pystray.MenuItem("Sign in to Claude…", self._on_login),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open config file", self._on_config),
            pystray.MenuItem("Open log file", self._on_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )

    def run(self):
        import pystray
        ensure_app_icon()
        httpd, self.port = start_server(self.port)
        try:
            PORT_PATH.write_text(str(self.port), encoding="utf-8")   # so --session-hook can find us
        except Exception:
            pass
        # Preload the last-known snapshot so the UI shows data immediately, even if
        # the first poll is rate-limited (marked stale until the first good poll).
        prev = load_json(SNAPSHOT_PATH, None)
        if isinstance(prev, dict) and prev.get("windows"):
            prev["ok"] = False
            prev["error"] = None
            with STORE_LOCK:
                STORE["snapshot"] = prev
        self.icon = pystray.Icon(APP_NAME, icon=make_icon_image({}),
                                 title=f"{APP_NAME}\nstarting…", menu=self.build_menu())
        self._job = make_kill_on_close_job()
        CONTROL["refresh"] = self._wake.set            # let the dashboard/widget re-poll
        CONTROL["check_update"] = self._check_update_now
        CONTROL["update"] = lambda: threading.Thread(target=self._do_update, daemon=True).start()
        CONTROL["set_config"] = self._set_config       # the tray is the single config writer
        CONTROL["toggle_overlay"] = self._toggle_overlay
        CONTROL["remote_action"] = self._remote_action
        CONTROL["session_waiting"] = self._on_session_waiting
        CONTROL["team_changed"] = lambda: (self._team.reset_throttle(), self._wake.set())  # report right after join/create
        global REMOTE_PUSH
        REMOTE_PUSH = lambda title, msg: self._remote.push(self.cfg, title, msg)  # mirror toasts to the phone
        threading.Thread(target=self._poll_loop, daemon=True).start()
        if self.cfg.get("show_widget_on_start", True):
            self.widget_proc = self._spawn_mode("--widget")
        if self.cfg.get("show_bar_on_start", False):
            self.bar_proc = self._spawn_mode("--bar")
        self.icon.run()

    # ----- child window/widget process control -----
    def _spawn_mode(self, mode: str):
        try:
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, mode, "--port", str(self.port)]
            else:
                cmd = [_pythonw(), str(Path(__file__).resolve()), mode, "--port", str(self.port)]
            p = subprocess.Popen(cmd, close_fds=True)
            assign_to_job(self._job, p)        # OS kills it if we ever die
            self._children = [c for c in self._children if c.poll() is None]
            self._children.append(p)
            return p
        except Exception as exc:
            log(f"spawn {mode} failed: {exc}")
            return None

    def _widget_alive(self) -> bool:
        return self.widget_proc is not None and self.widget_proc.poll() is None

    def _on_toggle_widget(self, icon, item):
        if self._widget_alive():
            try:
                self.widget_proc.terminate()
            except Exception:
                pass
            self.widget_proc = None        # reflect "hidden" immediately (poll() lags terminate)
        else:
            self.widget_proc = self._spawn_mode("--widget")
        self._refresh_menu(icon)

    def _bar_alive(self) -> bool:
        return self.bar_proc is not None and self.bar_proc.poll() is None

    def _on_toggle_bar(self, icon, item):
        if self._bar_alive():
            try:
                self.bar_proc.terminate()
            except Exception:
                pass
            self.bar_proc = None
        else:
            self.bar_proc = self._spawn_mode("--bar")
        self._refresh_menu(icon)

    def _refresh_menu(self, icon=None):
        try:
            (icon or self.icon).update_menu()    # re-evaluate dynamic item labels now
        except Exception:
            pass

    def _toggle_overlay(self, oid):
        (self._on_toggle_bar if oid == "bar" else self._on_toggle_widget)(None, None)
        try:
            if self.icon:
                self.icon.update_menu()
        except Exception:
            pass

    def _set_config(self, patch: dict):
        """Single config writer: merge an allowlisted patch into the live cfg, persist
        once, and bump the epoch so overlays know to re-read."""
        if not isinstance(patch, dict):
            return
        with CONFIG_LOCK:
            for k, v in patch.items():
                if k in CONFIG_ALLOW:
                    self.cfg[k] = v
            try:
                save_json(CONFIG_PATH, self.cfg)
            except Exception as exc:
                log(f"config save failed: {exc}")
            self._config_epoch += 1
        self._wake.set()   # re-poll now so the snapshot (remote/ui) reflects the change quickly
        if "notify_session_waiting" in patch:   # toggle installs/removes the Claude Code idle hook
            target = install_session_hook if patch["notify_session_waiting"] else remove_session_hook
            threading.Thread(target=target, daemon=True).start()

    # ----- remote sync (optional, opt-in) -----
    def _on_session_waiting(self, payload: dict) -> None:
        """A Claude Code session finished its turn and is awaiting the user (Stop hook, via
        --session-hook). Fire a toast (the REMOTE_PUSH hook also mirrors it to the phone),
        lightly de-duped per session so a single turn can't double-fire."""
        if not self.cfg.get("notify_session_waiting", False):
            return
        payload = payload or {}
        if payload.get("stop_hook_active"):     # a stop-hook-induced continuation, not a real wait
            return
        cwd = payload.get("cwd") or ""
        sid = payload.get("session_id") or cwd or "session"
        name = project_name(cwd, "session")
        now = time.time()
        if now - self._session_waiting_last.get(sid, 0) < 10:   # collapse rapid duplicates only
            return
        self._session_waiting_last[sid] = now
        notify("Claude is waiting", f"{name} · finished — your turn to respond")

    def _remote_action(self, action: str) -> None:
        """Driven by POST /api/remote: rotate the key, unpair, or force a sync."""
        if action == "rotate":
            rotate_remote_identity()
            self._config_epoch += 1
        elif action == "unpair":
            unpair_remote()
            self._set_config({"remote_enabled": False})
        elif action == "sync":
            with STORE_LOCK:
                snap = dict(STORE.get("snapshot", {}))
            self._remote.reset_throttle()
            if snap:
                threading.Thread(target=self._remote.sync, args=(snap, self.cfg), daemon=True).start()

    # menu handlers
    def _on_open(self, icon, item):
        if self.cfg.get("open_as_window", True) and webview_available():
            if self._spawn_mode("--window"):
                return
        webbrowser.open(f"http://127.0.0.1:{self.port}/")

    def _on_browser(self, icon, item):
        webbrowser.open(f"http://127.0.0.1:{self.port}/")

    def _on_update(self, icon, item):
        threading.Thread(target=self._do_update, daemon=True).start()

    def _do_update(self):
        url = self._update_url
        if getattr(sys, "frozen", False) and url:
            try:
                notify(APP_NAME, f"Downloading v{self._update_available}…")
                dst = os.path.join(os.environ.get("TEMP", str(APP_DIR)), "ClaudeUsageTracker-Setup.exe")
                req = urllib.request.Request(url, headers={"User-Agent": "claude-usage-tracker"})
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = r.read()
                with open(dst, "wb") as f:
                    f.write(data)
                # Silent installer: closes this app, upgrades in place, relaunches it.
                subprocess.Popen([dst, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], close_fds=True)
                time.sleep(1)
                self._on_quit(self.icon, None)
            except Exception as exc:
                log(f"self-update failed: {exc}")
                notify("Update failed", "Opening the download page…")
                webbrowser.open(RELEASES_URL)
        else:
            self._pip_self_update()

    def _pip_self_update(self):
        """Upgrade an installed (pipx/pip) copy in place using our OWN interpreter —
        no reliance on a `pipx` command being on PATH — then relaunch. Source checkouts
        are git-managed, so we just point those at the releases page."""
        NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if not _is_installed_pkg():
            # Source checkout: update in place with `git pull` if it's a git repo.
            here = Path(__file__).resolve().parent
            if (here / ".git").exists():
                notify(APP_NAME, "Updating (git pull)…")
                try:
                    r = subprocess.run(["git", "-C", str(here), "pull", "--ff-only"],
                                       capture_output=True, text=True, timeout=120, creationflags=NO_WIN)
                    out = (r.stdout + r.stderr).lower()
                    if r.returncode == 0 and "up to date" not in out:
                        notify(APP_NAME, "Updated — restarting…")
                        self._relaunch_detached()
                        self._on_quit(self.icon, None)
                        return
                    if r.returncode == 0:
                        notify(APP_NAME, "Already up to date.")
                        return
                    log("git pull failed:\n" + r.stdout + r.stderr)
                except Exception as exc:
                    log(f"git pull error: {exc}")
            webbrowser.open(RELEASES_URL)
            notify("Update", "Couldn't auto-update this checkout — `git pull` in a terminal.")
            return
        notify(APP_NAME, f"Updating to v{self._update_available}…")
        try:
            # sys.executable is the app's venv python (works for pipx and pip installs).
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "claude-usage-tracker"],
                               capture_output=True, text=True, timeout=300, creationflags=NO_WIN)
            ok = r.returncode == 0
            if not ok:                              # fall back to a pipx binary if we can find one
                pipx = _find_pipx()
                if pipx:
                    r = subprocess.run([pipx, "upgrade", "claude-usage-tracker"],
                                       capture_output=True, text=True, timeout=300, creationflags=NO_WIN)
                    ok = r.returncode == 0
            if ok:
                notify(APP_NAME, f"Updated to v{self._update_available} — restarting…")
                self._relaunch_detached()
                self._on_quit(self.icon, None)
            else:
                log("self-update failed:\n" + (r.stdout or "") + "\n" + (r.stderr or ""))
                webbrowser.open(RELEASES_URL)
                notify("Update failed", "Couldn't upgrade automatically — opened the releases page.")
        except Exception as exc:
            log(f"self-update error: {exc}")
            webbrowser.open(RELEASES_URL)
            notify("Update failed", "Couldn't upgrade automatically — opened the releases page.")

    def _relaunch_detached(self, delay=2):
        """Start a fresh instance after this one releases the single-instance port."""
        try:
            target, args = _launch_target()
            cmd = f'ping 127.0.0.1 -n {delay + 1} >nul & start "" "{target}" {args}'
            subprocess.Popen(cmd, shell=True, close_fds=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:
            log(f"relaunch failed: {exc}")

    def _on_refresh(self, icon, item):
        self._wake.set()

    def _check_update_now(self) -> dict:
        """Query GitHub for the latest release now; update state if newer. Returns
        {current, latest, url, update}."""
        latest, dl = check_github_latest()
        res = {"current": __version__, "latest": latest, "url": dl, "update": False}
        if latest and _vtuple(latest) > _vtuple(__version__):
            self._update_available, self._update_url = latest, dl
            res["update"] = True
        return res

    def _on_check_update(self, icon, item):
        threading.Thread(target=self._do_check_update, daemon=True).start()

    def _do_check_update(self):
        res = self._check_update_now()
        if res["update"]:
            notify("Update available", f"v{res['latest']} — open the tray menu → Update")
        elif res["latest"]:
            notify(APP_NAME, f"You're on the latest version (v{__version__}).")
        else:
            notify(APP_NAME, "Couldn't reach GitHub to check for updates.")
        try:
            if self.icon:
                self.icon.update_menu()      # surface the Update item if one appeared
        except Exception:
            pass

    def _on_login(self, icon, item):
        threading.Thread(target=launch_login, daemon=True).start()

    def _on_config(self, icon, item):
        load_config()
        os.startfile(str(CONFIG_PATH))  # noqa: S606

    def _on_log(self, icon, item):
        if not LOG_PATH.exists():
            LOG_PATH.write_text("", encoding="utf-8")
        os.startfile(str(LOG_PATH))  # noqa: S606

    def _on_quit(self, icon, item):
        self._stop.set()
        self._wake.set()
        for p in self._children:        # job object is the backstop; this is the clean path
            try:
                if p.poll() is None:
                    p.terminate()
            except Exception:
                pass
        icon.stop()

    def _refresh_visual(self, r: FetchResult):
        if not self.icon:
            return
        try:
            if r.ok:
                self.icon.icon = make_icon_image(r.windows)
                head = f"{APP_NAME} · {self._verdict}" if self._verdict else APP_NAME
                self.icon.title = f"{head}\n{status_line(r.windows)}"[:127]
            elif r.token_state in (TokenState.EXPIRED, TokenState.MISSING):
                self.icon.icon = make_icon_image({}, error=True)
                self.icon.title = f"{APP_NAME}\n{r.error}"[:127]
            else:
                # transient (429/network): keep last-good icon, note retry in tooltip
                self.icon.title = f"{APP_NAME}\n{r.error} — retrying"[:127]
        except Exception as exc:
            log(f"visual update failed: {exc}")

    def _refresh_team_overview(self):
        """Admin: fetch + compact the pool overview for the phone-embedded snapshot.
        Runs off the poll thread; failures leave the last good value in place."""
        try:
            merged = supabase_team_overview()
            if merged:
                self._team_overview = team_overview_compact(merged)
        except Exception:
            log("team overview embed error:\n" + traceback.format_exc())

    def _poll_loop(self):
        timeout = int(self.cfg.get("request_timeout_seconds", 20))
        cap = int(self.cfg.get("history_cap", 2880))
        ui_iv = int(self.cfg.get("ui_refresh_seconds", 15))
        stale_secs = int(self.cfg.get("statusline_stale_seconds", 300))
        extras_iv = int(self.cfg.get("api_extras_interval_seconds", 1800))
        fallback_iv = int(self.cfg.get("api_fallback_interval_seconds", 300))
        self._extra = getattr(self, "_extra", None)
        self._sessions = getattr(self, "_sessions", [])
        self._sessions_cache = getattr(self, "_sessions_cache", {})
        self._alltime = getattr(self, "_alltime", {})
        self._alltime_cache = getattr(self, "_alltime_cache", load_json(ALLTIME_PATH, {}))
        self._context = getattr(self, "_context", None)
        self._cwd = getattr(self, "_cwd", None)
        self._verdict = getattr(self, "_verdict", "")
        self._update_available = getattr(self, "_update_available", None)
        self._update_url = getattr(self, "_update_url", None)
        self._status = getattr(self, "_status", None)
        self._team_overview = getattr(self, "_team_overview", None)   # compact team KPIs embedded for the admin's phone
        status_iv = int(self.cfg.get("status_interval_seconds", 300))
        sessions_iv = int(self.cfg.get("sessions_interval_seconds", 45))
        sessions_top = int(self.cfg.get("sessions_top_n", 8))
        alltime_iv = int(self.cfg.get("alltime_interval_seconds", 600))
        alltime_top = int(self.cfg.get("alltime_top_n", 8))
        alltime_days = int(self.cfg.get("alltime_days", 30))
        last_api = 0.0
        last_sessions = 0.0
        last_alltime = 0.0
        last_team_ov = 0.0
        team_ov_iv = 30   # how often the admin refetches the team overview to embed for the phone
        last_update = 0.0
        last_status = 0.0
        warned_token = False
        while not self._stop.is_set():
            try:
                now = time.time()
                if now - last_sessions >= sessions_iv:
                    try:
                        self._sessions = scan_sessions(self._sessions_cache, now, top_n=sessions_top)
                    except Exception:
                        log("sessions scan error:\n" + traceback.format_exc())
                    last_sessions = now
                if self.cfg.get("update_check", True) and (now - last_update >= 86400):
                    last_update = now
                    latest, dl = check_github_latest()
                    if latest and _vtuple(latest) > _vtuple(__version__):
                        self._update_available, self._update_url = latest, dl
                        if not getattr(self, "_update_notified", False):
                            notify("Update available", f"v{latest} — open the tray menu → Update")
                            self._update_notified = True
                if self.cfg.get("status_check", True) and (now - last_status >= status_iv):
                    last_status = now
                    s = fetch_status()
                    if s:
                        self._status = s
                sl = read_statusline_snapshot()
                fresh = bool(sl and (now - sl["ts"]) <= stale_secs)
                # Prefer the statusline file (no API). Hit /api/oauth/usage only to
                # occasionally refresh overage/scoped extras, or as a fallback when
                # Claude Code isn't running — heavily rate-limited so we poll rarely.
                if fresh:
                    self._context = sl.get("context") or self._context
                    self._cwd = sl.get("cwd") or self._cwd
                    if now - last_api >= extras_iv:
                        rr = fetch_usage(timeout)
                        last_api = now
                        if rr.ok:
                            self._extra = rr.extra
                    r = FetchResult(True, windows=sl["windows"], extra=self._extra)
                    src = "statusline"
                elif now - last_api >= fallback_iv:
                    r = fetch_usage(timeout)
                    last_api = now
                    if r.ok:
                        self._extra = r.extra
                    src = "api"
                else:
                    r = FetchResult(False, error="waiting for Claude Code activity")
                    src = "idle"
                self.last = r

                if r.ok:
                    append_history(self.history, r.windows, now, cap)
                    save_json(HISTORY_PATH, self.history)
                    check_thresholds(r.windows, self.state, self.cfg)
                    check_danger(r.windows, self.state, self.cfg)
                    save_json(STATE_PATH, self.state)
                    warned_token = False
                    log(f"ok ({src}): {status_line(r.windows)}")
                    if not self._started_notified and self.cfg.get("notify_on_start", True):
                        notify(f"{APP_NAME} started", status_line(r.windows))
                        self._started_notified = True
                    snap = build_snapshot(r, self.history, self.cfg)
                    check_alerts(snap, self.state, self.cfg)
                    save_json(STATE_PATH, self.state)
                    save_json(SNAPSHOT_PATH, snap)
                else:
                    if r.token_state in (TokenState.EXPIRED, TokenState.MISSING) and not warned_token:
                        notify(APP_NAME, f"{r.error}. Run any Claude Code command to refresh your login.")
                        warned_token = True
                    # Keep the last good reading; just flag the state.
                    with STORE_LOCK:
                        snap = dict(STORE.get("snapshot", {}))
                    snap["ok"] = False
                    snap["error"] = r.error
                    snap["token_state"] = r.token_state
                    snap["updated_at"] = int(now)
                snap["sessions"] = self._sessions
                snap["context"] = self._context
                snap["cwd"] = self._cwd
                snap["alltime"] = self._alltime
                snap["update"] = {"available": self._update_available, "url": self._update_url}
                snap["status"] = self._status
                snap["ui"] = {k: self.cfg.get(k) for k in
                              ("bar_fields",
                               "show_widget_on_start", "show_bar_on_start", "status_components",
                               "notify_session_waiting", "remote_transcript")}
                snap["config_epoch"] = self._config_epoch
                snap["overlays_alive"] = {"widget": self._widget_alive(), "bar": self._bar_alive()}
                snap["remote"] = {"enabled": bool(self.cfg.get("remote_enabled")),
                                  "relay_url": self.cfg.get("remote_relay_url", ""),
                                  "available": remote_available(),
                                  "paired": load_remote_identity(create=False) is not None,
                                  "last_sync_ok": self._remote.last_ok}
                team_signed_in = supabase_pool.configured() and supabase_pool.has_session()
                is_team_admin = supabase_pool.is_admin() if team_signed_in else False
                snap["team"] = {"in_team": team_signed_in,
                                "role": ("admin" if is_team_admin else "member") if team_signed_in else None,
                                "email": supabase_pool.email() if team_signed_in else None,
                                "team": supabase_pool.team() if team_signed_in else None,
                                "report_seconds": int(self.cfg.get("team_report_seconds", 10)),
                                "tz": self.cfg.get("team_tz", "Europe/Athens"),
                                "last_ok": self._team.last_ok}
                # Admin only: the compact pool overview the phone renders (E2EE via the snapshot).
                snap["team_overview"] = self._team_overview if is_team_admin else None
                self._verdict = (snap.get("verdict") or {}).get("text", "")
                with STORE_LOCK:
                    STORE["snapshot"] = snap
                self._refresh_visual(r)
                # Relay the snapshot to the phone (opt-in, E2EE), throttled + off-thread.
                if RemoteSync.enabled(self.cfg):
                    iv = max(5, int(self.cfg.get("remote_sync_seconds", 10)))
                    if self._remote.due(now, iv):
                        threading.Thread(target=self._remote.sync, args=(snap, self.cfg), daemon=True).start()
                # Team mode: push the compact usage row to the admin's relay (docs/TEAM.md).
                if TeamSync.enabled(self.cfg):
                    tiv = max(5, int(self.cfg.get("team_report_seconds", 10)))
                    if self._team.due(now, tiv):
                        threading.Thread(target=self._team.sync,
                                         args=(snap, self.cfg, self._alltime_cache), daemon=True).start()
                # Admin + phone paired: refresh the compact team overview embedded for the phone.
                # Off-thread + throttled so the relay round-trips never block the poll loop.
                if (RemoteSync.enabled(self.cfg) and is_team_admin
                        and now - last_team_ov >= team_ov_iv):
                    last_team_ov = now
                    threading.Thread(target=self._refresh_team_overview, daemon=True).start()
                # Fold newly-written session bytes into lifetime totals. Runs after
                # the live snapshot is published so the first (full-history) backfill
                # never delays first paint; the result lands in the next snapshot.
                if now - last_alltime >= alltime_iv:
                    try:
                        self._alltime = scan_all_time(self._alltime_cache, now,
                                                      top_n=alltime_top, days=alltime_days)
                        save_json(ALLTIME_PATH, self._alltime_cache)
                    except Exception:
                        log("all-time scan error:\n" + traceback.format_exc())
                    last_alltime = now
            except Exception:
                log("poll loop error:\n" + traceback.format_exc())
            self._wake.wait(timeout=ui_iv)
            self._wake.clear()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_once(debug: bool) -> int:
    cfg = load_config()
    r = fetch_usage(int(cfg.get("request_timeout_seconds", 20)))
    if r.ok:
        print("STATUS:", status_line(r.windows))
        for key, w in r.windows.items():
            print(f"  {key:16s} {w['pct']:6.1f}%   resets {fmt_reset(w['resets_at'])}")
        if r.extra and r.extra.get("enabled"):
            e = r.extra
            if e.get("used") is not None:
                print(f"  overage credits  {e['currency']} {e['used']:.2f} / {e['limit']:.2f}  ({e['pct']:.0f}%)")
    else:
        print("ERROR:", r.error, f"(token_state={r.token_state})")
    if debug:
        print("\n--- RAW RESPONSE ---")
        print(json.dumps(r.raw, indent=2)[:6000] if r.raw is not None else "(no body)")
    return 0 if r.ok else 1


# ---------------------------------------------------------------------------
# Session-waiting alerts (Claude Code idle hook -> tray -> toast/push)
# ---------------------------------------------------------------------------

def _read_server_port():
    try:
        return int(PORT_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def run_session_hook() -> int:
    """Entry point for the Claude Code Notification[idle_prompt] hook. Reads the hook
    JSON on stdin and POSTs {cwd, session_id} to the running tray. Fast + best-effort;
    never blocks Claude Code."""
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        d = json.loads(raw) if raw.strip() else {}
    except Exception:
        d = {}
    port = _read_server_port()
    if not port:
        return 0
    body = json.dumps({"cwd": d.get("cwd"), "session_id": d.get("session_id"),
                       "stop_hook_active": d.get("stop_hook_active"),
                       "notification_type": d.get("notification_type"),
                       "message": d.get("message")}).encode("utf-8")
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/session-waiting",
                                     data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass
    return 0


def _session_hook_command() -> str:
    """Shell command Claude Code runs for the idle hook."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --session-hook'
    return f'"{_pythonw()}" "{Path(__file__).resolve()}" --session-hook'


def _save_claude_settings(data: dict) -> None:
    try:
        if CLAUDE_SETTINGS.exists():
            CLAUDE_SETTINGS.with_name(CLAUDE_SETTINGS.name + ".cut-bak").write_text(
                CLAUDE_SETTINGS.read_text(encoding="utf-8"), encoding="utf-8")
        save_json(CLAUDE_SETTINGS, data)   # atomic tmp+replace
    except Exception as exc:
        log(f"write claude settings failed: {exc}")


def _strip_session_hook(groups) -> list:
    """Return `groups` with our --session-hook command removed (drops now-empty groups)."""
    out = []
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            continue
        kept = [h for h in (g.get("hooks") or [])
                if not (isinstance(h, dict) and "--session-hook" in str(h.get("command", "")))]
        if kept:
            g = dict(g); g["hooks"] = kept; out.append(g)
    return out


def install_session_hook() -> bool:
    """Install our session-waiting hook on the **Stop** event in ~/.claude/settings.json —
    Stop fires the moment Claude finishes a turn (i.e. it's now awaiting you), so you get
    pinged every time. (The old Notification[idle_prompt] hook only fired after Claude Code's
    own ~60s idle debounce, which is why pings were missed — we migrate off it here.)
    Idempotent; backs the file up first. Returns True on success."""
    try:
        data = load_json(CLAUDE_SETTINGS, {})
        if not isinstance(data, dict):
            data = {}
        hooks = data.setdefault("hooks", {})
        cmd = _session_hook_command()
        # migrate: remove any prior Notification[idle_prompt] hook of ours
        if isinstance(hooks.get("Notification"), list):
            cleaned = _strip_session_hook(hooks["Notification"])
            if cleaned:
                hooks["Notification"] = cleaned
            else:
                hooks.pop("Notification", None)
        groups = hooks.setdefault("Stop", [])
        if not isinstance(groups, list):
            groups = hooks["Stop"] = []
        for g in groups:                                   # already present -> refresh path
            for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
                if isinstance(h, dict) and "--session-hook" in str(h.get("command", "")):
                    h["command"] = cmd
                    _save_claude_settings(data)
                    return True
        groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
        _save_claude_settings(data)
        log("installed session-waiting (Stop) hook")
        return True
    except Exception as exc:
        log(f"install session hook failed: {exc}")
        return False


def remove_session_hook() -> None:
    """Strip our hook out of ~/.claude/settings.json — from the Stop event and the legacy
    Notification event — leaving any other hooks intact."""
    try:
        data = load_json(CLAUDE_SETTINGS, None)
        if not isinstance(data, dict):
            return
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return
        for event in ("Stop", "Notification"):
            if isinstance(hooks.get(event), list):
                cleaned = _strip_session_hook(hooks[event])
                if cleaned:
                    hooks[event] = cleaned
                else:
                    hooks.pop(event, None)
        if not hooks:
            data.pop("hooks", None)
        _save_claude_settings(data)
        log("removed session-waiting hook")
    except Exception as exc:
        log(f"remove session hook failed: {exc}")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--once", action="store_true", help="print status once and exit")
    ap.add_argument("--debug", action="store_true", help="with --once, dump raw API JSON")
    ap.add_argument("--window", action="store_true", help="open the dashboard as a native window")
    ap.add_argument("--widget", action="store_true", help="open the always-on-top mini widget")
    ap.add_argument("--bar", action="store_true", help="open the minimal one-line HUD bar overlay")
    ap.add_argument("--port", type=int, default=None, help="dashboard port (with --window/--widget/--bar)")
    ap.add_argument("--install", action="store_true", help="interactive setup (shortcuts + autostart)")
    ap.add_argument("--uninstall", action="store_true", help="remove shortcuts")
    ap.add_argument("--install-autostart", action="store_true", help="add the Startup shortcut only")
    ap.add_argument("--uninstall-autostart", action="store_true")
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--session-hook", action="store_true",
                    help="(internal) Claude Code idle hook: forward the session to the tray")
    args = ap.parse_args()

    if args.session_hook:
        return run_session_hook()
    if args.version:
        print(f"{APP_NAME} {__version__}")
        return 0
    if args.install:
        do_install()
        return 0
    if args.uninstall:
        do_uninstall()
        return 0
    if args.install_autostart:
        install_autostart()
        return 0
    if args.uninstall_autostart:
        uninstall_autostart()
        return 0
    if args.once:
        return run_once(args.debug)
    if args.window:
        port = args.port or int(load_config().get("dashboard_port", 8787))
        return run_window(port)
    if args.widget:
        port = args.port or int(load_config().get("dashboard_port", 8787))
        return run_overlay(port, "panel")
    if args.bar:
        port = args.port or int(load_config().get("dashboard_port", 8787))
        return run_overlay(port, "bar")

    # Tray mode: single-instance guard (hold a localhost port for the lifetime).
    global _instance_guard
    try:
        _instance_guard = bind_guard(49222)
    except OSError:
        log("another instance is already running; exiting")
        return 0

    log("tray app starting")
    TrayApp(load_config()).run()
    log("tray app stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
