"""Alerts (spec §4): one alert per episode, muting, quiet hours. Pure bookkeeping — the engine
supplies the current conditions; this decides which alerts are live and which to deliver."""
from __future__ import annotations

import time

SEVERITY = {"will_hit": "warning", "limited": "critical", "refilled": "info", "context_high": "warning",
            "overage_high": "warning", "session_waiting": "info", "needs_approval": "warning", "incident": "info"}
PHONE_PUSH = {"will_hit", "limited", "refilled", "overage_high", "session_waiting", "needs_approval"}
MAX_ALERTS = 20


def cond(kind: str, key: str, discriminator, title: str, body: str) -> dict:
    """A live condition. `key` names the episode stream (e.g. "will_hit:five_hour"); the
    discriminator (usually the window's resets_at) makes the alert id."""
    return {"kind": kind, "key": key, "id": f"{key}:{discriminator}" if discriminator is not None else key,
            "title": title, "body": body}


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def quiet_until(q: dict, now: float):
    """End of the current quiet-hours span (epoch s), or None when outside it / disabled."""
    if not q or not q.get("enabled"):
        return None
    try:
        start, end = _mins(q["start"]), _mins(q["end"])
    except Exception:
        return None
    if start == end:
        return None
    lt = time.localtime(now)
    cur = lt.tm_hour * 60 + lt.tm_min
    inside = (start <= cur < end) if start < end else (cur >= start or cur < end)
    if not inside:
        return None
    left = (end - cur) % 1440
    return int(now - lt.tm_sec + left * 60)


class AlertBook:
    def __init__(self, persisted=None):
        p = persisted if isinstance(persisted, dict) else {}
        self.live: dict = {a["id"]: a for a in p.get("live", []) if isinstance(a, dict) and a.get("id")}
        self.ended: dict = dict(p.get("ended") or {})       # id -> times an episode with this id ended
        m = p.get("mute") if isinstance(p.get("mute"), dict) else {}
        self.mute_until = m.get("until")
        self.mute_reason = m.get("reason")

    def to_json(self) -> dict:
        ended = dict(list(self.ended.items())[-200:])
        return {"live": list(self.live.values()), "ended": ended,
                "mute": {"until": self.mute_until, "reason": self.mute_reason}}

    # ---- mute ----
    def set_mute(self, how: str, now: float, refill_at=None) -> None:
        if how == "off":
            self.mute_until, self.mute_reason = None, None
        elif how == "hour":
            self.mute_until, self.mute_reason = int(now + 3600), "hour"
        elif how == "refill":
            self.mute_until, self.mute_reason = int(refill_at or now + 3600), "refill"

    def mute_state(self, now: float, quiet: dict | None) -> dict:
        if self.mute_until and self.mute_until <= now:
            self.mute_until, self.mute_reason = None, None
        if self.mute_until:
            return {"until": self.mute_until, "reason": self.mute_reason}
        q = quiet_until(quiet, now)
        if q:
            return {"until": q, "reason": "quiet_hours"}
        return {"until": None, "reason": None}

    # ---- episodes ----
    def evaluate(self, conds: list, now: float, muted: bool, enabled: dict) -> list:
        """Update the live set from the current conditions. Returns the alerts that started an
        episode now and should be delivered (enabled, not muted)."""
        current = {}
        for c in conds:
            if not enabled.get(c["kind"], True):
                continue
            current[c["key"]] = c
        new = []
        live_by_key = {a["key"]: a for a in self.live.values()}
        for key, a in list(live_by_key.items()):
            c = current.get(key)
            if c is None or c["id"] != a["base_id"]:           # condition gone or a new window: episode over
                self.live.pop(a["id"], None)
                self.ended[a["base_id"]] = self.ended.get(a["base_id"], 0) + 1
            else:
                a["title"], a["body"] = c["title"], c["body"]  # same episode: keep text current
        live_keys = {a["key"] for a in self.live.values()}
        for key, c in current.items():
            if key in live_keys:
                continue
            n = self.ended.get(c["id"], 0)
            aid = c["id"] if n == 0 else f"{c['id']}#{n + 1}"
            a = {"id": aid, "base_id": c["id"], "key": key, "kind": c["kind"],
                 "severity": SEVERITY.get(c["kind"], "info"), "title": c["title"], "body": c["body"],
                 "at": int(now), "muted": bool(muted)}
            self.live[aid] = a
            if not muted:
                new.append(a)
        return new

    def view(self) -> list:
        """state.alerts: newest first."""
        out = [{k: a[k] for k in ("id", "kind", "severity", "title", "body", "at", "muted")}
               for a in self.live.values()]
        out.sort(key=lambda a: a["at"], reverse=True)
        return out[:MAX_ALERTS]
