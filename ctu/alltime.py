"""Lifetime usage mined from every local session log (token counts, models, timestamps; never
message text). A LogTailer consumer: per-file offsets live in the persisted cache, so the first
run backfills history and later polls only fold appended bytes."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path

from ctu.logtail import Consumer, LogTailer, parse_iso, project_name, usage_of

ALLTIME_VERSION = 2
LOCK = threading.Lock()   # the tail worker folds into the cache while readers build views


def pretty_model(raw) -> str:
    """'claude-opus-4-8' -> 'Opus 4.8'; date suffixes dropped ('claude-haiku-4-5-20251001' -> 'Haiku 4.5')."""
    s = str(raw or "").strip()
    low = s.lower()
    for fam in ("opus", "sonnet", "haiku", "fable"):
        if fam in low:
            nums = [c for c in low.split(fam, 1)[1].replace("_", "-").split("-")
                    if c.isdigit() and len(c) <= 2]   # version parts, not a date stamp
            return (fam.capitalize() + " " + ".".join(nums[:2])).strip()
    if not s or s.startswith("<"):
        return "Other"
    return s.replace("claude-", "").replace("-", " ").strip().title() or "Other"


def empty_cache() -> dict:
    return {"v": ALLTIME_VERSION, "files": {}, "models": {}, "projects": {},
            "days": {}, "day_models": {}, "hours": {}, "sessions": {},
            "first_ts": None, "last_ts": None}


def _acc(d: dict, key: str, inp, out, cw, cr) -> None:
    r = d.get(key)
    if r is None:
        r = d[key] = {"in": 0, "out": 0, "cw": 0, "cr": 0, "msgs": 0}
    r["in"] += inp; r["out"] += out; r["cw"] += cw; r["cr"] += cr; r["msgs"] += 1


def at_tokens(r) -> int:
    """Headline metric: input + output + cache writes (cheap cache reads excluded)."""
    return r["in"] + r["out"] + r["cw"]


def _merge(cache: dict, delta: dict) -> None:
    for sect in ("models", "projects", "days"):
        dst = cache[sect]
        for k, r in delta[sect].items():
            a = dst.get(k)
            if a is None:
                dst[k] = dict(r)
            else:
                for f in ("in", "out", "cw", "cr", "msgs"):
                    a[f] = a.get(f, 0) + r.get(f, 0)
    for ds, mm in delta["day_models"].items():
        dm = cache["day_models"].setdefault(ds, {})
        for model, r in mm.items():
            a = dm.setdefault(model, {"in": 0, "out": 0, "cw": 0})
            a["in"] += r["in"]; a["out"] += r["out"]; a["cw"] += r["cw"]
    for hk, v in delta["hours"].items():
        cache["hours"][hk] = cache["hours"].get(hk, 0) + v
    for sid, ts in delta["sessions"].items():
        if ts > cache["sessions"].get(sid, 0):
            cache["sessions"][sid] = ts
    for k, fn in (("first_ts", min), ("last_ts", max)):
        if delta[k] is not None:
            cache[k] = delta[k] if cache[k] is None else fn(cache[k], delta[k])


class AllTime(Consumer):
    name = "alltime"

    def __init__(self, cache=None):
        if not isinstance(cache, dict) or cache.get("v") != ALLTIME_VERSION:
            cache = empty_cache()
        self.cache = cache
        self.rev = 0            # bumps on every commit; views are cached per rev

    def want(self, key, st, now):
        consumed = self.cache["files"].get(key, 0)
        if st.st_size < consumed:                   # rotated / truncated -> re-read from the top
            return 0
        return None if st.st_size == consumed else consumed

    def begin(self, key, path):
        d = empty_cache()
        d["_cwd"] = None
        return d

    def feed(self, d, o, key, path: Path):
        if d["_cwd"] is None and isinstance(o.get("cwd"), str):
            d["_cwd"] = o["cwd"]
        u = usage_of(o)
        if not u:
            return
        inp = int(u.get("input_tokens", 0) or 0)
        out = int(u.get("output_tokens", 0) or 0)
        cw = int(u.get("cache_creation_input_tokens", 0) or 0)
        cr = int(u.get("cache_read_input_tokens", 0) or 0)
        if inp == out == cw == cr == 0:
            return
        msg = o.get("message")
        model = pretty_model(msg.get("model") if isinstance(msg, dict) else None)
        _acc(d["models"], model, inp, out, cw, cr)
        _acc(d["projects"], project_name(o.get("cwd") or d["_cwd"], path.parent.name), inp, out, cw, cr)
        ts = parse_iso(o.get("timestamp"))
        if not ts:
            return
        dt = datetime.fromtimestamp(ts)
        ds = dt.strftime("%Y-%m-%d")
        _acc(d["days"], ds, inp, out, cw, cr)
        dr = d["day_models"].setdefault(ds, {}).setdefault(model, {"in": 0, "out": 0, "cw": 0})
        dr["in"] += inp; dr["out"] += out; dr["cw"] += cw
        hk = str(dt.hour)
        d["hours"][hk] = d["hours"].get(hk, 0) + inp + out + cw
        sid = o.get("sessionId")
        if isinstance(sid, str) and ts > d["sessions"].get(sid, 0):
            d["sessions"][sid] = ts
        d["first_ts"] = ts if d["first_ts"] is None else min(d["first_ts"], ts)
        d["last_ts"] = ts if d["last_ts"] is None else max(d["last_ts"], ts)

    def commit(self, key, d, offset):
        with LOCK:
            _merge(self.cache, d)
            self.cache["files"][key] = offset
            self.rev += 1

    def forget(self, live):
        with LOCK:                              # totals stay (cumulative); only offsets go
            for k in [k for k in self.cache["files"] if k not in live]:
                del self.cache["files"][k]

    def month_tokens(self, month: str) -> int:
        """This device's tokens for a calendar month (local-date day buckets)."""
        return device_month_tokens(self.cache, month)


def device_month_tokens(cache, month: str) -> int:
    if not isinstance(cache, dict):
        return 0
    total = 0
    with LOCK:
        for ds, r in list((cache.get("days") or {}).items()):
            if isinstance(ds, str) and ds.startswith(month + "-"):
                try:
                    total += at_tokens(r)
                except Exception:
                    pass
    return total


def scan_all_time(cache: dict, now: float, top_n: int = 8) -> dict:
    """One-shot scan with `cache` (mutated) as the state; returns the display dict."""
    at = AllTime(cache)
    if at.cache is not cache:                   # wrong/old version: reset in place
        cache.clear()
        cache.update(at.cache)
        at.cache = cache
    LogTailer([at]).poll(now)
    return display(cache, now, top_n)


# ---- views ----

def _d(ds):
    return datetime.strptime(ds, "%Y-%m-%d").date()


def _segs(model_map):
    segs = [{"name": m, "tok": r["in"] + r["out"] + r["cw"]} for m, r in model_map.items()]
    segs = [s for s in segs if s["tok"] > 0]
    segs.sort(key=lambda x: x["tok"], reverse=True)
    return segs


def _series(cache, today, ndays):
    """Stacked-bar bins: daily for 7d/30d, weekly (Monday-aligned, last 26) for all-time."""
    dm = cache["day_models"]
    bins = []
    if ndays is not None:
        for i in range(ndays - 1, -1, -1):
            d = today - timedelta(days=i)
            segs = _segs(dm.get(d.strftime("%Y-%m-%d"), {}))
            bins.append({"date": d.strftime("%Y-%m-%d"), "label": d.strftime("%b ") + str(d.day),
                         "total": sum(s["tok"] for s in segs), "segs": segs})
        return bins
    first = cache.get("first_ts")
    if not first:
        return []
    wk = datetime.fromtimestamp(first).date()
    wk = wk - timedelta(days=wk.weekday())
    while wk <= today:
        acc = {}
        for j in range(7):
            for m, r in dm.get((wk + timedelta(days=j)).strftime("%Y-%m-%d"), {}).items():
                acc[m] = acc.get(m, 0) + r["in"] + r["out"] + r["cw"]
        segs = sorted(({"name": m, "tok": t} for m, t in acc.items() if t > 0), key=lambda x: -x["tok"])
        bins.append({"date": wk.strftime("%Y-%m-%d"), "label": wk.strftime("%b ") + str(wk.day),
                     "total": sum(s["tok"] for s in segs), "segs": segs})
        wk += timedelta(days=7)
    return bins[-26:]


def _period(cache, now, today, ndays):
    cutoff = None if ndays is None else (today - timedelta(days=ndays - 1))
    tokens = msgs = active = 0
    for ds, r in cache["days"].items():
        if cutoff and _d(ds) < cutoff:
            continue
        t = at_tokens(r)
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
        sessions = sum(1 for ts in cache["sessions"].values() if ts >= now - ndays * 86400)
    return {"tokens": tokens, "messages": msgs, "sessions": sessions, "active_days": active,
            "fav_model": models[0]["name"] if models else "—", "models": models[:8],
            "series": _series(cache, today, ndays)}


def heatmap(cache, today):
    """A ~1-year day grid (Sunday-aligned), shaded 0-4 relative to the busiest DISPLAYED day."""
    toks = {ds: at_tokens(r) for ds, r in cache["days"].items()}
    first = cache.get("first_ts")
    if not first:
        return {"days": [], "max": 0}
    start = datetime.fromtimestamp(first).date()
    start = start - timedelta(days=(start.weekday() + 1) % 7)
    floor = today - timedelta(days=371)
    floor = floor - timedelta(days=(floor.weekday() + 1) % 7)
    start = max(start, floor)
    lo, hi = start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")
    mx = max((t for ds, t in toks.items() if lo <= ds <= hi), default=0)
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


def streaks(cache, today):
    active = {ds for ds, r in cache["days"].items() if at_tokens(r) > 0}
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


def peak_hour(cache):
    h = cache.get("hours") or {}
    if not h:
        return None
    hr = int(max(h.items(), key=lambda kv: kv[1])[0])
    return f"{hr % 12 or 12} {'AM' if hr < 12 else 'PM'}"


def _common(cache, now, top_n):
    today = datetime.fromtimestamp(now).date()
    total = {"in": 0, "out": 0, "cw": 0, "cr": 0, "msgs": 0}
    for r in cache["models"].values():
        for k in total:
            total[k] += r[k]
    projects = [{"name": n, "tokens": at_tokens(r)} for n, r in cache["projects"].items() if at_tokens(r) > 0]
    projects.sort(key=lambda x: x["tokens"], reverse=True)
    pg = sum(p["tokens"] for p in projects) or 1
    for p in projects:
        p["share"] = round(p["tokens"] / pg * 100, 1)
    cur, longest = streaks(cache, today)
    return today, {"ready": cache.get("first_ts") is not None, "total": total,
                   "first_ts": cache.get("first_ts"), "last_ts": cache.get("last_ts"),
                   "streak_current": cur, "streak_longest": longest, "peak_hour": peak_hour(cache),
                   "projects": projects[:top_n], "project_count": len(projects)}


def display(cache: dict, now: float, top_n: int = 8) -> dict:
    """Every period at once (the <= 0.3 shape)."""
    with LOCK:
        today, out = _common(cache, now, top_n)
        out["heatmap"] = heatmap(cache, today)
        out["periods"] = {"7": _period(cache, now, today, 7), "30": _period(cache, now, today, 30),
                          "all": _period(cache, now, today, None)}
        return out


RANGES = {"7d": 7, "30d": 30, "all": None}

# "That's about N x the tokens in <work>" (rough estimates: words x 1.3).
COMPARISONS = [("the U.S. Constitution", 10_000), ("The Little Prince", 22_000), ("The Great Gatsby", 63_000),
               ("1984", 120_000), ("Pride and Prejudice", 165_000), ("Dune", 244_000), ("Moby-Dick", 268_000),
               ("War and Peace", 750_000), ("the Lord of the Rings trilogy", 760_000),
               ("the Harry Potter series", 1_400_000)]


def comparison(tokens: int, rank: int):
    """The largest work the range beats twice over; `rank` (0 all, 1 30d, 2 7d) varies the pick."""
    cands = sorted([(n, t) for n, t in COMPARISONS if tokens >= t * 2], key=lambda x: x[1], reverse=True)
    if not cands:
        return None
    name, tok = cands[min(rank, len(cands) - 1)]
    return {"x": round(tokens / tok), "name": name}


def view(cache: dict, now: float, rng: str, top_n: int = 8, usage=None) -> dict:
    """GET /api/v2/alltime?range=7d|30d|all: range-scoped totals at the top level, plus lifetime
    totals, streaks, peak hour (local 0-23) and the heatmap. Projects are lifetime (projects_scope)."""
    with LOCK:
        today, common = _common(cache, now, top_n)
        p = _period(cache, now, today, RANGES[rng])
        hours = cache.get("hours") or {}
        return {
            "ready": common["ready"], "range": rng, "generated_at": int(now),
            "first_ts": common["first_ts"], "last_ts": common["last_ts"],
            **{k: p[k] for k in ("tokens", "messages", "sessions", "active_days", "fav_model", "models", "series")},
            "compare": comparison(p["tokens"], {"all": 0, "30d": 1, "7d": 2}[rng]),
            "usage": usage or {"t": [], "five_hour": [], "seven_day": []},
            "projects": common["projects"], "project_count": common["project_count"], "projects_scope": "all",
            "total": common["total"], "streak_current": common["streak_current"],
            "streak_longest": common["streak_longest"],
            "peak_hour": int(max(hours.items(), key=lambda kv: kv[1])[0]) if hours else None,
            "heatmap": heatmap(cache, today),
        }


def compact(cache: dict, now: float) -> dict:
    """The small subset the phone reads (relay payload): streaks, peak hour, period totals."""
    with LOCK:
        today = datetime.fromtimestamp(now).date()
        cur, longest = streaks(cache, today)
        periods = {}
        for key, nd in (("7", 7), ("30", 30), ("all", None)):
            p = _period(cache, now, today, nd)
            periods[key] = {k: p[k] for k in ("tokens", "messages", "sessions", "active_days", "fav_model")}
        return {"ready": cache.get("first_ts") is not None, "streak_current": cur, "streak_longest": longest,
                "peak_hour": peak_hour(cache), "periods": periods}
