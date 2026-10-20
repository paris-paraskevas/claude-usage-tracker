"""Team mode — pure logic (report rows, ledger maths, pool view, sync policy)."""
import ctu.team as m
from ctu import alltime, forecast


# ---- report rows ------------------------------------------------------------

def test_build_team_report():
    snap = {
        "windows": [
            {"key": "five_hour", "pct": 99.0, "resets_at": 1751806800000},
            {"key": "seven_day", "pct": 19.0, "resets_at": None},
        ],
        "extra": {"enabled": True, "used": 70.8, "limit": 75.0, "currency": "EUR", "pct": 94.4},
    }
    dev = {"did": "d1d1d1d1", "device": "DESKTOP-X", "tok_month": 12345}
    account = {"acct": "paris.paraskevas@skg-t.com", "name": "Paris", "org": "org-uuid"}
    row = m.build_team_report(snap, dev, account)
    assert row["fh_pct"] == 99.0 and row["sd_pct"] == 19.0
    assert row["fh_resets_at"] and row["fh_resets_at"].startswith("20")
    assert row["sd_resets_at"] is None
    assert row["did"] == "d1d1d1d1" and row["device"] == "DESKTOP-X"
    assert row["tok_month"] == 12345
    assert row["acct"] == "paris.paraskevas@skg-t.com" and row["name"] == "Paris" and row["org"] == "org-uuid"
    assert row["extra"] == {"enabled": True, "used": 70.8, "limit": 75.0,
                            "currency": "EUR", "pct": 94.4}
    assert row["ts"] > 0


def test_build_team_report_minimal():
    row = m.build_team_report({"windows": [], "extra": None})
    assert row["fh_pct"] is None and row["sd_pct"] is None and row["extra"] is None
    assert row["did"] is None and row["tok_month"] is None
    assert row["acct"] is None and row["name"] is None and row["org"] is None
    row = m.build_team_report({"windows": [], "extra": {"enabled": False}})
    assert row["extra"] is None                                # disabled overage isn't shared


# ---- calendar-month spend ---------------------------------------------------

def test_month_spend_plain():
    assert m.team_month_spend([1.0, 2.5, 4.0]) == 3.0          # no baseline: first sample seeds only
    assert m.team_month_spend([1.0, 2.5, 4.0], baseline=0.5) == 3.5


def test_month_spend_cycle_reset_mid_month():
    # meter: 70 -> 74 -> (anchor reset) -> 2 -> 5, entering the month at 68
    assert m.team_month_spend([70, 74, 2, 5], baseline=68) == 11.0


def test_month_spend_empty_and_junk():
    assert m.team_month_spend([]) == 0.0
    assert m.team_month_spend([None, "x"]) == 0.0
    assert m.team_month_spend([5.0], baseline=5.0) == 0.0      # idle month


def test_prev_month():
    assert m.prev_month("2026-07") == "2026-06"
    assert m.prev_month("2026-01") == "2025-12"


def _dev(used=None, ts=0, src="push", tok=None):
    r = {"ts": ts, "src": src}
    if used is not None:
        r["extra"] = {"used": used}
    if tok is not None:
        r["tok_month"] = tok
    return r


def test_day_account_row_prefers_cron_then_newest():
    assert m.day_account_row({"account": _dev(5, 10, "cron"), "d1": _dev(9, 99)})["extra"]["used"] == 5
    assert m.day_account_row({"d1": _dev(1, 10), "d2": _dev(2, 20)})["extra"]["used"] == 2
    assert m.day_account_row({}) is None
    assert m.day_account_row(None) is None


def test_ledger_computed_device_rows():
    led = {
        "accounts": {"a": "A", "b": "B"},
        "days": {
            "2026-07-01": {"a": {"d1": _dev(10.0, 100)}, "b": {"d9": _dev(1.0, 100)}},
            "2026-07-02": {"a": {"d1": _dev(11.0, 100), "account": _dev(12.5, 90, "cron")}},
            "2026-07-03": {"a": {"d1": _dev(2.0, 100)}, "b": {"d9": _dev(4.0, 100)}},
        },
        "finals": {},
    }
    prev = {"days": {"2026-06-30": {"a": {"account": _dev(9.0, 5, "cron")}}},
            "finals": {"b": {"extra": {"used": 0.5}}}}
    out = m.team_ledger_computed(led, prev)
    # a: baseline 9 -> 10(+1) -> 12.5 cron wins (+2.5) -> 2(reset, +2) = 5.5
    # b: baseline 0.5 (prev FINAL preferred) -> 1(+0.5) -> 4(+3) = 3.5
    assert out["a"] == 5.5 and out["b"] == 3.5


def test_ledger_computed_no_prev_month():
    led = {"accounts": {"a": "A"},
           "days": {"2026-07-01": {"a": {"d1": _dev(3.0, 1)}},
                    "2026-07-02": {"a": {"d1": _dev(7.0, 2)}}},
           "finals": {}}
    assert m.team_ledger_computed(led)["a"] == 4.0


def test_member_month_tokens_latest_wins():
    led = {"days": {
        "2026-07-01": {"a": {"d1": _dev(ts=1, tok=100), "d2": _dev(ts=1, tok=10)}},
        "2026-07-03": {"a": {"d1": _dev(ts=2, tok=250), "account": _dev(ts=9, src="cron")}},
    }}
    assert m.member_month_tokens(led, "a") == 250        # latest push (ts=2) wins; account row ignored
    assert m.member_month_tokens(led, "zz") == 0


def test_team_overview_merge():
    ov = {"team": "t", "tz": "Europe/Athens", "today": "2026-07-06", "accounts": [
        {"acct": "a", "name": "A", "account": {"fh_pct": 99.0, "sd_pct": 10.0,
                                               "extra": {"used": 70.8, "limit": 75.0, "currency": "EUR", "pct": 94.4}},
         "devices": [], "escrow": {"present": True}},
        {"acct": "b", "name": "B", "account": {"fh_pct": 10.0, "sd_pct": 85.0, "extra": None},
         "devices": [], "escrow": {"present": False}},
    ]}
    led = {"days": {"2026-07-06": {"a": {"d1": _dev(70.8, 5, tok=45)},
                                   "b": {"d2": _dev(ts=5, tok=7)}}}, "finals": {}, "accounts": {}}
    # Steady state: the overview proxy always supplies the prior month as a baseline
    # (here account A entered July having spent nothing through June).
    prev = {"days": {}, "finals": {"a": {"extra": {"used": 0.0}}}}
    out = m.team_overview_merge(ov, led, prev)
    ka = out["kpis"]
    assert ka["org_spend"] == 70.8 and ka["account_count"] == 2
    assert ka["near"] == [{"name": "A", "window": "5h", "pct": 99.0},
                          {"name": "B", "window": "weekly", "pct": 85.0}]
    ma = out["accounts"][0]
    assert ma["month_spend"] == 70.8 and ma["month_tokens"] == 45


# ---- device tokens ----------------------------------------------------------

def test_device_month_tokens():
    cache = {"days": {
        "2026-06-30": {"in": 5, "out": 5, "cw": 0, "cr": 9, "msgs": 1},
        "2026-07-01": {"in": 100, "out": 50, "cw": 10, "cr": 999, "msgs": 3},
        "2026-07-05": {"in": 1, "out": 2, "cw": 3, "cr": 0, "msgs": 1},
    }}
    assert alltime.device_month_tokens(cache, "2026-07") == 166   # cr excluded, June excluded
    assert alltime.device_month_tokens(cache, "2026-05") == 0
    assert alltime.device_month_tokens({}, "2026-07") == 0
    assert alltime.device_month_tokens(None, "2026-07") == 0


def test_team_overview_compact():
    merged = {
        "tz": "Europe/Athens",
        "kpis": {"org_spend": 169.2, "account_count": 2,
                 "near": [{"name": "A", "window": "5h", "pct": 99.0}]},
        "accounts": [
            {"acct": "a", "name": "A", "month_spend": 70.8, "month_tokens": 45,
             "account": {"fh_pct": 99.0, "sd_pct": 19.0, "extra": {"currency": "EUR"}}},
            {"acct": "b", "name": "B", "month_spend": 0.0, "month_tokens": 7,
             "account": {"fh_pct": 10.0, "sd_pct": 85.0, "extra": None}},
        ],
    }
    c = m.team_overview_compact(merged)
    # Output keeps the phone's `members`/`member_count` keys (sourced from the account pool).
    assert c["org_spend"] == 169.2 and c["member_count"] == 2 and c["tz"] == "Europe/Athens"
    assert c["near"] == [{"name": "A", "window": "5h", "pct": 99.0}]
    assert c["members"][0] == {"name": "A", "fh_pct": 99.0, "sd_pct": 19.0,
                               "month_spend": 70.8, "month_tokens": 45, "currency": "EUR"}
    assert c["members"][1]["currency"] is None      # no extra block -> currency None
    assert m.team_overview_compact(None) is None


# ---- sync policy + session-based enable ------------------------------------

def _signed_in(monkeypatch, pushed, matches=True):
    monkeypatch.setattr(m.pool, "signed_in", lambda: True)
    monkeypatch.setattr(m.pool, "device_matches", lambda: matches)
    monkeypatch.setattr(m.pool, "push", lambda *a, **k: pushed.append(a) or True)
    monkeypatch.setattr(m.pool, "device_id", lambda: "dev1")
    monkeypatch.setattr(m.usage, "read_account", lambda: {"email": "a@x.com", "name": "A"})
    monkeypatch.setattr(m.usage, "fetch_profile_org", lambda: None)


def test_teamsync_pushes_on_change_plus_heartbeat(monkeypatch):
    pushed = []
    _signed_in(monkeypatch, pushed)
    ts = m.TeamSync()
    snap = {"windows": [{"key": "five_hour", "pct": 10.0, "resets_at": None}], "extra": None}
    cfg = {"team_tz": "UTC"}
    assert ts.sync(snap, cfg, None, now=1000.0) and len(pushed) == 1
    assert not ts.sync(snap, cfg, None, now=1010.0) and len(pushed) == 1         # unchanged: no push
    snap2 = {"windows": [{"key": "five_hour", "pct": 11.0, "resets_at": None}], "extra": None}
    assert ts.sync(snap2, cfg, None, now=1020.0) and len(pushed) == 2            # content changed
    assert ts.sync(snap2, cfg, None, now=1020.0 + m.HEARTBEAT_S) and len(pushed) == 3   # heartbeat


def test_teamsync_checks_device_every_5_min(monkeypatch):
    pushed, calls = [], []
    _signed_in(monkeypatch, pushed)
    monkeypatch.setattr(m.pool, "device_matches", lambda: calls.append(1) or True)
    ts = m.TeamSync()
    snap = {"windows": [], "extra": None}
    for t in (0.0, 30.0, 299.0):
        ts.sync(snap, {}, None, now=1000.0 + t)
    assert len(calls) == 1
    ts.sync(snap, {}, None, now=1000.0 + m.DEVICE_CHECK_S)
    assert len(calls) == 2


def test_teamsync_enabled_tracks_session(monkeypatch):
    monkeypatch.setattr(m.pool, "configured", lambda: True)
    monkeypatch.setattr(m.pool, "has_session", lambda: False)
    assert m.TeamSync.enabled({}) is False          # signed out -> disabled
    monkeypatch.setattr(m.pool, "has_session", lambda: True)
    assert m.TeamSync.enabled({}) is True           # signed in -> enabled


def test_pool_view_orders_by_headroom_and_marks_me():
    merged = {"accounts": [
        {"acct": "busy@x.com", "name": "Busy", "account": {"fh_pct": 91.0, "sd_pct": 77.0, "ts": 5,
                                                           "fh_resets_at": "2099-01-01T00:00:00Z"}},
        {"acct": "me@x.com", "name": "Me", "account": {"fh_pct": 64.0, "sd_pct": 41.0, "ts": 6,
                                                       "extra": {"currency": "EUR"}}, "month_spend": 12.4},
        {"acct": "free@x.com", "name": "Free", "account": {"fh_pct": 18.0, "sd_pct": 33.0, "ts": 7},
         "last_used": {"device": "OPS-LAPTOP"}},
    ]}
    rows = m.pool_view(merged, "ME@x.com", None, 1000.0)
    assert [r["email"] for r in rows] == ["free@x.com", "me@x.com", "busy@x.com"]
    assert rows[1]["is_me"] and rows[1]["currency"] == "EUR" and rows[1]["month_spend"] == 12.4
    assert rows[0]["device"] == "OPS-LAPTOP" and rows[0]["level"] == "ok" and rows[2]["level"] == "watch"
    assert rows[2]["five_hour_resets_at"] == 4070908800
    assert forecast.suggestion(rows, 64.0, "will_hit", 1000.0)["email"] == "free@x.com"


# ---- currencies, role, team clock --------------------------------------------

def test_overview_merge_never_sums_mixed_currencies():
    ov = {"accounts": [
        {"acct": "a", "name": "A", "account": {"extra": {"currency": "EUR"}}},
        {"acct": "b", "name": "B", "account": {"extra": {"currency": "USD"}}},
        {"acct": "c", "name": "C", "account": {"extra": None}},
    ]}
    led = {"accounts": {}, "finals": {}, "days": {
        "2026-07-01": {"a": {"d": _dev(1.0, 1)}, "b": {"d": _dev(2.0, 1)}},
        "2026-07-02": {"a": {"d": _dev(4.0, 2)}, "b": {"d": _dev(7.0, 2)}}}}
    k = m.team_overview_merge(ov, led)["kpis"]
    assert k["org_spend"] is None and k["org_currency"] is None
    assert k["org_spend_by_currency"] == {"EUR": 3.0, "USD": 5.0}
    one = m.team_overview_merge({"accounts": ov["accounts"][:1] + ov["accounts"][2:]}, led)["kpis"]
    assert one["org_spend"] == 3.0 and one["org_currency"] == "EUR"


def test_overview_compact_carries_role():
    c = m.team_overview_compact({"kpis": {}, "accounts": []}, role="member")
    assert c["role"] == "member" and c["members"] == []


def test_team_now_falls_back_on_bad_zone():
    assert m.team_now("Not/AZone").tzinfo is not None              # no exception, local clock
    assert m.team_now(None).tzinfo is not None
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("UTC")
    except Exception:
        return                                                     # no tz database here
    assert m.team_now("UTC").utcoffset().total_seconds() == 0
