"""Forecast and decision rules (spec §3). Pure functions; all times are epoch seconds.

Usage inside one window only grows, so samples are a step series: the rate is a least-squares
slope over the trailing fit window (5 h: 30 min, weekly: 6 h), using samples after the last reset."""
from __future__ import annotations

import time

WINDOW_S = {"five_hour": 18000, "seven_day": 604800, "seven_day_opus": 604800, "seven_day_sonnet": 604800}
FIT_S = {"five_hour": 1800}            # weekly scopes: 6 h
MIN_SPAN_S = {"five_hour": 600}        # weekly scopes: 1 h
LABELS = {"five_hour": "5-hour", "seven_day": "Weekly", "seven_day_opus": "Weekly · Opus",
          "seven_day_sonnet": "Weekly · Sonnet"}
SCOPES = {"five_hour": "all", "seven_day": "all", "seven_day_opus": "opus", "seven_day_sonnet": "sonnet"}
ORDER = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")
LEVELS = ("ok", "watch", "will_hit", "limited")
RANK = {lv: i for i, lv in enumerate(LEVELS)}
SAME_WINDOW_S = 300                    # resets_at jitter within one window
WATCH_AT, HIT_AT, HYST = 80.0, 100.0, 5.0
SUGGEST_MARGIN = 20.0

# Short names used inside sentences ("Close to the 5-hour limit").
_NOUN = {"five_hour": "5-hour", "seven_day": "weekly", "seven_day_opus": "weekly Opus",
         "seven_day_sonnet": "weekly Sonnet"}


def fit_s(lid):
    return FIT_S.get(lid, 21600)


def min_span_s(lid):
    return MIN_SPAN_S.get(lid, 3600)


def lsq_slope(pts) -> float | None:
    """Ordinary least-squares slope of [(x, y)], or None if x has no spread."""
    n = len(pts)
    if n < 2:
        return None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    if sxx <= 0:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx


def window_samples(samples, resets_at, window_s, now):
    """Samples of the current window (after the last reset), oldest first."""
    start = (resets_at - window_s - SAME_WINDOW_S) if resets_at else float("-inf")
    return [(t, v) for (t, v) in samples if start <= t <= now + 1]


def fit_rate(lid, samples, resets_at, now):
    """(rate_pct_h | None, confidence). Needs >= 3 samples spanning >= 10 min (5 h) or 1 h (weekly)
    inside the fit window; negative slopes clamp to 0."""
    if not resets_at:
        return None, "gathering"                       # no live window (expired / not started)
    pts = window_samples(samples, resets_at, WINDOW_S[lid], now)
    pts = [(t, v) for (t, v) in pts if t >= now - fit_s(lid)]
    if len(pts) < 3 or pts[-1][0] - pts[0][0] < min_span_s(lid):
        return None, "gathering"
    t0 = pts[0][0]
    slope = lsq_slope([((t - t0) / 3600.0, v) for t, v in pts])
    if slope is None:
        return None, "gathering"
    span = pts[-1][0] - pts[0][0]
    conf = "low" if (len(pts) < 6 or span < 2 * min_span_s(lid)) else "ok"
    return max(0.0, slope), conf


def pace(lid, used, resets_at, samples, now) -> dict:
    """The `pace` block plus `elapsed_pct` for one limit."""
    window_s = WINDOW_S[lid]
    rate, conf = fit_rate(lid, samples, resets_at, now)
    out = {"rate_pct_h": None if rate is None else round(rate, 2), "sustainable_pct_h": None,
           "projected_at_reset": None, "eta_100": None, "confidence": conf}
    elapsed = None
    if resets_at and resets_at > now:
        left_s = resets_at - now
        hours = left_s / 3600.0
        elapsed = round(max(0.0, min(100.0, 100.0 * (window_s - left_s) / window_s)), 1)
        out["sustainable_pct_h"] = round(max(0.0, 100.0 - used) / hours, 2) if hours > 0 else None
        if rate is not None:
            out["projected_at_reset"] = round(used + rate * hours, 1)
            if rate > 0 and used < 100.0:
                eta = now + (100.0 - used) / rate * 3600.0
                if eta < resets_at:
                    out["eta_100"] = int(eta)
    return {"pace": out, "elapsed_pct": elapsed}


def raw_level(used, p: dict) -> str:
    if used >= 100.0:
        return "limited"
    if p.get("eta_100"):
        return "will_hit"
    proj = p.get("projected_at_reset")
    if proj is not None and WATCH_AT <= proj < HIT_AT:
        return "watch"
    if p.get("rate_pct_h") is None and used >= 90.0:
        return "watch"
    return "ok"


def apply_hysteresis(prev: str | None, raw: str, projected, used, reset: bool) -> str:
    """Drop from will_hit / watch only when the projection is >= 5 points below that level's
    threshold (100 / 80), or the window reset. Unknown projections use used % (a lower bound)."""
    if reset or prev is None or prev == "limited" or RANK[raw] >= RANK.get(prev, 0):
        return raw
    proj = projected if projected is not None else used
    if prev == "will_hit" and proj > HIT_AT - HYST:
        return "will_hit"
    if RANK[prev] >= RANK["watch"] and RANK[raw] < RANK["watch"] and proj > WATCH_AT - HYST:
        return "watch"
    return raw


def same_window(a, b) -> bool:
    return bool(a and b and abs(a - b) <= SAME_WINDOW_S)


def scope_of_model(model) -> str | None:
    m = str(model or "").lower()
    return "opus" if "opus" in m else "sonnet" if "sonnet" in m else None


def pick_binding(limits: list, active_scope: str | None) -> str | None:
    """Highest level; ties -> earliest eta_100, then highest used %. Scoped weekly limits bind
    only when the active model uses that scope."""
    elig = [l for l in limits if l["scope"] == "all" or l["scope"] == active_scope]
    if not elig:
        return None
    best = min(elig, key=lambda l: (-RANK[l["level"]], l["pace"]["eta_100"] or float("inf"), -l["used_pct"]))
    return best["id"]


def resume_at(limits: list, active_scope: str | None):
    elig = [l for l in limits if (l["scope"] == "all" or l["scope"] == active_scope) and l["level"] == "limited"]
    rs = [l["resets_at"] for l in elig if l["resets_at"]]
    return max(rs) if rs else None


# ---- strings ----

CLOCK_12H = False      # set by the engine from the Windows short-time format


def _hm(lt) -> str:
    if CLOCK_12H:
        return time.strftime("%I:%M %p", lt).lstrip("0")
    return time.strftime("%H:%M", lt)


def fmt_clock(ts, now=None) -> str:
    """'16:40' today, 'Fri 16:40' within a week, else 'Oct 24 16:40' (local time; '4:40 PM' when
    Windows uses a 12-hour clock)."""
    if not ts:
        return "?"
    now = now or time.time()
    lt, ln = time.localtime(ts), time.localtime(now)
    if (lt.tm_year, lt.tm_yday) == (ln.tm_year, ln.tm_yday):
        return _hm(lt)
    if 0 <= ts - now < 6.5 * 86400:
        return time.strftime("%a ", lt) + _hm(lt)
    return time.strftime("%b %d ", lt) + _hm(lt)


def fmt_dur(s) -> str:
    s = max(0, int(s or 0))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    if d:
        return f"{d} d {h} h" if h else f"{d} d"
    if h:
        return f"{h} h {m:02d} min" if m else f"{h} h"
    return f"{max(1, m)} min"


def fmt_rate(r) -> str:
    return f"{r:.0f}" if r >= 9.95 else f"{r:.1f}"


def fmt_money(v, cur) -> str:
    if v is None:
        return "?"
    sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(str(cur or "").upper())
    return f"{sym}{v:.2f}" if sym else f"{v:.2f} {cur}".strip()


def _subject(lid) -> str:
    return {"five_hour": "Limit", "seven_day": "Weekly limit", "seven_day_opus": "Weekly Opus limit",
            "seven_day_sonnet": "Weekly Sonnet limit"}[lid]


def texts(lim: dict | None, now: float, overage=None) -> tuple[str, str]:
    """(title, detail) for the binding limit."""
    if lim is None:
        return "Waiting for data", "No usage reading yet. It appears after your next Claude Code reply."
    lid, used, p, reset = lim["id"], lim["used_pct"], lim["pace"], lim["resets_at"]
    noun, at = _NOUN[lid], fmt_clock(reset, now)
    if lim["level"] == "limited":
        title = f"{LABELS[lid].replace(' · ', ' ')} limit reached"
        detail = f"Back at {at}." if reset else "Waiting for the window to reset."
        if isinstance(overage, dict) and overage.get("enabled") and (overage.get("pct") or 0) < 100:
            used_s = fmt_money(overage.get("used"), overage.get("currency"))
            lim_s = fmt_money(overage.get("limit"), overage.get("currency"))
            detail += f" Extra usage is on ({used_s} of {lim_s})." if overage.get("limit") else " Extra usage is on."
        return title, detail
    if lim["level"] == "will_hit" and p.get("eta_100"):
        title = f"{_subject(lid)} around {fmt_clock(p['eta_100'], now)}"
        detail = f"{fmt_dur(reset - p['eta_100'])} before the {at} refill."
        if p.get("sustainable_pct_h") is not None:
            detail += f" Slow to {fmt_rate(p['sustainable_pct_h'])}%/h to make it."
        return title, detail
    if lim["level"] in ("will_hit", "watch"):
        proj = p.get("projected_at_reset")
        if proj is not None:
            return f"About {min(proj, 99.9):.0f}% by {at}", f"Close to the {noun} limit at this pace."
        return f"{LABELS[lid]} at {used:.0f}%", f"Close to the {noun} limit. Refills {at}."
    detail = f"{LABELS[lid]} {used:.0f}%"
    detail += f" · refills {at}." if reset else "."
    return "On pace", detail


def suggestion(pool, my_used, level: str, now: float):
    """The pooled account with the most 5-hour headroom whose weekly limit isn't reached, if it has
    >= 20 points more headroom than this account (only while this account isn't on pace)."""
    if level == "ok" or not pool or my_used is None:
        return None
    best, best_left = None, None
    for a in pool:
        if a.get("is_me") or not a.get("email") or a.get("level") == "limited":
            continue
        sd = a.get("seven_day_pct")
        if isinstance(sd, (int, float)) and sd >= 100:
            continue
        fh = a.get("five_hour_pct")
        if not isinstance(fh, (int, float)):
            continue
        if a.get("five_hour_resets_at") and a["five_hour_resets_at"] <= now:
            fh = 0.0                                   # that window has already refilled
        left = 100.0 - fh
        if best_left is None or left > best_left:
            best, best_left = a, left
    if best is None or best_left < (100.0 - my_used) + SUGGEST_MARGIN:
        return None
    n = int(best_left)
    return {"email": best["email"], "five_hour_left_pct": n, "label": f"Switch to {best['email']} — {n}% left"}


def pool_level(fh, sd) -> str:
    vals = [v for v in (fh, sd) if isinstance(v, (int, float))]
    if any(v >= 100 for v in vals):
        return "limited"
    if (isinstance(fh, (int, float)) and fh >= WATCH_AT) or (isinstance(sd, (int, float)) and sd >= 90):
        return "watch"
    return "ok"


def is_stale(updated_at, now: float, expected_interval: float, last_failed: bool) -> bool:
    """No reading, a reading older than max(3 x expected interval, 15 min), or a failed last fetch."""
    if not updated_at or last_failed:
        return True
    return now - updated_at > max(3 * expected_interval, 900)
