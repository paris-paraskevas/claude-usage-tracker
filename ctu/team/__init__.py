"""Team account pool (opt-in, docs/TEAM.md): ledger maths, the pool overview, and the push of
this machine's usage row. Rows are plaintext usage numbers only (RLS-scoped to the team)."""
from __future__ import annotations

import hashlib
import json
import socket
import time
import traceback
from datetime import datetime, timedelta, timezone

from ctu import alltime, io, usage
from ctu.team import pool

HEARTBEAT_S = 120          # push an unchanged row at most this often
DEVICE_CHECK_S = 300


def team_now(tz_name=None) -> datetime:
    """Now on the team's wall clock (ledger dates); the local clock if the zone is unknown."""
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(str(tz_name)))
        except Exception:
            pass
    return io.now_local()


def build_team_report(snap: dict, dev=None, account=None) -> dict:
    """The compact row shared with the team: window percents + resets, overage, this device's
    month tokens and which pooled account it is. `snap` is v1-shaped (windows[].resets_at in ms)."""
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
    """Month spend from day-by-day `extra.used` samples: sum of increases; a drop is a billing-cycle
    reset (the new value is all new spend). Without a baseline the first sample only seeds."""
    spend, prev = 0.0, baseline
    for v in samples:
        if not isinstance(v, (int, float)):
            continue
        if prev is not None:
            spend += (v - prev) if v >= prev else v
        prev = v
    return round(spend, 2)


def prev_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def day_account_row(devmap):
    """A day's authoritative row: the cron's `account` row, else the newest push."""
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
    vals = []
    for date in sorted(led.get("days") or {}):
        e = (day_account_row((led["days"][date] or {}).get(mid)) or {}).get("extra") or {}
        if isinstance(e.get("used"), (int, float)):
            vals.append(e["used"])
    return vals


def member_month_tokens(led: dict, mid: str) -> int:
    """tok_month of the account's latest push in the month (month-cumulative; cron rows skipped)."""
    best_ts, best_tok = None, 0
    for date in (led.get("days") or {}):
        for did, row in ((led["days"][date] or {}).get(mid) or {}).items():
            if did == "account" or not isinstance(row, dict) or not isinstance(row.get("tok_month"), (int, float)):
                continue
            ts = row.get("ts") or 0
            if best_ts is None or ts > best_ts:
                best_ts, best_tok = ts, int(row["tok_month"])
    return best_tok


def _ledger_baseline(prev_led, mid: str):
    if not prev_led:
        return None
    f = (((prev_led.get("finals") or {}).get(mid) or {}).get("extra")) or {}
    if isinstance(f.get("used"), (int, float)):
        return f["used"]
    prior = _ledger_samples(prev_led, mid)
    return prior[-1] if prior else None


def team_ledger_computed(led: dict, prev_led=None) -> dict:
    mids = set(led.get("accounts") or {})
    for d in (led.get("days") or {}).values():
        mids.update(d)
    return {mid: team_month_spend(_ledger_samples(led, mid), _ledger_baseline(prev_led, mid)) for mid in mids}


def team_overview_merge(ov: dict, led, prev_led=None) -> dict:
    """Month spend / tokens per account plus KPIs. Spend is totalled per currency and never summed
    across currencies (`org_spend` is set only for a single-currency pool)."""
    out = dict(ov)
    accounts = [dict(a0) for a0 in (ov.get("accounts") or [])]
    near, by_cur = [], {}
    spend = team_ledger_computed(led, prev_led) if led else {}
    for a0 in accounts:
        a0["month_spend"] = spend.get(a0.get("acct"))
        a0["month_tokens"] = member_month_tokens(led, a0.get("acct")) if led else 0
        row = a0.get("account") or {}
        if isinstance(a0["month_spend"], (int, float)):
            cur = (row.get("extra") or {}).get("currency") or ""
            by_cur[cur] = by_cur.get(cur, 0.0) + a0["month_spend"]
        for key, label in (("fh_pct", "5h"), ("sd_pct", "weekly")):
            p = row.get(key)
            if isinstance(p, (int, float)) and p >= 80:
                near.append({"name": a0.get("name"), "window": label, "pct": p})
    near.sort(key=lambda x: -x["pct"])
    if "" in by_cur and (len(by_cur) > 1 or not by_cur[""]):
        unknown = by_cur.pop("")              # zero spend without a currency is noise
        if unknown and len(by_cur) == 1:
            by_cur[next(iter(by_cur))] += unknown
        elif unknown:
            by_cur[""] = unknown
    by_cur = {k: round(v, 2) for k, v in by_cur.items()}
    single = len(by_cur) <= 1
    out["accounts"] = accounts
    out["kpis"] = {"org_spend": (round(sum(by_cur.values()), 2) if single else None),
                   "org_currency": (next(iter(by_cur), "") if single else None),
                   "org_spend_by_currency": by_cur, "account_count": len(accounts), "near": near}
    return out


def supabase_team_overview(tz: str):
    """Merged pool overview (overview + this/previous month ledgers), or None when signed out.
    Raises pool.PoolError when Supabase can't be read."""
    if not pool.signed_in():
        return None
    today = team_now(tz)
    tstr = today.strftime("%Y-%m-%d")
    ystr = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    ov = pool.read_overview(tz, tstr, ystr)
    month = tstr[:7]
    return team_overview_merge(ov, pool.read_ledger(month), pool.read_ledger(prev_month(month)))


def ledger(month: str) -> dict:
    """GET /api/team/ledger and /api/v2/team/ledger."""
    if not pool.signed_in():
        return {"error": "not_signed_in"}
    if not (len(month) == 7 and month[:4].isdigit() and month[4] == "-" and month[5:].isdigit()):
        return {"error": "bad_month"}
    try:
        led = pool.read_ledger(month)
        led["computed_spend"] = team_ledger_computed(led, pool.read_ledger(prev_month(month)))
        led["month_tokens"] = {acct: member_month_tokens(led, acct) for acct in (led.get("accounts") or {})}
        return led
    except Exception as exc:
        return {"error": "unreachable", "detail": str(exc)}


def ledger_rows(led: dict, prev_led, month: str, current_month: str, my_email=None) -> dict:
    """{rows, totals} for one month: per account spend (currency), tokens, the meter at month end,
    the cap, days with data, and live / end_of_day / closed."""
    spend = team_ledger_computed(led, prev_led)
    names = led.get("accounts") or {}
    mids = set(spend) | set(names)
    me = (my_email or "").strip().lower()
    rows, totals = [], {}
    for mid in mids:
        days = [d for d in sorted(led.get("days") or {}) if (led["days"][d] or {}).get(mid)]
        last = day_account_row(led["days"][days[-1]].get(mid)) if days else None
        final = ((led.get("finals") or {}).get(mid) or {})
        extra = (final.get("extra") or (last or {}).get("extra") or {})
        cur = extra.get("currency") or ""
        samples = _ledger_samples(led, mid)
        state = "live" if month >= current_month else ("closed" if final else "end_of_day")
        rows.append({"email": mid, "name": names.get(mid) or (last or {}).get("name") or mid,
                     "spend": spend.get(mid, 0.0), "currency": cur or None,
                     "tokens": member_month_tokens(led, mid),
                     "meter_end": samples[-1] if samples else None, "cap": extra.get("limit"),
                     "days": len(days), "state": state, "is_me": bool(me and str(mid).lower() == me)})
        if isinstance(spend.get(mid), (int, float)) and spend[mid] > 0:
            totals[cur] = round(totals.get(cur, 0.0) + spend[mid], 2)
    rows.sort(key=lambda r: (-(r["spend"] or 0), r["name"]))
    return {"rows": rows, "totals": [{"currency": c or None, "spend": v} for c, v in sorted(totals.items())]}


def ledger_view(month: str, tz, my_email=None) -> dict:
    if not (len(month) == 7 and month[:4].isdigit() and month[4] == "-" and month[5:].isdigit()):
        return {"ok": False, "error": "Month must be YYYY-MM."}
    if not pool.signed_in():
        return {"ok": False, "error": "signed_out"}
    current = team_now(tz).strftime("%Y-%m")
    try:
        led = pool.read_ledger(month)
        prev = pool.read_ledger(prev_month(month))
    except Exception:
        return {"ok": False, "error": "unreachable"}
    return {"month": month, "current_month": current, "tz": tz, "updated_at": int(time.time()),
            **ledger_rows(led, prev, month, current, my_email)}


def team_overview_compact(merged, role=None):
    """The small projection the phone renders (keeps its `members` / `member_count` keys)."""
    if not isinstance(merged, dict):
        return None
    k = merged.get("kpis") or {}
    members = []
    for a in merged.get("accounts") or []:
        row = a.get("account") or {}
        members.append({"name": a.get("name"), "fh_pct": row.get("fh_pct"), "sd_pct": row.get("sd_pct"),
                        "month_spend": a.get("month_spend"), "month_tokens": a.get("month_tokens"),
                        "currency": (row.get("extra") or {}).get("currency")})
    return {"org_spend": k.get("org_spend"), "org_currency": k.get("org_currency"),
            "org_spend_by_currency": k.get("org_spend_by_currency") or {},
            "member_count": k.get("account_count"), "role": role,
            "near": k.get("near") or [], "tz": merged.get("tz"), "members": members}


def pool_view(merged, my_email, my_level, now: float) -> list:
    """state.team.pool: best 5-hour headroom first."""
    from ctu import forecast
    out = []
    me = (my_email or "").strip().lower()
    for a in (merged or {}).get("accounts") or []:
        row = a.get("account") or {}
        last = a.get("last_used") or {}
        email = a.get("acct")
        is_me = bool(email and email.lower() == me)
        fh, sd = row.get("fh_pct"), row.get("sd_pct")
        out.append({
            "email": email, "name": a.get("name"),
            "five_hour_pct": fh, "five_hour_resets_at": usage.parse_reset(row.get("fh_resets_at")),
            "seven_day_pct": sd,
            "level": my_level if is_me and my_level else forecast.pool_level(fh, sd),
            "month_spend": a.get("month_spend"),
            "currency": (row.get("extra") or {}).get("currency") or None,
            "device": last.get("device") or row.get("device"),
            "updated_at": int(row["ts"]) if isinstance(row.get("ts"), (int, float)) else None,
            "is_me": is_me,
        })

    def headroom(p):
        fh = p["five_hour_pct"]
        if not isinstance(fh, (int, float)):
            return -1
        if p["five_hour_resets_at"] and p["five_hour_resets_at"] <= now:
            return 100
        return 100 - fh
    out.sort(key=headroom, reverse=True)
    return out


class TeamSync:
    """Pushes this machine's row when its content changes (plus a heartbeat), and runs the soft
    device-lock check every 5 min: a login claimed by another machine pauses pushing."""

    def __init__(self):
        self._acct_email = None
        self._acct_org = None
        self._last_hash = None
        self._last_push = 0.0
        self._device_checked = 0.0
        self.last_ok: bool | None = None
        self.device_ok: bool | None = None

    @staticmethod
    def enabled(cfg: dict | None = None) -> bool:
        return pool.configured() and pool.has_session()

    def session_changed(self) -> None:
        """Sign-in / sign-out / device claim: re-check the device lock and push right away."""
        self.device_ok = None
        self._last_hash = None

    def sync(self, snap: dict, cfg: dict, alltime_cache=None, now: float | None = None) -> bool:
        """Returns True if a push happened."""
        now = time.time() if now is None else now
        try:
            if not pool.signed_in():
                return False
            if self.device_ok is None or now - self._device_checked >= DEVICE_CHECK_S:
                self.device_ok = pool.device_matches()
                self._device_checked = now
            if not self.device_ok:
                return False
            acct = usage.read_account() or {}
            email = (acct.get("email") or "").strip().lower()
            if not email:
                return False
            if email != self._acct_email:                 # login switched to another pooled account
                self._acct_email = email
                self._acct_org = usage.fetch_profile_org()
                self._last_hash = None
            try:
                host = socket.gethostname()[:32] or "device"
            except Exception:
                host = "device"
            tnow = team_now(cfg.get("team_tz"))
            dev = {"did": pool.device_id(), "device": host,
                   "tok_month": alltime.device_month_tokens(alltime_cache, tnow.strftime("%Y-%m"))}
            account = {"acct": email, "name": acct.get("name"), "org": self._acct_org}
            row = build_team_report(snap, dev, account)
            h = hashlib.sha256(json.dumps({**row, "ts": None}, sort_keys=True).encode()).hexdigest()
            if h == self._last_hash and now - self._last_push < HEARTBEAT_S:
                return False
            prev = self.last_ok
            self.last_ok = pool.push(row, dev, account, tnow.strftime("%Y-%m-%d"))
            self._last_push = now
            self._last_hash = h if self.last_ok else None
            if self.last_ok != prev:
                io.log(f"team: push {'ok' if self.last_ok else 'failing'}")
            return True
        except Exception:
            if self.last_ok is not False:
                io.log("team: sync error:\n" + traceback.format_exc())
            self.last_ok = False
            return False
