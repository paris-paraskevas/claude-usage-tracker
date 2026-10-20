"""Usage readings: Claude Code credentials (read-only), the OAuth usage endpoint, and the
snapshot our `--statusline` command writes. All times are epoch seconds."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from ctu import io, paths

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"

# Mirrors what the Claude Code CLI sends for OAuth requests.
BASE_HEADERS = {
    "anthropic-beta": "oauth-2025-04-20",
    "anthropic-version": "2023-06-01",
    "User-Agent": "claude-cli/2.0.0 (external, cli)",
    "Accept": "application/json",
}

LIMIT_IDS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")


class TokenState:
    OK = "ok"
    EXPIRED = "expired"
    MISSING = "missing"


class _MtimeCache:
    """Re-parse a JSON file only when its mtime/size changes (Claude Code rewrites these often)."""

    def __init__(self, path_attr: str, extract):
        self.path_attr, self.extract = path_attr, extract
        self.sig, self.data = None, None
        self.lock = threading.Lock()

    def get(self):
        p = getattr(paths, self.path_attr)
        sig = io.file_sig(p)
        sig = sig and (str(p), *sig)
        with self.lock:
            if sig is None:
                self.sig, self.data = None, None
                return None
            if sig != self.sig:
                try:
                    self.data = self.extract(json.loads(p.read_text(encoding="utf-8")))
                    self.sig = sig
                except Exception:
                    pass        # mid-write; keep the last good value and retry next time
            return self.data


def _oauth_of(d):
    return (d or {}).get("claudeAiOauth") or {} if isinstance(d, dict) else {}


def _account_of(d):
    a = (d.get("oauthAccount") if isinstance(d, dict) else None) or {}
    return {"email": a.get("emailAddress"), "name": a.get("displayName"), "org": a.get("organizationName")}


_creds = _MtimeCache("CREDS_PATH", _oauth_of)
_account = _MtimeCache("CLAUDE_JSON", _account_of)


def read_oauth() -> dict:
    return _creds.get() or {}


def read_account():
    """Logged-in account {email, name, org} from ~/.claude.json, or None."""
    return _account.get()


def read_plan() -> str:
    return str(read_oauth().get("subscriptionType") or "")


def read_token() -> tuple[str | None, str]:
    """(access_token, state). Never writes the credentials file."""
    oauth = read_oauth()
    token = oauth.get("accessToken")
    expires_at = oauth.get("expiresAt")  # epoch ms
    if not token:
        return None, TokenState.MISSING
    if isinstance(expires_at, (int, float)) and expires_at / 1000.0 <= time.time() + 60:
        return token, TokenState.EXPIRED
    return token, TokenState.OK


class FetchResult:
    def __init__(self, ok, windows=None, extra=None, raw=None, error=None, token_state=TokenState.OK):
        self.ok = ok
        self.windows = windows or {}     # id -> {"pct": float, "resets_at": int | None}
        self.extra = extra               # overage dict or None
        self.raw = raw
        self.error = error
        self.token_state = token_state


def _coerce_pct(value) -> float | None:
    """Clamp a 0-100 utilisation (the endpoint already reports percent)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(100.0, float(value)))


def parse_reset(value) -> int | None:
    """Epoch seconds from epoch s/ms or ISO-8601, else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            return int(value / 1000.0 if value > 1e12 else value)
        s = str(value).strip()
        if s.isdigit():
            return parse_reset(int(s))
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(round(dt.timestamp()))
    except Exception:
        return None


def parse_windows(data: dict) -> dict:
    out: dict = {}
    if not isinstance(data, dict):
        return out
    candidates = [data] + [data[k] for k in ("usage", "rate_limits", "limits", "data") if isinstance(data.get(k), dict)]
    for key in LIMIT_IDS:
        for cand in candidates:
            w = cand.get(key)
            if not isinstance(w, dict):
                continue
            pct = None
            for pk in ("utilization", "percentage", "percent", "used_pct", "used_percentage", "pct"):
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
                    reset = parse_reset(w[rk])
                    if reset:
                        break
            primary = key in ("five_hour", "seven_day")
            if pct is not None and (primary or reset is not None or pct > 0):
                out[key] = {"pct": pct, "resets_at": reset}
            break
    return out


def parse_extra(data: dict):
    """Overage credits in currency units. Prefers the `spend` block."""
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


def _get_json(url: str, token: str, timeout: int):
    headers = dict(BASE_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def fetch_usage(timeout: int) -> FetchResult:
    token, tstate = read_token()
    if tstate == TokenState.MISSING:
        return FetchResult(False, error="No Claude login found", token_state=tstate)
    if tstate == TokenState.EXPIRED:
        return FetchResult(False, error="Login expired", token_state=tstate)
    try:
        data = _get_json(USAGE_URL, token, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return FetchResult(False, error=f"Auth rejected (HTTP {exc.code})", token_state=TokenState.EXPIRED)
        if exc.code == 429:
            return FetchResult(False, error="Rate limited by the API (429)")
        return FetchResult(False, error=f"HTTP {exc.code}")
    except Exception as exc:
        return FetchResult(False, error=f"{type(exc).__name__}: {exc}")
    return FetchResult(True, windows=parse_windows(data), extra=parse_extra(data), raw=data)


def fetch_profile_org(timeout: int = 10):
    """The logged-in account's organisation uuid, or None."""
    token, tstate = read_token()
    if not token or tstate != TokenState.OK:
        return None
    try:
        return ((_get_json(PROFILE_URL, token, timeout) or {}).get("organization") or {}).get("uuid")
    except Exception:
        return None


def read_statusline():
    """The `--statusline` snapshot: {"ts": reading time, "windows": {...}, "sessions": {...}} or None."""
    d = io.load_json(paths.STATUSLINE_PATH, None)
    if not isinstance(d, dict):
        return None
    windows = {}
    for key, w in (d.get("rate_limits") or {}).items():
        if key not in ("five_hour", "seven_day") or not isinstance(w, dict):
            continue
        pct = _coerce_pct(w.get("used_percentage"))
        if pct is not None:
            windows[key] = {"pct": pct, "resets_at": parse_reset(w.get("resets_at"))}
    ts = d.get("rl_ts")
    sessions = d.get("sessions") if isinstance(d.get("sessions"), dict) else {}
    return {"ts": float(ts) if isinstance(ts, (int, float)) else None, "windows": windows, "sessions": sessions}
