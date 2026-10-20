"""`--statusline`: Claude Code's statusLine command. Fast, stdlib-only.

Claude Code pipes session JSON to it on every assistant message (debounced 300 ms), including
`rate_limits.five_hour/seven_day.{used_percentage, resets_at}` for Pro/Max accounts
(https://code.claude.com/docs/en/statusline). We merge that into a small snapshot the tray
watches (paths.STATUSLINE_PATH), then print a one-line status for the terminal."""
from __future__ import annotations

import json
import os
import sys
import time

from ctu import paths

MAX_SESSIONS = 16
SESSION_TTL = 6 * 3600
SAME_WINDOW_S = 300          # resets_at jitter tolerated within one window


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def merge(old, d: dict, now: float) -> dict:
    """New snapshot from the previous one and one statusline payload (pure).

    Rate limits are account-wide but each session reports its own last API response, so an idle
    session can carry older numbers: within one window usage only grows (keep the max), and a
    payload from an older window is ignored."""
    snap = old if isinstance(old, dict) and old.get("v") == 1 else {"v": 1, "rate_limits": {}, "sessions": {}}
    rl_old = snap.get("rate_limits") or {}
    rl_new = (d.get("rate_limits") or {}) if isinstance(d.get("rate_limits"), dict) else {}
    out_rl, accepted = {}, False
    for key in ("five_hour", "seven_day"):
        o, n = rl_old.get(key), rl_new.get(key)
        o_ok = isinstance(o, dict) and _num(o.get("resets_at")) and o["resets_at"] > now
        if isinstance(n, dict) and _num(n.get("used_percentage")) is not None and _num(n.get("resets_at")):
            n = {"used_percentage": float(n["used_percentage"]), "resets_at": int(n["resets_at"])}
            if not o_ok or n["resets_at"] > o["resets_at"] + SAME_WINDOW_S:
                out_rl[key], accepted = n, True                  # first reading or a newer window
            elif abs(n["resets_at"] - o["resets_at"]) <= SAME_WINDOW_S:
                out_rl[key] = {"used_percentage": max(o["used_percentage"], n["used_percentage"]),
                               "resets_at": o["resets_at"]}
                accepted = True
            else:
                out_rl[key] = o                                  # an idle session's older window
        elif o_ok:
            out_rl[key] = o
    snap["rate_limits"] = out_rl
    if accepted:
        snap["rl_ts"] = int(now)

    sid = d.get("session_id")
    if isinstance(sid, str) and sid:
        cw = d.get("context_window") if isinstance(d.get("context_window"), dict) else {}
        model = d.get("model") if isinstance(d.get("model"), dict) else {}
        ws = d.get("workspace") if isinstance(d.get("workspace"), dict) else {}
        sessions = {k: v for k, v in (snap.get("sessions") or {}).items()
                    if isinstance(v, dict) and now - (v.get("ts") or 0) < SESSION_TTL}
        sessions[sid] = {"ts": int(now), "model": model.get("id"),
                         "cwd": ws.get("current_dir") or d.get("cwd"),
                         "window": _num(cw.get("context_window_size")),
                         "used_pct": _num(cw.get("used_percentage")),
                         "tokens": _num(cw.get("total_input_tokens"))}
        if len(sessions) > MAX_SESSIONS:
            keep = sorted(sessions, key=lambda k: sessions[k]["ts"], reverse=True)[:MAX_SESSIONS]
            sessions = {k: sessions[k] for k in keep}
        snap["sessions"] = sessions
    snap["ts"] = int(now)
    return snap


def _clock(ts) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(ts))
    except Exception:
        return "?"


def format_line(d: dict, snap: dict) -> str:
    """e.g. `Opus · ctx 73% · 5h 64% ↻16:40 · 7d 41%`."""
    parts = []
    model = d.get("model") if isinstance(d.get("model"), dict) else {}
    if model.get("display_name"):
        parts.append(str(model["display_name"]))
    cw = d.get("context_window") if isinstance(d.get("context_window"), dict) else {}
    if _num(cw.get("used_percentage")) is not None:
        parts.append(f"ctx {cw['used_percentage']:.0f}%")
    rl = snap.get("rate_limits") or {}
    if "five_hour" in rl:
        parts.append(f"5h {rl['five_hour']['used_percentage']:.0f}% ↻{_clock(rl['five_hour']['resets_at'])}")
    if "seven_day" in rl:
        parts.append(f"7d {rl['seven_day']['used_percentage']:.0f}%")
    return " · ".join(parts)


def _read_stdin() -> bytes:
    try:
        if sys.stdin is not None:
            return sys.stdin.buffer.read()
    except Exception:
        pass
    chunks = []
    try:                                     # windowed builds can lack sys.stdin; fd 0 is the pipe
        while True:
            b = os.read(0, 65536)
            if not b:
                break
            chunks.append(b)
    except Exception:
        pass
    return b"".join(chunks)


def _write_stdout(s: str) -> None:
    data = (s + "\n").encode("utf-8", errors="replace")
    try:
        if sys.stdout is not None:
            sys.stdout.buffer.write(data)
            sys.stdout.flush()
            return
    except Exception:
        pass
    try:
        os.write(1, data)
    except Exception:
        pass


def run() -> int:
    try:
        d = json.loads(_read_stdin().decode("utf-8", errors="replace") or "{}")
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    now = time.time()
    p = paths.STATUSLINE_PATH
    try:
        old = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        old = None
    snap = merge(old, d, now)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(snap, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        pass
    _write_stdout(format_line(d, snap))
    return 0
