"""config.json: internal knobs + user settings (typed, validated for PATCH /api/v2/settings).

Unknown keys in an existing file are ignored, and only known keys are written back."""
from __future__ import annotations

import copy
import re
import threading

from ctu import io, paths

ALERT_KINDS = ("will_hit", "limited", "refilled", "context_high", "overage_high",
               "session_waiting", "needs_approval", "incident")
OVERLAY_FIELDS = ("5h", "7d", "opus", "sonnet", "ctx", "waiting", "status", "project", "extra")
DEFAULT_RELAY = "https://claude-usage-relay.businessofzeus.workers.dev"

# Internal knobs: config.json only, never PATCHable.
INTERNAL = {
    "tick_seconds": 10,                    # recompute / staleness / time-based alerts
    "statusline_stale_seconds": 300,       # statusline older than this -> fall back to the API
    "api_extras_interval_seconds": 1800,   # overage + scoped weekly windows while it's fresh
    "status_interval_seconds": 300,
    "request_timeout_seconds": 20,
    "sessions_top_n": 12,
    "alltime_top_n": 8,
    "dashboard_port": 8787,
    "remote_sync_seconds": 10,             # min gap between relay pushes
    "overlay_pos": None,                   # [x, y] physical px, remembered after a move
    "overlay_size": None,                  # [w, h] logical px, last overlay_resize
}

# User settings: GET/PATCH /api/v2/settings.
SETTINGS = {
    "alerts": {k: k != "incident" for k in ALERT_KINDS},   # incidents: opt-in (informational)
    "quiet_hours": {"enabled": False, "start": "22:00", "end": "08:00"},
    "phone_push": True,
    "status_components": [],
    "overlay": {"shown": True, "pinned": False, "click_through": False, "opacity": 100,
                "auto_hide_fullscreen": True, "fields": ["5h", "7d", "ctx", "waiting"],
                "tray_icon": "bars"},                # tray: "bars" (5-hour | weekly) or "number"
    "remote_enabled": False,
    "remote_relay_url": DEFAULT_RELAY,
    "remote_transcript": False,
    "team_tz": "Europe/Athens",
    "session_hook": False,
    "update_check": True,
    "api_fallback_interval_seconds": 300,  # API poll gap while the statusline is quiet (data.refresh_s)
}

DEFAULTS = {**INTERNAL, **SETTINGS}

_B = {"type": "bool"}
SCHEMA = {
    "alerts": {"type": "object", "fields": {k: _B for k in ALERT_KINDS}},
    "quiet_hours": {"type": "object", "fields": {"enabled": _B, "start": {"type": "hhmm"},
                                                 "end": {"type": "hhmm"}}},
    "phone_push": _B,
    "status_components": {"type": "list", "max": 50, "item": {"type": "str", "max": 100}},
    "overlay": {"type": "object", "fields": {
        "shown": _B, "pinned": _B, "click_through": _B,
        "opacity": {"type": "int", "min": 30, "max": 100},
        "auto_hide_fullscreen": _B,
        "tray_icon": {"type": "enum", "values": ["bars", "number"]},
        "fields": {"type": "list", "max": len(OVERLAY_FIELDS), "unique": True,
                   "item": {"type": "enum", "values": list(OVERLAY_FIELDS)}}}},
    "remote_enabled": _B,
    "remote_relay_url": {"type": "url"},
    "remote_transcript": _B,
    "team_tz": {"type": "tz"},
    "session_hook": _B,
    "update_check": _B,
    "api_fallback_interval_seconds": {"type": "int", "min": 30, "max": 3600},
}

# The public settings object (GET/PATCH /api/v2/settings) groups some flat config keys.
API_DIRECT = ("alerts", "quiet_hours", "phone_push", "status_components", "overlay")
API_MAP = {("remote", "enabled"): "remote_enabled", ("remote", "relay_url"): "remote_relay_url",
           ("remote", "transcript"): "remote_transcript", ("team", "tz"): "team_tz",
           ("data", "refresh_s"): "api_fallback_interval_seconds", ("data", "session_hook"): "session_hook",
           ("about", "update_check"): "update_check"}
API_ACTIONS = {("data", "statusline_installed")}           # bool: install / remove, not stored
API_READ_ONLY = {("about", "version")}
_API_PATH = {v: f"{g}.{k}" for (g, k), v in API_MAP.items()}
API_SCHEMA = {**{k: SCHEMA[k] for k in API_DIRECT},
              "remote": {"type": "object", "fields": {"enabled": _B, "relay_url": {"type": "url"}, "transcript": _B}},
              "team": {"type": "object", "fields": {"tz": {"type": "tz"}}},
              "data": {"type": "object", "fields": {"refresh_s": SCHEMA["api_fallback_interval_seconds"],
                                                    "session_hook": _B, "statusline_installed": _B}},
              "about": {"type": "object", "fields": {"update_check": _B}}}

LOCK = threading.RLock()   # serialises config read-modify-write (the engine is the only writer)

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_TZ = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+\-]+){0,2}$|^UTC$")


def _check(spec: dict, v, path: str, errors: dict):
    """Validated value, or None with errors[path] set."""
    t = spec["type"]
    if t == "bool":
        if isinstance(v, bool):
            return v
        errors[path] = "must be true or false"
    elif t == "int":
        if isinstance(v, int) and not isinstance(v, bool) and spec["min"] <= v <= spec["max"]:
            return v
        errors[path] = f"must be an integer {spec['min']}..{spec['max']}"
    elif t == "str":
        if isinstance(v, str) and len(v) <= spec["max"]:
            return v
        errors[path] = f"must be a string of at most {spec['max']} characters"
    elif t == "enum":
        if v in spec["values"]:
            return v
        errors[path] = "must be one of " + ", ".join(spec["values"])
    elif t == "hhmm":
        if isinstance(v, str) and _HHMM.match(v):
            return v
        errors[path] = "must be HH:MM (24 h)"
    elif t == "url":
        if isinstance(v, str) and len(v) <= 300 and re.match(r"^https://[A-Za-z0-9.\-]+(:\d+)?(/[^\s]*)?$", v):
            return v.rstrip("/")
        errors[path] = "must be an https:// URL"
    elif t == "tz":
        if isinstance(v, str) and len(v) <= 64 and _TZ.match(v) and _tz_ok(v):
            return v
        errors[path] = "must be an IANA time zone such as Europe/Athens"
    elif t == "list":
        if not isinstance(v, list) or len(v) > spec["max"]:
            errors[path] = f"must be a list of at most {spec['max']} items"
            return None
        out = []
        for i, item in enumerate(v):
            c = _check(spec["item"], item, f"{path}[{i}]", errors)
            if c is not None:
                out.append(c)
        if spec.get("unique") and len(set(out)) != len(out):
            errors[path] = "items must be unique"
        return out
    elif t == "object":
        if not isinstance(v, dict):
            errors[path] = "must be an object"
            return None
        out = {}
        for k, sub in v.items():
            if k not in spec["fields"]:
                errors[f"{path}.{k}"] = "unknown setting"
                continue
            c = _check(spec["fields"][k], sub, f"{path}.{k}", errors)
            if c is not None:
                out[k] = c
        return out
    return None


def _tz_ok(name: str) -> bool:
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return True
    except ImportError:
        return True
    except Exception:
        # No tz database (Windows without tzdata): accept a well-formed name; team_now() falls back.
        try:
            import zoneinfo
            return not zoneinfo.available_timezones()
        except Exception:
            return True


def validate_patch(patch) -> tuple[dict, dict]:
    """(clean_patch, errors). A patch is applied only when errors is empty."""
    errors: dict = {}
    if not isinstance(patch, dict):
        return {}, {"": "body must be a JSON object"}
    clean = {}
    for k, v in patch.items():
        if k not in SCHEMA:
            errors[k] = "unknown setting"
            continue
        c = _check(SCHEMA[k], v, k, errors)
        if c is not None:
            clean[k] = c
    return clean, errors


def merge(cfg: dict, clean: dict) -> dict:
    """cfg with a validated patch applied (objects merge one level deep)."""
    out = copy.deepcopy(cfg)
    for k, v in clean.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def settings_view(cfg: dict, *, version: str = "", statusline_installed: bool = False,
                  components: list | None = None) -> dict:
    """The public settings object (read-only parts: status_components_available, about.version,
    data.statusline_installed reflects ~/.claude/settings.json)."""
    g = lambda k: copy.deepcopy(cfg.get(k, DEFAULTS[k]))      # noqa: E731
    return {**{k: g(k) for k in API_DIRECT},
            "status_components_available": list(components or []),
            "remote": {"enabled": g("remote_enabled"), "relay_url": g("remote_relay_url"), "transcript": g("remote_transcript")},
            "team": {"tz": g("team_tz")},
            "data": {"refresh_s": g("api_fallback_interval_seconds"), "session_hook": g("session_hook"),
                     "statusline_installed": bool(statusline_installed)},
            "about": {"version": version, "update_check": g("update_check")}}


def api_patch(patch) -> tuple[dict, dict, dict]:
    """A grouped PATCH body -> (flat config patch, requested actions, errors in API paths).
    Read-only keys are ignored; anything unknown is an error."""
    if not isinstance(patch, dict):
        return {}, {}, {"": "body must be a JSON object"}
    flat, acts, errors = {}, {}, {}
    for grp, v in patch.items():
        if grp in API_DIRECT:
            flat[grp] = v
        elif grp == "status_components_available":
            continue
        elif grp in ("remote", "team", "data", "about"):
            if not isinstance(v, dict):
                errors[grp] = "must be an object"
                continue
            for k, x in v.items():
                if (grp, k) in API_MAP:
                    flat[API_MAP[(grp, k)]] = x
                elif (grp, k) in API_ACTIONS:
                    if isinstance(x, bool):
                        acts[k] = x
                    else:
                        errors[f"{grp}.{k}"] = "must be true or false"
                elif (grp, k) not in API_READ_ONLY:
                    errors[f"{grp}.{k}"] = "unknown setting"
        else:
            errors[grp] = "unknown setting"
    clean, errs = validate_patch(flat)
    for path, msg in errs.items():
        head = path.split(".")[0].split("[")[0]
        errors[_API_PATH.get(head, head) + path[len(head):]] = msg
    return clean, acts, errors


def _migrate(raw: dict) -> dict:
    """Map <= 0.3 keys onto the v2 settings (the old keys are then dropped)."""
    out = {}
    if raw.get("notify_session_waiting") is True:
        out["session_hook"] = True
    if "show_widget_on_start" in raw or "show_bar_on_start" in raw:
        out["overlay"] = {"shown": bool(raw.get("show_widget_on_start") or raw.get("show_bar_on_start"))}
    if raw.get("predictive_alerts") is False:
        out["alerts"] = {"will_hit": False, "context_high": False, "overage_high": False}
    return out


def _coerce(raw: dict) -> dict:
    """Defaults overlaid with the file's known, well-typed values."""
    cfg = copy.deepcopy(DEFAULTS)
    for k, v in INTERNAL.items():
        if k in raw:
            rv = raw[k]
            if v is None or type(rv) is type(v) or (isinstance(v, int) and isinstance(rv, int) and not isinstance(rv, bool)):
                cfg[k] = rv
    legacy = _migrate(raw)
    for src in (legacy, raw):                       # v2 keys override what was migrated
        for k in SETTINGS:
            if k in src:
                clean, _errors = validate_patch({k: src[k]})   # bad parts dropped
                if k in clean:
                    cfg = merge(cfg, clean)
    return cfg


def load() -> dict:
    with LOCK:
        raw = io.load_json(paths.CONFIG_PATH, None)
        if not isinstance(raw, dict):
            cfg = copy.deepcopy(DEFAULTS)
            if raw is None and not paths.CONFIG_PATH.exists():
                save(cfg)
            return cfg
        return _coerce(raw)


def save(cfg: dict) -> bool:
    with LOCK:
        return io.save_json(paths.CONFIG_PATH, {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}, pretty=True)
