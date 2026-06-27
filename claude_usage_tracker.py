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

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

APP_NAME = "Claude Usage Tracker"


__version__ = "0.1.18"


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

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

# Headers mirror what the Claude Code CLI sends for OAuth requests.
BASE_HEADERS = {
    "anthropic-beta": "oauth-2025-04-20",
    "anthropic-version": "2023-06-01",
    "User-Agent": "claude-cli/2.0.0 (external, cli)",
    "Accept": "application/json",
}

DEFAULT_CONFIG = {
    "ui_refresh_seconds": 15,            # how often the loop refreshes the UI from the statusline file
    "statusline_stale_seconds": 300,     # treat statusline data older than this as stale
    "api_extras_interval_seconds": 1800, # how often to refresh overage/scoped extras from the API
    "api_fallback_interval_seconds": 300,# min gap between API polls when no statusline data
    "sessions_interval_seconds": 45,     # how often to rescan local session logs
    "sessions_top_n": 8,
    "alltime_interval_seconds": 600,     # how often to fold new session bytes into lifetime totals
    "alltime_top_n": 8,                  # projects shown on the All-time tab
    "alltime_days": 30,                  # span of the daily-usage chart
    "predictive_alerts": True,           # burn-rate / context-full / overage toasts
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
    "bar_width": 360,
    "bar_height": 40,
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
CONTROL: dict = {}   # {"refresh": fn, "check_update": fn} — wired by the running TrayApp
                     # so the dashboard/widget can drive the poll loop over HTTP


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
                 token_state=TokenState.OK, retry_after=None):
        self.ok = ok
        self.windows = windows or {}     # key -> {"pct": float, "resets_at": datetime|None}
        self.extra = extra               # overage credits dict or None
        self.raw = raw
        self.error = error
        self.token_state = token_state
        self.retry_after = retry_after   # seconds to back off (HTTP 429)


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
            try:
                ra = int(exc.headers.get("retry-after", ""))
            except (TypeError, ValueError):
                ra = 300
            return FetchResult(False, error="Rate limited by API (429)", retry_after=max(60, ra))
        return FetchResult(False, error=f"HTTP {exc.code}")
    except Exception as exc:
        return FetchResult(False, error=f"{type(exc).__name__}: {exc}")
    return FetchResult(True, windows=parse_windows(data), extra=parse_extra(data), raw=data)


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
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(ent["size"])
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            o = json.loads(line)
                        except Exception:
                            continue
                        if ent["cwd"] is None and isinstance(o.get("cwd"), str):
                            ent["cwd"] = o["cwd"]
                        msg = o.get("message")
                        u = msg.get("usage") if isinstance(msg, dict) else o.get("usage")
                        if isinstance(u, dict):
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
        cwd = ent.get("cwd")
        # platform-independent basename (cwd uses Windows '\' even when scanned elsewhere)
        name = cwd.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1] if cwd else Path(key).parent.name
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
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(consumed)
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if cwd is None and isinstance(o.get("cwd"), str):
                        cwd = o["cwd"]
                    msg = o.get("message")
                    u = msg.get("usage") if isinstance(msg, dict) else o.get("usage")
                    if not isinstance(u, dict):
                        continue
                    inp = int(u.get("input_tokens", 0) or 0)
                    out = int(u.get("output_tokens", 0) or 0)
                    cw = int(u.get("cache_creation_input_tokens", 0) or 0)
                    cr = int(u.get("cache_read_input_tokens", 0) or 0)
                    if inp == out == cw == cr == 0:
                        continue
                    model = pretty_model(msg.get("model") if isinstance(msg, dict) else None)
                    name = cwd.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1] if cwd else Path(key).parent.name
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
}


def compute_verdict(windows) -> dict:
    """One traffic-light status from the 5h + weekly windows (pct + burn-rate ETA)."""
    level = "ok"
    for w in windows:
        if w.get("key") not in ("five_hour", "seven_day"):
            continue
        pct, eta = w.get("pct", 0), w.get("eta_seconds")
        if pct >= 95:
            level = "stop"
        elif (pct >= 80 or eta is not None) and level != "stop":
            level = "caution"
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
        "poll_interval": int(cfg.get("poll_interval_seconds", 60)),
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
            box = [x0 + 2, max(top + 2, ytop), x1 - 2, bot - 2]
            if box[3] - box[1] >= 8:
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
# Notifications
# ---------------------------------------------------------------------------

def notify(title: str, msg: str) -> None:
    try:
        from winotify import Notification, audio
        kwargs = {"app_id": APP_NAME, "title": title, "msg": msg}
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
            name = cwd.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1] if cwd else "session"
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
    --bg:#0e0e10; --panel:#161618; --panel2:#1d1d20;
    --line:rgba(255,255,255,.07); --line2:rgba(255,255,255,.045);
    --ink:#e7e6e3; --dim:#9b9a95; --faint:#6c6b66;
    --accent:#d97757; --accent2:#7f93b0;
    --ok:#5e9e72; --warn:#cda24e; --high:#d4694f;
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

  /* tabs */
  .tabs{display:inline-flex;gap:2px;margin-bottom:18px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:3px}
  .tab{background:none;border:0;color:var(--dim);font:600 12px/1 var(--sans);padding:8px 16px;border-radius:6px;cursor:pointer}
  .tab:hover{color:var(--ink)}
  .tab.on{background:var(--panel2);color:var(--ink)}
  .tabpane[hidden]{display:none}

  /* cards */
  .card{background:var(--panel);border:1px solid var(--line);border-radius:11px}
  .panel{padding:18px}
  .ptitle{font:11px/1 var(--mono);color:var(--faint);text-transform:uppercase;letter-spacing:1px;
    margin-bottom:16px;display:flex;justify-content:space-between;align-items:center;gap:10px}
  .legend{display:flex;gap:12px;flex-wrap:wrap}
  .legend span{display:inline-flex;align-items:center;gap:5px;font:11px/1 var(--mono);color:var(--dim);text-transform:none;letter-spacing:0}
  .legend i{width:8px;height:8px;border-radius:2px;display:inline-block}

  /* gauges */
  .gauges{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
  @media(max-width:520px){.gauges{grid-template-columns:1fr}}
  .gauge{container-type:inline-size;padding:20px}
  .gwrap{display:flex;flex-direction:column;align-items:center;gap:18px}
  @container (min-width:360px){.gwrap{flex-direction:row;justify-content:center;gap:26px}.ginfo{align-items:flex-start;text-align:left}}
  .gauge-dial{position:relative;display:grid;place-items:center;flex:none}
  .dial{width:clamp(124px,40cqi,156px);height:auto;aspect-ratio:1;display:block;overflow:visible}
  .dial-track{fill:none;stroke:var(--panel2);stroke-width:7}
  .dial-arc{fill:none;stroke:var(--accent);stroke-width:7;stroke-linecap:round;
    stroke-dasharray:314.16;stroke-dashoffset:314.16;
    transition:stroke-dashoffset .8s cubic-bezier(.22,1,.36,1),stroke .4s}
  .dial-tick{stroke:var(--line)}
  .dial-val{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px}
  .pct{font:600 clamp(26px,12cqi,38px)/1 var(--mono);letter-spacing:-1px;color:var(--ink);font-variant-numeric:tabular-nums}
  .pct span{font-size:.5em;color:var(--dim);margin-left:1px}
  .glabel{font:10px/1 var(--mono);color:var(--faint);text-transform:uppercase;letter-spacing:1.5px}
  .ginfo{display:flex;flex-direction:column;align-items:center;text-align:center;gap:7px;min-width:0}
  .reset b{font:500 15px/1.2 var(--mono);font-variant-numeric:tabular-nums}
  .reset .abs{color:var(--faint);font:11px/1.3 var(--mono);margin-top:3px}
  .burn{font:11px/1.4 var(--mono);color:var(--dim);min-height:15px}
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

  /* scale / footer / error */
  .scale{display:flex;justify-content:center;gap:16px;flex-wrap:wrap;margin-top:18px;color:var(--faint);font:11px/1 var(--mono)}
  .scale span{display:inline-flex;align-items:center;gap:6px} .scale i{width:9px;height:9px;border-radius:2px;display:inline-block}
  footer{margin-top:18px;text-align:center;color:var(--faint);font:11px/1.7 var(--mono)}
  .err{padding:13px 15px;border:1px solid rgba(212,105,79,.4);background:rgba(212,105,79,.08);
    border-radius:9px;color:#e9b3a6;margin-bottom:16px;font-size:13px;display:none}
  .err.show{display:flex;align-items:center;flex-wrap:wrap;gap:8px}
  .err .signin{background:var(--accent);color:#1c0f08;border:0;font:600 12px/1 var(--sans);
    padding:7px 13px;border-radius:6px;cursor:pointer;margin-left:auto}
  .err .signin:hover{filter:brightness(1.08)} .err .signin:disabled{opacity:.6;cursor:default}
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
  .hm{width:11px;height:11px;border-radius:2px;background:var(--panel2)}
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
    <button class="updcta" id="updcta" hidden></button>
    <button class="ic" id="btn-refresh" title="Re-check usage now">↻</button>
  </header>

  <div class="err" id="err"></div>

  <div class="tabs">
    <button class="tab on" data-t="live">Live</button>
    <button class="tab" data-t="alltime">All-time</button>
  </div>

  <div id="tab-live" class="tabpane">
  <div class="gauges">
    <div class="card gauge" id="g-five_hour">
      <div class="gwrap">
        <div class="gauge-dial">
          <svg class="dial" viewBox="0 0 120 120">
            <circle class="dial-track" cx="60" cy="60" r="50"></circle>
            <circle class="dial-arc" id="arc-five_hour" cx="60" cy="60" r="50" transform="rotate(-90 60 60)"></circle>
          </svg>
          <div class="dial-val">
            <div class="pct" id="p-five_hour">––<span>%</span></div>
            <div class="glabel">5-hour</div>
          </div>
        </div>
        <div class="ginfo">
          <div class="reset"><b id="cd-five_hour">—</b><div class="abs" id="ab-five_hour"></div></div>
          <div class="burn" id="bn-five_hour"></div>
        </div>
      </div>
    </div>
    <div class="card gauge" id="g-seven_day">
      <div class="gwrap">
        <div class="gauge-dial">
          <svg class="dial" viewBox="0 0 120 120">
            <circle class="dial-track" cx="60" cy="60" r="50"></circle>
            <circle class="dial-arc" id="arc-seven_day" cx="60" cy="60" r="50" transform="rotate(-90 60 60)"></circle>
          </svg>
          <div class="dial-val">
            <div class="pct" id="p-seven_day">––<span>%</span></div>
            <div class="glabel">Weekly</div>
          </div>
        </div>
        <div class="ginfo">
          <div class="reset"><b id="cd-seven_day">—</b><div class="abs" id="ab-seven_day"></div></div>
          <div class="burn" id="bn-seven_day"></div>
        </div>
      </div>
    </div>
  </div>

  <div class="row">
    <div class="card panel">
      <div class="ptitle">Usage history
        <span class="legend"><span><i style="background:#d97757"></i>5h</span><span><i style="background:#7f93b0"></i>weekly</span></span>
      </div>
      <svg class="spark" id="spark" preserveAspectRatio="none"></svg>
    </div>
    <div class="card panel">
      <div class="ptitle">Overage credits &amp; scoped limits</div>
      <div id="extra"></div>
      <div id="scoped"></div>
    </div>
  </div>

  <div class="card panel" id="sesscard">
    <div class="ptitle"><span>Sessions · last 5h</span>
      <span class="srt">
        <span class="stabs"><button class="stab on" data-m="context">Context&nbsp;%</button><button class="stab" data-m="tokens">Tokens</button></span>
        <span class="legend" id="sesssub"></span>
      </span>
    </div>
    <div id="sessions"></div>
  </div>

  <div class="scale">
    <span><i style="background:#5e9e72"></i>ok · under 60%</span>
    <span><i style="background:#cda24e"></i>busy · 60–80%</span>
    <span><i style="background:#d4694f"></i>near limit · 80%+</span>
  </div>
  </div><!-- /tab-live -->

  <div id="tab-alltime" class="tabpane" hidden>
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

  <footer>
    <button class="linkbtn" id="btn-checkupd">Check for updates</button>
    <span id="updres"></span><br>
    Live usage from Claude Code's statusline data · read-only · no endpoint polling
  </footer>
</div>

<script>
const $=id=>document.getElementById(id);
let WIN={};   // key -> {resets_at, color}
let LASTH=null;   // last history payload, for resize reflow
function esc(s){ return (s||"").replace(/[&<>]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
async function doSignin(){
  const b=$("signin");
  if(b){ b.disabled=true; b.textContent="Opening sign-in…"; }
  try{ await fetch("/api/login",{method:"POST"}); }catch(e){}
  // claude auth login runs in its own console; the next poll picks up the new token.
  setTimeout(()=>{ if(b){ b.disabled=false; b.textContent="Sign in to Claude"; } },4000);
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
}
const DIAL_C=2*Math.PI*50;   // circumference of the dial arc (r=50)
function buildTicks(){       // subtle measured tick ring — the instrument signature
  const ns="http://www.w3.org/2000/svg";
  document.querySelectorAll("svg.dial").forEach(svg=>{
    for(let i=0;i<100;i+=10){
      const a=(i/100)*2*Math.PI-Math.PI/2, r1=55, r2=58.5;
      const ln=document.createElementNS(ns,"line");
      ln.setAttribute("x1",(60+r1*Math.cos(a)).toFixed(2)); ln.setAttribute("y1",(60+r1*Math.sin(a)).toFixed(2));
      ln.setAttribute("x2",(60+r2*Math.cos(a)).toFixed(2)); ln.setAttribute("y2",(60+r2*Math.sin(a)).toFixed(2));
      ln.setAttribute("class","dial-tick");
      svg.insertBefore(ln, svg.firstChild);
    }
  });
}
function renderGauge(w){
  const k=w.key;
  const arc=$("arc-"+k);
  if(arc){ arc.style.strokeDashoffset=(DIAL_C*(1-Math.min(100,w.pct)/100)).toFixed(2); arc.style.stroke=w.color; }
  const p=$("p-"+k); if(p)p.innerHTML=Math.round(w.pct)+"<span>%</span>";
  WIN[k]={resets_at:w.resets_at,color:w.color};
  $("ab-"+k).textContent=fabs(w.resets_at);
  const bn=$("bn-"+k);
  if(bn){
    if(w.eta_seconds!=null&&w.eta_seconds>=0&&w.rate_per_hour>0){
      bn.innerHTML="↗ <span class='hot'>"+w.rate_per_hour.toFixed(1)+"%/h</span> · hits 100% in ~"+fdur(w.eta_seconds);
    }else if(w.rate_per_hour!=null&&w.rate_per_hour>0.1){
      bn.innerHTML="↗ "+w.rate_per_hour.toFixed(1)+"%/h · <span class='ok'>resets before limit</span>";
    }else if(w.rate_per_hour!=null){
      bn.innerHTML="<span class='ok'>steady</span> · no recent burn";
    }else{ bn.textContent="gathering rate…"; }
  }
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
  if(ATVIEW==="models" && !$("tab-alltime").hidden) renderSeries(p.series);
}
async function refresh(){
  try{
    const d=await (await fetch("/api/usage",{cache:"no-store"})).json();
    const wins=d.windows||[];
    const authBad=d.token_state==="expired"||d.token_state==="missing";
    const err=$("err");
    // Big banner only when there's nothing to show (or login needs refreshing);
    // otherwise keep the last data and show a subtle "stale" state.
    if(!d.ok && (authBad || !wins.length)){
      err.className="err show";
      if(authBad){
        err.innerHTML="⚠ "+esc(d.error||"sign-in needed")+" <button class='signin' id='signin'>Sign in to Claude</button>";
        const b=$("signin"); if(b) b.onclick=doSignin;
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
    wins.filter(w=>w.key==="five_hour"||w.key==="seven_day").forEach(renderGauge);
    LASTH=d.history; renderSpark(LASTH);
    renderExtra(d.extra);
    renderScoped(wins);
    renderSessions(d.sessions);
    renderAlltime(d.alltime);
    const up=d.update||{}, cta=$("updcta");
    if(cta){ if(up.available){ cta.hidden=false; if(!cta.disabled)cta.textContent="Update to v"+up.available; } else { cta.hidden=true; } }
    tickCountdowns();
  }catch(e){ $("err").className="err show"; $("err").textContent="⚠ cannot reach the tracker service."; }
}
buildTicks();
$("btn-refresh").onclick=doRefresh;
$("btn-checkupd").onclick=doCheckUpdate;
$("updcta").onclick=doUpdate;
setInterval(tickCountdowns,1000);
setInterval(refresh,5000);
refresh();
let _rz; window.addEventListener("resize",()=>{clearTimeout(_rz);_rz=setTimeout(()=>{renderSpark(LASTH); if(!$("tab-alltime").hidden)renderAlltime();},120);});
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
document.querySelectorAll(".tab").forEach(b=>b.addEventListener("click",()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("on"));
  b.classList.add("on");
  const t=b.dataset.t;
  $("tab-live").hidden=(t!=="live"); $("tab-alltime").hidden=(t!=="alltime");
  if(t==="alltime")renderAlltime();   // (re)draw now that the pane has layout
}));
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
  .grip{position:fixed;right:0;bottom:0;width:18px;height:18px;border:0;background:transparent;cursor:nwse-resize;padding:0}
  .grip::after{content:"";position:absolute;right:3px;bottom:3px;width:8px;height:8px;
    border-right:2px solid var(--faint);border-bottom:2px solid var(--faint)}
  .grip:hover::after{border-color:var(--dim)}
  /* ---- minimal "bar" kind (FPS-overlay style) ---- */
  body.kind-bar{flex-direction:row;align-items:center;gap:0;padding:5px 11px;
    background:rgba(18,18,20,.62);border:1px solid rgba(255,255,255,.08);border-radius:9px}
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
    <select class="tier" id="tier" title="Track a session"></select><span class="verdict" id="verdict"></span><span class="x" onclick="closeWidget()" title="Hide">×</span>
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
  <button class="grip" id="grip" title="Drag to resize"></button>
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
function renderBar(d){           // minimal one-line HUD (dir + chosen percentages)
  const wins=d.windows||[];
  if(!d.ok && !wins.length){ $("bar").innerHTML="<span class='f dir'>"+esc(d.error||"unavailable")+"</span>"; return; }
  const cx=curContext(d), dir=SEL||actDir(d);
  let html="<span class='f dir' title='"+esc(d.cwd||"")+"'>"+esc(dir)+"</span>";
  html+=bfld("Ctx:",cx.pct)+bfld("5h:",pctOf(wins,"five_hour"))+bfld("7d:",pctOf(wins,"seven_day"));
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
  tick();
}catch(e){ if(KIND!=="bar"){ $("dot").style.background="#cda24e"; } } }
(function(){ const g=$("grip"); if(!g)return; let sx,sy,sw,sh,on=false;
  g.addEventListener("mousedown",e=>{ e.stopPropagation(); e.preventDefault(); });  // don't let easy_drag move the window
  g.addEventListener("pointerdown",e=>{ on=true; sx=e.screenX; sy=e.screenY; sw=window.innerWidth; sh=window.innerHeight;
    try{g.setPointerCapture(e.pointerId);}catch(_){} e.preventDefault(); e.stopPropagation(); });
  g.addEventListener("pointermove",e=>{ if(!on)return;
    const W=Math.max(280,Math.round(sw+(e.screenX-sx))), H=Math.max(150,Math.round(sh+(e.screenY-sy)));
    try{ window.pywebview.api.resize(W,H); }catch(_){} });
  g.addEventListener("pointerup",e=>{ if(!on)return; on=false; try{g.releasePointerCapture(e.pointerId);}catch(_){}
    try{ window.pywebview.api.save_size(window.innerWidth,window.innerHeight); }catch(_){} });
})();
$("tier").addEventListener("change",function(){ SEL=this.value; try{localStorage.setItem("trackSel",SEL);}catch(_){} refresh(); });
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
        elif path == "/favicon.ico":
            self._send(204, "image/x-icon", b"")
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        # Loopback only (the server already binds 127.0.0.1; double-check anyway).
        # These just nudge the tray's own poll loop / sign-in — nothing destructive.
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self._send(403, "text/plain", b"forbidden")
            return
        if path == "/api/login":
            threading.Thread(target=launch_login, daemon=True).start()
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
        else:
            self._send(404, "text/plain", b"not found")


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

            def resize(self, w, h):     # called by the corner grip (frameless has no OS edges)
                try:
                    webview.windows[0].resize(int(w), int(h))
                except Exception:
                    pass

            def save_size(self, w, h):  # remember the chosen size for next launch
                try:
                    c = load_config()
                    if bar:
                        c["bar_width"], c["bar_height"] = max(200, int(w)), max(30, int(h))
                    else:
                        c["widget_width"], c["widget_height"] = max(280, int(w)), max(150, int(h))
                    save_json(CONFIG_PATH, c)
                except Exception:
                    pass

        kw = dict(width=w, height=h, resizable=True, min_size=minsz,
                  frameless=True, easy_drag=True, on_top=True, js_api=Api(), **pos)
        if bar:
            kw["transparent"] = True            # see-through HUD; CSS paints a translucent panel
        else:
            kw["background_color"] = "#141416"
        webview.create_window(APP_NAME, url, **kw)
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


def launch_login() -> bool:
    """Launch `claude auth login` (the CLI equivalent of /login) in a visible
    console so the user can finish the browser sign-in. Returns True if started.
    We never write the token ourselves — Claude Code owns
    ~/.claude/.credentials.json; the next poll picks up the refreshed login."""
    exe = _claude_cli()
    if not exe:
        notify("Claude Code not found",
               "Install Claude Code, then sign in — or run `claude auth login` in a terminal.")
        return False
    try:
        if os.name == "nt":
            CREATE_NEW_CONSOLE = 0x00000010
            # String (not list) command so cmd /k keeps the quoted exe + args intact.
            subprocess.Popen(f'cmd /k "{exe}" auth login',
                             creationflags=CREATE_NEW_CONSOLE, close_fds=True)
        else:
            subprocess.Popen([exe, "auth", "login"], close_fds=True)
        notify(APP_NAME, "Opening Claude sign-in — complete it in the terminal window.")
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
        self._update_available = None
        self._update_url = None
        self._verdict = ""
        self._job = None
        self._children = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._started_notified = False

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
        else:
            self.widget_proc = self._spawn_mode("--widget")

    def _bar_alive(self) -> bool:
        return self.bar_proc is not None and self.bar_proc.poll() is None

    def _on_toggle_bar(self, icon, item):
        if self._bar_alive():
            try:
                self.bar_proc.terminate()
            except Exception:
                pass
        else:
            self.bar_proc = self._spawn_mode("--bar")

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
        if not _is_installed_pkg():
            webbrowser.open(RELEASES_URL)
            notify("Update", "Running from source — `git pull` to update (or install via pipx / Setup.exe).")
            return
        notify(APP_NAME, f"Updating to v{self._update_available}…")
        NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
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
        sessions_iv = int(self.cfg.get("sessions_interval_seconds", 45))
        sessions_top = int(self.cfg.get("sessions_top_n", 8))
        alltime_iv = int(self.cfg.get("alltime_interval_seconds", 600))
        alltime_top = int(self.cfg.get("alltime_top_n", 8))
        alltime_days = int(self.cfg.get("alltime_days", 30))
        last_api = 0.0
        last_sessions = 0.0
        last_alltime = 0.0
        last_update = 0.0
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
                self._verdict = (snap.get("verdict") or {}).get("text", "")
                with STORE_LOCK:
                    STORE["snapshot"] = snap
                self._refresh_visual(r)
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
    args = ap.parse_args()

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
