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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

APP_NAME = "Claude Usage Tracker"


__version__ = "0.1.10"


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
    "widget_height": 178,
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
    (100, "#ffd60a", "max"),
    (90,  "#0a0a0c", "crit"),
    (80,  "#f85149", "high"),
    (60,  "#e3893a", "warn"),
    (20,  "#58a6ff", "low"),
    (0,   "#3fb950", "ok"),
]


def usage_style(pct: float) -> tuple[str, str]:
    """Return (hex_color, level) for a 0-100 utilization value."""
    for threshold, hexv, level in USAGE_BANDS:
        if pct >= threshold:
            return (hexv, level)
    return ("#3fb950", "ok")


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
    "ok": ("#3fb950", "All clear"),
    "caution": ("#e3893a", "Ease up"),
    "stop": ("#f85149", "Near limit"),
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
  @property --p5 { syntax:'<number>'; initial-value:0; inherits:false; }
  @property --p7 { syntax:'<number>'; initial-value:0; inherits:false; }
  *{box-sizing:border-box;margin:0;padding:0}
  :root{
    --bg0:#070a10; --bg1:#0c111b; --card:rgba(255,255,255,.045);
    --line:rgba(255,255,255,.09); --ink:#e8eef6; --dim:#8b97a8; --track:rgba(255,255,255,.07);
  }
  html,body{height:100%}
  html{transition:--p5 .9s cubic-bezier(.22,1,.36,1),--p7 .9s cubic-bezier(.22,1,.36,1)}
  body{
    font:14px/1.5 -apple-system,"Segoe UI",Inter,system-ui,sans-serif;
    color:var(--ink);
    background:
      radial-gradient(1200px 700px at 12% -10%, #15233b 0%, transparent 55%),
      radial-gradient(1000px 600px at 110% 10%, #221a33 0%, transparent 50%),
      linear-gradient(180deg,var(--bg1),var(--bg0));
    background-attachment:fixed; padding:clamp(14px,3.2vw,30px) clamp(12px,3vw,26px) clamp(18px,3vw,34px); -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:960px;margin:0 auto}
  header{display:flex;align-items:center;gap:12px;margin-bottom:22px;flex-wrap:wrap}
  .logo{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,#d97757,#cf5b3e);
    display:grid;place-items:center;font-weight:800;color:#1a0f0a;box-shadow:0 6px 18px rgba(207,91,62,.35)}
  h1{font-size:18px;font-weight:650;letter-spacing:.2px}
  .sub{color:var(--dim);font-size:12px}
  .badge{font-size:11px;color:var(--dim);border:1px solid var(--line);padding:3px 9px;border-radius:999px;text-transform:capitalize}
  .vpill{margin-left:auto;font-size:12px;font-weight:650;padding:4px 12px;border-radius:999px;border:1px solid transparent}
  .live{margin-left:14px;display:flex;align-items:center;gap:7px;color:var(--dim);font-size:12px}
  .dot{width:8px;height:8px;border-radius:50%;background:#3fb950;box-shadow:0 0 0 0 rgba(63,185,80,.6);animation:pulse 2.4s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(63,185,80,.55)}70%{box-shadow:0 0 0 9px rgba(63,185,80,0)}100%{box-shadow:0 0 0 0 rgba(63,185,80,0)}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:18px;backdrop-filter:blur(14px);
    box-shadow:0 12px 40px rgba(0,0,0,.35)}
  .gauges{display:grid;grid-template-columns:1fr 1fr;gap:clamp(10px,1.6vw,16px);margin-bottom:clamp(10px,1.6vw,16px)}
  @media(max-width:340px){.gauges{grid-template-columns:1fr}}
  .gauge{container-type:inline-size;padding:clamp(16px,2vw,22px);transition:box-shadow .4s}
  .gauge.warn{box-shadow:0 12px 40px rgba(0,0,0,.35),0 0 26px rgba(227,137,58,.20)}
  .gauge.high{box-shadow:0 12px 40px rgba(0,0,0,.35),0 0 30px rgba(248,81,73,.32)}
  .gauge.crit{box-shadow:0 12px 40px rgba(0,0,0,.35),0 0 38px rgba(248,81,73,.55)}
  .gauge.max{box-shadow:0 12px 40px rgba(0,0,0,.35),0 0 40px rgba(255,214,10,.55)}
  .gwrap{display:flex;flex-direction:column;align-items:center;gap:clamp(12px,3cqi,18px)}
  .ring{position:relative;width:clamp(112px,40cqi,172px);aspect-ratio:1;border-radius:50%;display:grid;place-items:center;flex:none;
    background:conic-gradient(var(--c,#3fb950) calc(var(--p)*1%), var(--track) 0)}
  .ring#r-five_hour{--p:var(--p5)} .ring#r-seven_day{--p:var(--p7)}
  .ring::after{content:"";position:absolute;inset:7.5%;border-radius:50%;
    background:linear-gradient(180deg,#0e1420,#0a0f18);border:1px solid rgba(255,255,255,.05)}
  .ring .val{position:relative;z-index:1;display:flex;flex-direction:column;align-items:center;gap:2px}
  .pct{font-size:clamp(22px,9cqi,40px);font-weight:720;letter-spacing:-1.5px;line-height:.92}
  .pct small{font-size:clamp(11px,3.4cqi,16px);font-weight:600;color:var(--dim);margin-left:1px;letter-spacing:0}
  .glabel{color:var(--dim);font-size:clamp(8px,2.3cqi,11px);text-transform:uppercase;letter-spacing:1.2px}
  .ginfo{display:flex;flex-direction:column;align-items:center;text-align:center;gap:6px;min-width:0}
  .reset{font-size:clamp(12px,3cqi,14px)}
  .reset b{font-variant-numeric:tabular-nums}
  .reset .abs{color:var(--dim);font-size:clamp(10px,2.4cqi,12px);margin-top:2px}
  .burn{font-size:clamp(10px,2.4cqi,12px);color:var(--dim);min-height:16px}
  @container (min-width:380px){
    .gwrap{flex-direction:row;align-items:center;justify-content:center;gap:clamp(18px,5cqi,34px)}
    .ginfo{align-items:flex-start;text-align:left}
  }
  .burn .hot{color:#e3893a;font-weight:600} .burn .ok{color:#3fb950}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:clamp(10px,1.6vw,16px)}
  @media(max-width:600px){.row{grid-template-columns:1fr}}
  .panel{padding:18px 18px 16px}
  .ptitle{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:1.4px;margin-bottom:12px;display:flex;justify-content:space-between}
  .legend span{display:inline-flex;align-items:center;gap:5px;margin-left:10px;font-size:11px;color:var(--dim);text-transform:none;letter-spacing:0}
  .legend i{width:9px;height:9px;border-radius:2px;display:inline-block}
  svg.spark{width:100%;height:clamp(92px,15vw,128px);display:block;overflow:visible}
  .mini{display:flex;align-items:center;gap:10px;margin-top:11px}
  .mini .lbl{width:104px;font-size:12px;color:var(--dim)}
  .bar{flex:1;height:9px;border-radius:6px;background:var(--track);overflow:hidden}
  .bar>i{display:block;height:100%;border-radius:6px;transition:width .8s cubic-bezier(.22,1,.36,1)}
  .mini .num{font-size:12px;font-variant-numeric:tabular-nums;width:46px;text-align:right}
  .credits .cval{font-size:22px;font-weight:680;margin-bottom:2px}
  .credits .csub{color:var(--dim);font-size:12px;margin-bottom:12px}
  .srow{display:flex;align-items:center;gap:10px;margin:9px 0;font-size:12.5px}
  .sdot{width:8px;height:8px;border-radius:50%;flex:none;background:#3a3f48}
  .sdot.on{background:#3fb950;box-shadow:0 0 6px rgba(63,185,80,.55)}
  .sname{width:clamp(84px,24%,190px);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#d7dde6}
  .sbar{flex:1;height:8px;border-radius:5px;background:rgba(255,255,255,.08);overflow:hidden}
  .sbar>i{display:block;height:100%;border-radius:5px;background:#5aa3ff;transition:width .6s cubic-bezier(.22,1,.36,1)}
  .snum{width:52px;text-align:right;color:var(--dim);font-variant-numeric:tabular-nums}
  .sempty{color:var(--dim);font-size:12px;padding:6px 0}
  .srt{display:flex;align-items:center;gap:10px}
  .stabs{display:inline-flex;gap:3px;background:rgba(255,255,255,.05);border-radius:8px;padding:3px}
  .stab{background:none;border:0;color:var(--dim);font:inherit;font-size:11px;padding:3px 9px;border-radius:6px;cursor:pointer;text-transform:none;letter-spacing:0}
  .stab:hover{color:var(--ink)}
  .stab.on{background:rgba(255,255,255,.10);color:var(--ink)}
  .scale{display:flex;justify-content:center;gap:13px;flex-wrap:wrap;margin-top:18px;color:var(--dim);font-size:11px}
  .scale span{display:inline-flex;align-items:center;gap:6px}
  .scale i{width:11px;height:11px;border-radius:3px;display:inline-block}
  footer{margin-top:14px;text-align:center;color:var(--dim);font-size:11px;line-height:1.7}
  .err{padding:16px 18px;border:1px solid rgba(248,81,73,.4);background:rgba(248,81,73,.08);
    border-radius:14px;color:#ffb4ad;margin-bottom:16px;font-size:13px;display:none}
  .err.show{display:block}
  .skel{opacity:.35}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">C</div>
    <div>
      <h1>Claude Usage</h1>
      <div class="sub" id="updated">connecting…</div>
    </div>
    <div class="badge" id="tier">—</div>
    <div class="vpill" id="vpill"></div>
    <div class="live"><span class="dot" id="livedot"></span><span id="livetxt">live</span></div>
  </header>

  <div class="err" id="err"></div>

  <div class="gauges">
    <div class="card gauge" id="g-five_hour">
      <div class="gwrap">
        <div class="ring" id="r-five_hour" style="--c:#3fb950">
          <div class="val">
            <div class="pct" id="p-five_hour">–<small>%</small></div>
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
        <div class="ring" id="r-seven_day" style="--c:#3fb950">
          <div class="val">
            <div class="pct" id="p-seven_day">–<small>%</small></div>
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
        <span class="legend"><span><i style="background:#5aa3ff"></i>5h</span><span><i style="background:#c08bff"></i>weekly</span></span>
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
    <span><i style="background:#3fb950"></i>0–20</span>
    <span><i style="background:#58a6ff"></i>20–60</span>
    <span><i style="background:#e3893a"></i>60–80</span>
    <span><i style="background:#f85149"></i>80–90</span>
    <span><i style="background:#0a0a0c;border:1px solid #2a2f3a"></i>90–99</span>
    <span><i style="background:#ffd60a"></i>100</span>
  </div>

  <footer>
    Live usage from Claude Code's statusline data · read-only<br>
    <span id="src">no endpoint polling — avoids rate limits</span>
  </footer>
</div>

<script>
const $=id=>document.getElementById(id);
let WIN={};   // key -> {resets_at, color}
let LASTH=null;   // last history payload, for resize reflow
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
function renderGauge(w){
  const k=w.key;
  document.documentElement.style.setProperty(k==="five_hour"?"--p5":"--p7", w.pct);
  const ring=$("r-"+k); if(ring)ring.style.setProperty("--c",w.color);
  const p=$("p-"+k); if(p)p.innerHTML=Math.round(w.pct)+"<small>%</small>";
  const g=$("g-"+k); if(g)g.className="card gauge "+(["warn","high","crit","max"].indexOf(w.level)>=0?w.level:"");
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
    e.setAttribute("x",x);e.setAttribute("y",y);e.setAttribute("fill","#5b6675");e.setAttribute("font-size","9");
    if(anchor)e.setAttribute("text-anchor",anchor);e.textContent=t;svg.appendChild(e);}
  [0,50,100].forEach(p=>{line(26,yv(p),W,yv(p),"rgba(255,255,255,.06)");text(22,yv(p)+3,p,"end");});
  if(!h||!h.t||h.t.length<2){
    text(W/2,H/2,"collecting data…","middle");return;
  }
  const t0=h.t[0],t1=h.t[h.t.length-1],span=Math.max(1,t1-t0);
  const x=t=>30+((t-t0)/span)*(W-34);
  [["#5aa3ff",h.five_hour],["#c08bff",h.seven_day]].forEach(c=>{
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
  const head=(e.used!=null&&e.limit!=null)?(cur+" "+e.used.toFixed(2)+" <span style='color:#8b97a8;font-size:13px;font-weight:500'>of "+cur+" "+e.limit.toFixed(2)+"</span>"):(e.pct.toFixed(1)+"% used");
  const col=e.pct>=80?"#e3893a":"#3fb950";
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
      "<div class='bar'><i style='width:"+Math.min(100,w.pct)+"%;background:"+w.color+(w.color==="#0a0a0c"?";box-shadow:inset 0 0 0 1.5px #f85149":"")+"'></i></div>"+
      "<div class='num'>"+Math.round(w.pct)+"%</div></div>");
  });
}
function fmtTok(n){ n=n||0; if(n>=1e6)return (n/1e6).toFixed(n>=1e7?0:1)+"M"; if(n>=1e3)return Math.round(n/1e3)+"k"; return ""+n; }
function bandColor(p){ if(p>=100)return"#ffd60a"; if(p>=80)return"#f85149"; if(p>=60)return"#e3893a"; if(p>=20)return"#58a6ff"; return"#3fb950"; }
let SESS=[], SMODE="context";
function renderSessions(list){
  if(list) SESS=list;
  const box=$("sessions"), sub=$("sesssub");
  if(!SESS.length){ box.innerHTML="<div class='sempty'>no Claude sessions in the last 5h</div>"; sub.textContent=""; return; }
  const act=SESS.filter(s=>s.active).length;
  sub.innerHTML = act ? ("<span style='color:#3fb950'>"+act+" active</span>") : "";
  const maxTok=Math.max.apply(null, SESS.map(s=>s.tokens).concat([1]));
  box.innerHTML=SESS.map(s=>{
    const nm=(s.name||"?").replace(/[<>&]/g,""); let w, val, col;
    if(SMODE==="context"){ const p=s.context_pct||0; w=Math.max(3,Math.min(100,p)); val=Math.round(p)+"%"; col=bandColor(p); }
    else { w=Math.max(3, s.tokens/maxTok*100); val=fmtTok(s.tokens); col="#5aa3ff"; }
    return "<div class='srow'><span class='sdot "+(s.active?"on":"")+"'></span>"+
      "<span class='sname' title='"+nm+"'>"+nm+"</span>"+
      "<div class='sbar'><i style='width:"+w+"%;background:"+col+"'></i></div>"+
      "<span class='snum'>"+val+"</span></div>";
  }).join("");
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
      err.textContent="⚠ "+(d.error||"waiting for data")+(authBad?" — run any Claude Code command to refresh your login.":" — retrying…");
    }else{ err.className="err"; }
    const acc=d.account||{};
    $("tier").textContent = acc.org || d.subscription || "plan";
    $("tier").title = acc.email || "";
    const vp=$("vpill"), v=d.verdict;
    if(v && v.text){ vp.style.display=""; vp.textContent=v.text; vp.style.color=v.color; vp.style.borderColor=v.color+"66"; vp.style.background=v.color+"1f"; }
    else { vp.style.display="none"; }
    if(d.ok){
      $("livedot").style.background="#3fb950"; $("livetxt").textContent="live";
      $("updated").textContent="updated "+new Date(d.updated_at*1000).toLocaleTimeString();
    }else{
      $("livedot").style.background="#e3893a"; $("livetxt").textContent=wins.length?"stale":"offline";
      $("updated").textContent=wins.length?"reconnecting… (rate limited)":"—";
    }
    wins.filter(w=>w.key==="five_hour"||w.key==="seven_day").forEach(renderGauge);
    LASTH=d.history; renderSpark(LASTH);
    renderExtra(d.extra);
    renderScoped(wins);
    renderSessions(d.sessions);
    tickCountdowns();
  }catch(e){ $("err").className="err show"; $("err").textContent="⚠ cannot reach the tracker service."; }
}
setInterval(tickCountdowns,1000);
setInterval(refresh,5000);
refresh();
let _rz; window.addEventListener("resize",()=>{clearTimeout(_rz);_rz=setTimeout(()=>renderSpark(LASTH),120);});
document.querySelectorAll(".stab").forEach(b=>b.addEventListener("click",()=>{
  document.querySelectorAll(".stab").forEach(x=>x.classList.remove("on"));
  b.classList.add("on"); SMODE=b.dataset.m; renderSessions();
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
  body{font:12px/1.4 -apple-system,"Segoe UI",Inter,system-ui,sans-serif;color:#e8eef6;
    background:linear-gradient(180deg,#0e1422,#080b12);
    border:1px solid rgba(255,255,255,.10);border-radius:13px;overflow:hidden;
    padding:11px 13px;user-select:none;-webkit-user-select:none;cursor:default}
  .top{display:flex;align-items:center;gap:7px;margin-bottom:10px;color:#8b97a8;font-size:10px}
  .top .dot{width:6px;height:6px;border-radius:50%;background:#3fb950}
  .top .ttl{letter-spacing:.3px;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .top .tier{color:#e8eef6;font-size:13.5px;font-weight:700;text-transform:none;letter-spacing:.2px}
  .top .verdict{margin-left:auto;font-weight:700;font-size:11px;padding-right:8px;white-space:nowrap}
  .top .x{cursor:pointer;color:#6b7686;font-size:15px;line-height:1;padding:0 2px}
  .top .x:hover{color:#e8eef6}
  .row{display:flex;align-items:center;gap:10px;margin:8px 0}
  .lab{width:32px;color:#aeb8c6;font-weight:650;font-size:11.5px}
  .bar{flex:1;height:9px;border-radius:5px;background:rgba(255,255,255,.12);overflow:hidden}
  .bar>i{display:block;height:100%;border-radius:5px;width:0;transition:width .7s cubic-bezier(.22,1,.36,1)}
  .pc{width:40px;text-align:right;font-weight:720;font-size:15px;font-variant-numeric:tabular-nums}
  .cd{width:60px;text-align:right;color:#8b97a8;font-size:10.5px;font-variant-numeric:tabular-nums}
  .err{color:#ffb4ad;font-size:11px;padding:8px 2px}
</style>
</head>
<body>
  <div class="top">
    <span class="dot" id="dot"></span><span class="ttl" id="acct">Claude usage</span>
    <span class="tier" id="tier"></span><span class="verdict" id="verdict"></span><span class="x" onclick="closeWidget()" title="Hide">×</span>
  </div>
  <div id="body">
    <div class="row"><span class="lab">5h</span><div class="bar"><i id="b5"></i></div><span class="pc" id="pc5">–</span><span class="cd" id="cd5"></span></div>
    <div class="row"><span class="lab">Week</span><div class="bar"><i id="b7"></i></div><span class="pc" id="pc7">–</span><span class="cd" id="cd7"></span></div>
    <div class="row"><span class="lab">Ctx</span><div class="bar"><i id="bc"></i></div><span class="pc" id="pcc">–</span><span class="cd" id="cdc"></span></div>
  </div>
<script>
const $=id=>document.getElementById(id);
let R={};
function bandColor(p){ if(p>=100)return"#ffd60a"; if(p>=80)return"#f85149"; if(p>=60)return"#e3893a"; if(p>=20)return"#58a6ff"; return"#3fb950"; }
function fmtTok(n){ n=n||0; if(n>=1e6)return (n/1e6).toFixed(1)+"M"; if(n>=1e3)return Math.round(n/1e3)+"k"; return ""+n; }
function closeWidget(){ try{ window.pywebview.api.close(); }catch(e){ try{window.close();}catch(_){} } }
function sdur(s){ if(s==null)return""; s=Math.max(0,Math.floor(s));
  const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  if(d>0)return"↻ "+d+"d "+h+"h"; if(h>0)return"↻ "+h+"h "+String(m).padStart(2,"0")+"m";
  if(m>0)return"↻ "+m+"m"; return"↻ "+(s%60)+"s"; }
function tick(){const now=Date.now();for(const k in R){const el=$("cd"+k);if(el)el.textContent=R[k]?sdur((R[k]-now)/1000):"";}}
function setRow(i,w){ const b=$("b"+i),pc=$("pc"+i); const black=w.color==="#0a0a0c";
  if(b){b.style.width=Math.min(100,w.pct)+"%";b.style.background=w.color;
    b.style.boxShadow=black?"inset 0 0 0 1.5px #f85149":"none";}
  if(pc){pc.textContent=Math.round(w.pct)+"%";pc.style.color=black?"#e8eef6":w.color;}
  R[i]=w.resets_at; }
async function refresh(){ try{
  const d=await (await fetch("/api/usage",{cache:"no-store"})).json();
  const wins=d.windows||[];
  if(!d.ok && !wins.length){ $("dot").style.background="#f85149"; $("tier").textContent=(d.error||"unavailable"); return; }
  $("dot").style.background=d.ok?"#3fb950":"#e3893a";
  const v=d.verdict;
  if(v && v.text){ $("verdict").textContent=v.text; $("verdict").style.color=v.color; if(d.ok)$("dot").style.background=v.color; }
  else { $("verdict").textContent=""; }
  const acc=d.account||{};
  $("acct").textContent = acc.org || acc.name || (acc.email||"").split("@")[0] || "Claude";
  $("acct").title = acc.email || "";
  const dir=(d.cwd||"").replace(/[\\\/]+$/,"").split(/[\\\/]/).pop();
  $("tier").textContent = dir || d.subscription || "";
  $("tier").title = d.cwd || "";
  wins.forEach(w=>{ if(w.key==="five_hour")setRow("5",w); if(w.key==="seven_day")setRow("7",w); });
  const c=d.context;
  if(c && c.used_percentage!=null){
    const p=c.used_percentage, col=bandColor(p), bk=p>=90;
    $("bc").style.width=Math.min(100,p)+"%"; $("bc").style.background=col;
    $("pcc").textContent=Math.round(p)+"%"; $("pcc").style.color=col;
    $("cdc").textContent=c.total_input_tokens?fmtTok(c.total_input_tokens):"";
  }
  tick();
}catch(e){ $("dot").style.background="#e3893a"; } }
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


def run_widget(port: int) -> int:
    """Small frameless always-on-top draggable widget (the /widget view)."""
    global _instance_guard
    cfg = load_config()
    url = f"http://127.0.0.1:{port}/widget"
    try:
        _instance_guard = bind_guard(49224)   # one widget at a time
    except OSError:
        return 0
    try:
        import webview
        ensure_app_icon()
        set_app_user_model_id()

        w = int(cfg.get("widget_width", 392))
        h = int(cfg.get("widget_height", 150))
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

        webview.create_window(APP_NAME, url, width=w, height=h, resizable=False,
                              frameless=True, easy_drag=True, on_top=True,
                              background_color="#0e1422", js_api=Api(), **pos)
        threading.Thread(target=_apply_window_icon, args=(True,), daemon=True).start()
        webview.start(icon=str(ICO_PATH))
    except Exception as exc:
        log(f"widget mode failed: {exc}")
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
            pystray.MenuItem("Open dashboard", self._on_open, default=True),
            pystray.MenuItem("Open in browser", self._on_browser),
            pystray.MenuItem("Refresh now", self._on_refresh),
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
        threading.Thread(target=self._poll_loop, daemon=True).start()
        if self.cfg.get("show_widget_on_start", True):
            self.widget_proc = self._spawn_mode("--widget")
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
            webbrowser.open(RELEASES_URL)
            notify("Update", "Run: pipx upgrade claude-usage-tracker  (then relaunch)")

    def _on_refresh(self, icon, item):
        self._wake.set()

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
        self._context = getattr(self, "_context", None)
        self._cwd = getattr(self, "_cwd", None)
        self._verdict = getattr(self, "_verdict", "")
        self._update_available = getattr(self, "_update_available", None)
        self._update_url = getattr(self, "_update_url", None)
        sessions_iv = int(self.cfg.get("sessions_interval_seconds", 45))
        sessions_top = int(self.cfg.get("sessions_top_n", 8))
        last_api = 0.0
        last_sessions = 0.0
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
                self._verdict = (snap.get("verdict") or {}).get("text", "")
                with STORE_LOCK:
                    STORE["snapshot"] = snap
                self._refresh_visual(r)
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
    ap.add_argument("--port", type=int, default=None, help="dashboard port (with --window/--widget)")
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
        return run_widget(port)

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
