"""Generate the UI fixtures in tests/ui/fixtures/ (deterministic).

    python tests/ui/make_fixtures.py

state-will_hit.json is a verbatim copy of docs/design/state-v2.example.json;
every other state variant is derived from it. Times are epoch seconds around
2026-10-20 14:48 Europe/Athens (UTC+3), the example's "now".

The alltime / settings / ledger fixtures define the response shapes the UI
expects from the engine (see the README notes inside each file's "_doc").
"""

from __future__ import annotations

import copy
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "fixtures"
EXAMPLE = ROOT / "docs" / "design" / "state-v2.example.json"

NOW = 1792496880  # 2026-10-20 14:48 Athens
ATHENS = timezone(timedelta(hours=3))


def dump(name: str, data) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def lim(state, lid):
    return next(x for x in state["limits"] if x["id"] == lid)


def states(base: dict) -> dict:
    out = {"will_hit": copy.deepcopy(base)}

    # ---- ok: 38% used, slow pace
    s = copy.deepcopy(base)
    s["rev"] = 1301
    f = lim(s, "five_hour")
    f.update(used_pct=38.0, level="ok", binding=True)
    f["pace"] = {"rate_pct_h": 6.0, "sustainable_pct_h": 33.2, "projected_at_reset": 49.2, "eta_100": None, "confidence": "ok"}
    s["decision"] = {"level": "ok", "binding": "five_hour", "title": "On pace", "detail": "5-hour 38% · refills 16:40.", "resume_at": None, "suggestion": None}
    s["alerts"] = []
    s["team"]["pool"][1].update(five_hour_pct=38.0, level="ok")
    s["overage"]["used"], s["overage"]["pct"] = 0.0, 0.0
    s["team"]["pool"][1]["month_spend"] = 0.0
    out["ok"] = s

    # ---- watch: about 92% by the refill
    s = copy.deepcopy(base)
    s["rev"] = 1302
    f = lim(s, "five_hour")
    f.update(used_pct=71.0, level="watch")
    f["pace"] = {"rate_pct_h": 11.0, "sustainable_pct_h": 15.5, "projected_at_reset": 91.5, "eta_100": None, "confidence": "ok"}
    s["decision"].update(level="watch", title="About 92% by 16:40", detail="Close to the 5-hour limit at this pace.")
    s["decision"]["suggestion"] = {"email": "ops@acme.dev", "five_hour_left_pct": 82, "label": "Switch to ops@acme.dev — 82% left"}
    s["alerts"] = []
    s["team"]["pool"][1].update(five_hour_pct=71.0, level="watch")
    out["watch"] = s

    # ---- limited: 5-hour at 100%, back at 15:02 (in 14 min)
    s = copy.deepcopy(base)
    s["rev"] = 1303
    reset = NOW + 14 * 60
    f = lim(s, "five_hour")
    f.update(used_pct=100.0, level="limited", resets_at=reset, elapsed_pct=round(100 * (18000 - 840) / 18000, 1))
    f["pace"] = {"rate_pct_h": None, "sustainable_pct_h": None, "projected_at_reset": None, "eta_100": None, "confidence": "ok"}
    lim(s, "seven_day")["used_pct"] = 44.0
    s["overage"].update(used=14.8, pct=29.6)
    s["decision"].update(level="limited", title="5-hour limit reached", detail="Back at 15:02. Extra usage is on: €14.80 of €50.00 used.", resume_at=reset)
    s["alerts"] = [{"id": f"limited:five_hour:{reset}", "kind": "limited", "severity": "critical", "title": "5-hour limit reached", "body": "Back at 15:02.", "at": NOW - 17 * 60, "muted": False}]
    s["team"]["pool"][1].update(five_hour_pct=100.0, level="limited", five_hour_resets_at=reset, month_spend=14.8)
    s["sessions"][0]["state"] = "waiting"
    out["limited"] = s

    # ---- stale: last reading 14:31, fetch failing
    s = copy.deepcopy(base)
    s["rev"] = 1304
    s["source"].update(updated_at=NOW - 17 * 60, age_s=17 * 60, stale=True, error="Timed out reaching api.anthropic.com")
    lim(s, "five_hour")["used_pct"] = 61.0
    lim(s, "seven_day")["used_pct"] = 40.0
    s["team"]["pool"][1].update(five_hour_pct=61.0, seven_day_pct=40.0, updated_at=NOW - 17 * 60)
    out["stale"] = s

    # ---- gathering: a window that started 18 min ago
    s = copy.deepcopy(base)
    s["rev"] = 1305
    reset = NOW + 4 * 3600 + 42 * 60  # 19:30
    f = lim(s, "five_hour")
    f.update(used_pct=4.0, level="ok", resets_at=reset, elapsed_pct=round(100 * (18000 - (reset - NOW)) / 18000, 1))
    f["pace"] = {"rate_pct_h": None, "sustainable_pct_h": 20.4, "projected_at_reset": None, "eta_100": None, "confidence": "gathering"}
    s["decision"] = {"level": "ok", "binding": "five_hour", "title": "Gathering pace", "detail": "5-hour 4% · refills 19:30.", "resume_at": None, "suggestion": None}
    s["alerts"] = []
    s["sessions"] = [s["sessions"][0]]
    s["sessions"][0]["context"] = {"pct": 12.0, "tokens": 24000, "window": 200000}
    s["team"]["pool"][1].update(five_hour_pct=4.0, level="ok", five_hour_resets_at=reset)
    out["gathering"] = s

    # ---- signed_out: no team pool
    s = copy.deepcopy(base)
    s["rev"] = 1306
    s["team"] = None
    s["decision"]["suggestion"] = None
    out["signed_out"] = s

    # ---- empty: no sessions, no extra usage, nobody in the pool yet
    s = copy.deepcopy(out["ok"])
    s["rev"] = 1307
    s["sessions"] = []
    s["overage"] = None
    s["team"]["pool"] = []
    s["team"]["role"] = "member"
    out["empty"] = s
    return out


def five_hour_history() -> list:
    start = 1792503600 - 18000  # 11:40
    keys = [(0, 0), (12, 3), (30, 7), (60, 7), (85, 18), (110, 30), (130, 38), (145, 41), (158, 53), (188, 64)]
    rnd = random.Random(5)
    pts, last = [], 0.0
    for t in range(start, NOW + 1, 120):
        m = (t - start) / 60
        for (a, va), (b, vb) in zip(keys, keys[1:]):
            if a <= m <= b:
                v = va + (vb - va) * (m - a) / (b - a)
                break
        else:
            v = keys[-1][1]
        v = max(last, round(v + rnd.uniform(-0.3, 0.3), 1))
        last = v
        pts.append([t, v])
    pts[-1] = [NOW, 64.0]
    return pts


def seven_day_history() -> list:
    start = 1792648800 - 604800  # Thu 15 Oct 09:00
    rnd = random.Random(7)
    hours = list(range(start, NOW + 1, 3600))
    w = []
    for t in hours:
        dt = datetime.fromtimestamp(t, ATHENS)
        work = dt.weekday() < 5 and 9 <= dt.hour < 19
        w.append((1.0 + rnd.random()) if work else (0.15 * rnd.random() if dt.weekday() < 5 else 0.05 * rnd.random()))
    total = sum(w)
    acc, pts = 0.0, []
    for t, x in zip(hours, w):
        acc += x / total * 41.0
        pts.append([t, round(acc, 2)])
    pts.append([NOW, 41.0])
    return pts


def alltime() -> dict:
    rnd = random.Random(11)
    today = datetime.fromtimestamp(NOW, ATHENS).date()
    first = today - timedelta(days=150)
    # per-day tokens
    days = {}
    d = first
    while d <= today:
        wd = d.weekday()
        p_active = 0.86 if wd < 5 else 0.25
        tok = 0
        if rnd.random() < p_active:
            tok = int(rnd.lognormvariate(14.2, 0.7))
        days[d.isoformat()] = tok
        d += timedelta(days=1)
    days[today.isoformat()] = 2_310_000
    # heatmap: Sunday-aligned ~53 weeks ending today
    floor = today - timedelta(days=371)
    floor -= timedelta(days=(floor.weekday() + 1) % 7)
    vis = [days.get((floor + timedelta(days=i)).isoformat(), 0) for i in range((today - floor).days + 1)]
    mx = max(vis)
    hm = []
    for i, tok in enumerate(vis):
        lvl = 0
        if tok > 0:
            f = tok / mx
            lvl = 1 if f <= .25 else 2 if f <= .5 else 3 if f <= .75 else 4
        hm.append({"d": (floor + timedelta(days=i)).isoformat(), "tok": tok, "lvl": lvl})
    # 30-day usage samples (hourly), 5-hour sawtooth + weekly ramp
    t0 = NOW - 30 * 86400
    t0 -= t0 % 3600
    ts, f5, f7 = [], [], []
    week_reset = 1792648800
    while week_reset - 604800 > t0:
        week_reset -= 604800
    used7 = 0.0
    used5, win_end = 0.0, None
    t = t0
    while t <= NOW:
        dt = datetime.fromtimestamp(t, ATHENS)
        if t >= week_reset:
            week_reset += 604800
            used7 = 0.0
        work = dt.weekday() < 5 and 9 <= dt.hour < 20
        off = dt.weekday() >= 5 and dt.hour % 7 == 0 and rnd.random() < 0.5
        if win_end is not None and t >= win_end:
            used5, win_end = 0.0, None
        if work and rnd.random() < 0.85:
            if win_end is None:
                win_end = t + 5 * 3600
            inc = rnd.uniform(4, 19)
            used5 = min(100.0, used5 + inc)
            used7 = min(100.0, used7 + inc * 0.085)
        # tracker not running on Saturday nights (gap)
        if not (dt.weekday() == 5 and dt.hour >= 18) and not off:
            ts.append(t)
            f5.append(round(used5, 1))
            f7.append(round(used7, 1))
        t += 3600
    ts.append(NOW)
    f5.append(64.0)
    f7.append(41.0)

    cut = (today - timedelta(days=29)).isoformat()
    rng_days = {k: v for k, v in days.items() if k >= cut}
    tokens_30 = sum(rng_days.values())
    models = [("Opus 5.5", 0.58), ("Sonnet 5.5", 0.33), ("Haiku 4.5", 0.09)]
    model_rows = []
    for name, share in models:
        tok = int(tokens_30 * share)
        model_rows.append({"name": name, "in": int(tok * 0.18), "out": int(tok * 0.07), "tokens": tok, "share": round(share * 100, 1)})
    projects = [("claude-usage-tracker", 0.33), ("acme-api", 0.22), ("infra-terraform", 0.12), ("docs-site", 0.09), ("mobile-app", 0.07), ("data-pipeline", 0.06), ("design-system", 0.04), ("scratch", 0.03)]
    proj_rows = [{"name": n, "tokens": int(tokens_30 * s), "share": round(s * 100, 1)} for n, s in projects]
    series = []
    for i in range(29, -1, -1):
        day = today - timedelta(days=i)
        tok = days.get(day.isoformat(), 0)
        segs = [{"name": n, "tok": int(tok * s)} for n, s in models if tok]
        series.append({"label": day.strftime("%b ") + str(day.day), "date": day.isoformat(), "total": tok, "segs": segs})
    active = sum(1 for v in rng_days.values() if v > 0)
    lifetime = sum(days.values())
    return {
        "_doc": (
            "Response of GET /api/v2/alltime?range=7d|30d|all (this file: range=30d). Mirrors the old "
            "_alltime_display() field names. Range-scoped: tokens, messages, sessions, active_days, fav_model, "
            "models, projects (unless projects_scope='all'), series, compare, usage. Lifetime: total, first_ts, "
            "last_ts, streak_current, streak_longest, peak_hour, heatmap. tokens = input + output + cache writes "
            "(cache reads excluded). peak_hour is a local hour 0-23 (the old app sent '10 AM'). usage holds "
            "sampled 5-hour/weekly used % over the range (<= ~800 points, gaps where the tracker was off). "
            "heatmap.days is Sunday-aligned, ~53 weeks ending today, lvl 0-4 relative to the busiest visible day. "
            "ready=false while the first full scan runs."
        ),
        "ready": True,
        "range": "30d",
        "generated_at": NOW,
        "first_ts": int(datetime.combine(first, datetime.min.time(), ATHENS).timestamp()) + 9 * 3600,
        "last_ts": NOW - 40,
        "tokens": tokens_30,
        "messages": 2148,
        "sessions": 64,
        "active_days": active,
        "fav_model": "Opus 5.5",
        "streak_current": 2,
        "streak_longest": 12,
        "peak_hour": 10,
        "total": {"in": int(lifetime * 0.18), "out": int(lifetime * 0.07), "cw": int(lifetime * 0.75), "cr": int(lifetime * 6.2), "msgs": 7737},
        "compare": {"x": 170, "name": "the Harry Potter series"},
        "usage": {"t": ts, "five_hour": f5, "seven_day": f7},
        "series": series,
        "models": model_rows,
        "projects": proj_rows,
        "project_count": 10,
        "projects_scope": "range",
        "heatmap": {"days": hm, "max": mx},
    }


def settings() -> dict:
    return {
        "_doc": (
            "GET /api/v2/settings returns this object; PATCH takes any subset with the same nesting "
            "(e.g. {\"alerts\": {\"will_hit\": false}}) and returns the full validated object. "
            "status_components_available is read-only (optional). opacity: integer 30-100. "
            "refresh_s: seconds. overlay.fields: enabled fields, in display order."
        ),
        "alerts": {
            "will_hit": True, "limited": True, "refilled": True, "context_high": True,
            "overage_high": True, "session_waiting": True, "needs_approval": True, "incident": False,
        },
        "quiet_hours": {"enabled": False, "start": "22:00", "end": "08:00"},
        "status_components": ["claude.ai", "Claude Code", "Claude API (api.anthropic.com)"],
        "status_components_available": ["claude.ai", "Claude Code", "Claude API (api.anthropic.com)", "Claude Console (platform.claude.com)"],
        "overlay": {"fields": ["5h", "7d", "ctx", "waiting", "status"], "click_through": False, "opacity": 90, "auto_hide_fullscreen": True, "tray_icon": "bars"},
        "remote": {"enabled": True, "relay_url": "https://claude-usage-relay.businessofzeus.workers.dev", "transcript": False},
        "team": {"tz": "Europe/Athens"},
        "data": {"statusline_installed": True, "refresh_s": 60},
        "about": {"version": "0.4.0", "update_check": True},
    }


def ledger() -> dict:
    return {
        "_doc": (
            "Response of GET /api/v2/team/ledger?month=YYYY-MM. Engine computes everything; the UI only formats. "
            "current_month (team clock) caps the month picker. state: live | end_of_day | closed. "
            "totals: one entry per currency. On failure: {\"ok\": false, \"error\": \"unreachable\" | message}."
        ),
        "month": "2026-10",
        "current_month": "2026-10",
        "tz": "Europe/Athens",
        "updated_at": NOW - 60,
        "rows": [
            {"email": "design@acme.dev", "name": "Design", "spend": 31.2, "currency": "EUR", "tokens": 48_200_000, "meter_end": 31.2, "cap": 100.0, "days": 20, "state": "live", "is_me": False},
            {"email": "paris@acme.dev", "name": "Paris", "spend": 12.4, "currency": "EUR", "tokens": 39_800_000, "meter_end": 12.4, "cap": 50.0, "days": 19, "state": "live", "is_me": True},
            {"email": "ops@acme.dev", "name": "Ops", "spend": 0.0, "currency": "EUR", "tokens": 9_100_000, "meter_end": 0.0, "cap": None, "days": 11, "state": "end_of_day", "is_me": False},
        ],
        "totals": [{"currency": "EUR", "spend": 43.6}],
    }


def main() -> None:
    base = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    for name, s in states(base).items():
        dump(f"state-{name}.json", s)
    dump("history-five_hour.json", five_hour_history())
    dump("history-seven_day.json", seven_day_history())
    dump("alltime.json", alltime())
    dump("settings.json", settings())
    dump("ledger.json", ledger())
    print("fixtures written to", OUT)


if __name__ == "__main__":
    main()
