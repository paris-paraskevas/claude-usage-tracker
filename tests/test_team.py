"""Team-mode unit tests — pure logic only (join codes, report rows, ledger math).
No relay network: the one call that would touch it points at a closed local port."""
import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_usage_tracker as m  # noqa: E402


def _code(payload: dict) -> str:
    return "cutteam1:" + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


# ---- join codes -------------------------------------------------------------

def test_parse_join_roundtrip():
    p = {"u": "https://relay.example", "t": "T" * 16, "m": "M" * 16, "k": "K" * 32, "n": "Paris"}
    assert m.team_parse_join(_code(p)) == p


def test_parse_join_rejects_garbage():
    assert m.team_parse_join("") is None
    assert m.team_parse_join("cutpair1:abc") is None           # phone-pairing code, not a team code
    assert m.team_parse_join("cutteam1:!!!") is None
    assert m.team_parse_join(_code({"u": "x"})) is None        # missing fields
    assert m.team_parse_join(_code({"u": "x", "t": "", "m": "y", "k": "z"})) is None


def test_join_and_leave(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "TEAM_PATH", tmp_path / "team.json")
    p = {"u": "http://127.0.0.1:9", "t": "team1234", "m": "mem12345", "k": "k" * 32, "n": "P"}
    ident = m.team_join(_code(p))
    assert isinstance(ident, dict) and ident["role"] == "member"
    assert m.load_team_identity()["member_id"] == "mem12345"
    assert isinstance(m.team_join(_code(p)), str)              # second join refused with a message
    m.team_leave()                                             # closed port: best-effort DELETE no-ops
    assert m.load_team_identity() is None


def test_join_rejects_bad_code(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "TEAM_PATH", tmp_path / "team.json")
    assert isinstance(m.team_join("not-a-code"), str)
    assert m.load_team_identity() is None


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
    row = m.build_team_report(snap, dev)
    assert row["fh_pct"] == 99.0 and row["sd_pct"] == 19.0
    assert row["fh_resets_at"] and row["fh_resets_at"].startswith("20")
    assert row["sd_resets_at"] is None
    assert row["did"] == "d1d1d1d1" and row["device"] == "DESKTOP-X"
    assert row["tok_month"] == 12345
    assert row["extra"] == {"enabled": True, "used": 70.8, "limit": 75.0,
                            "currency": "EUR", "pct": 94.4}
    assert row["ts"] > 0


def test_build_team_report_minimal():
    row = m.build_team_report({"windows": [], "extra": None})
    assert row["fh_pct"] is None and row["sd_pct"] is None and row["extra"] is None
    assert row["did"] is None and row["tok_month"] is None
    row = m.build_team_report({"windows": [], "extra": {"enabled": False}})
    assert row["extra"] is None                                # disabled overage isn't shared


# ---- calendar-month spend ---------------------------------------------------

def test_month_spend_plain():
    assert m.team_month_spend([1.0, 2.5, 4.0]) == 3.0          # no baseline: first sample seeds only
    assert m.team_month_spend([1.0, 2.5, 4.0], baseline=0.5) == 3.5


def test_month_spend_cycle_reset_mid_month():
    # meter: 70 → 74 → (anchor reset) → 2 → 5, entering the month at 68
    assert m.team_month_spend([70, 74, 2, 5], baseline=68) == 11.0


def test_month_spend_empty_and_junk():
    assert m.team_month_spend([]) == 0.0
    assert m.team_month_spend([None, "x"]) == 0.0
    assert m.team_month_spend([5.0], baseline=5.0) == 0.0      # idle month


def test_prev_month():
    assert m._prev_month("2026-07") == "2026-06"
    assert m._prev_month("2026-01") == "2025-12"


def _dev(used=None, ts=0, src="push", tok=None):
    r = {"ts": ts, "src": src}
    if used is not None:
        r["extra"] = {"used": used}
    if tok is not None:
        r["tok_month"] = tok
    return r


def test_day_account_row_prefers_cron_then_newest():
    assert m._day_account_row({"account": _dev(5, 10, "cron"), "d1": _dev(9, 99)})["extra"]["used"] == 5
    assert m._day_account_row({"d1": _dev(1, 10), "d2": _dev(2, 20)})["extra"]["used"] == 2
    assert m._day_account_row({}) is None
    assert m._day_account_row(None) is None


def test_ledger_computed_device_rows():
    led = {
        "members": {"a": "A", "b": "B"},
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
    # a: baseline 9 → 10(+1) → 12.5 cron wins (+2.5) → 2(reset, +2) = 5.5
    # b: baseline 0.5 (prev FINAL preferred) → 1(+0.5) → 4(+3) = 3.5
    assert out["a"] == 5.5 and out["b"] == 3.5


def test_ledger_computed_no_prev_month():
    led = {"members": {"a": "A"},
           "days": {"2026-07-01": {"a": {"d1": _dev(3.0, 1)}},
                    "2026-07-02": {"a": {"d1": _dev(7.0, 2)}}},
           "finals": {}}
    assert m.team_ledger_computed(led)["a"] == 4.0


def test_member_month_tokens_sums_last_per_device():
    led = {"days": {
        "2026-07-01": {"a": {"d1": _dev(ts=1, tok=100), "d2": _dev(ts=1, tok=10)}},
        "2026-07-03": {"a": {"d1": _dev(ts=2, tok=250), "account": _dev(ts=9, src="cron")}},
    }}
    assert m.member_month_tokens(led, "a") == 260        # d1 last=250 + d2 last=10; account ignored
    assert m.member_month_tokens(led, "zz") == 0


def test_team_overview_merge():
    ov = {"team": "t", "tz": "Europe/Athens", "today": "2026-07-06", "members": [
        {"mid": "a", "name": "A", "account": {"fh_pct": 99.0, "sd_pct": 10.0,
                                              "extra": {"used": 70.8, "limit": 75.0, "currency": "EUR", "pct": 94.4}},
         "devices": [], "escrow": {"present": True}},
        {"mid": "b", "name": "B", "account": {"fh_pct": 10.0, "sd_pct": 85.0, "extra": None},
         "devices": [], "escrow": {"present": False}},
    ]}
    led = {"days": {"2026-07-06": {"a": {"d1": _dev(70.8, 5, tok=45)},
                                   "b": {"d2": _dev(ts=5, tok=7)}}}, "finals": {}, "members": {}}
    # Steady state: the overview proxy always supplies the prior month as a baseline
    # (here A entered July having spent nothing through June).
    prev = {"days": {}, "finals": {"a": {"extra": {"used": 0.0}}}}
    out = m.team_overview_merge(ov, led, prev)
    ka = out["kpis"]
    assert ka["org_spend"] == 70.8 and ka["member_count"] == 2
    assert ka["near"] == [{"name": "A", "window": "5h", "pct": 99.0},
                          {"name": "B", "window": "weekly", "pct": 85.0}]
    ma = out["members"][0]
    assert ma["month_spend"] == 70.8 and ma["month_tokens"] == 45


# ---- device identity + device tokens -----------------------------------------

def test_device_month_tokens():
    cache = {"days": {
        "2026-06-30": {"in": 5, "out": 5, "cw": 0, "cr": 9, "msgs": 1},
        "2026-07-01": {"in": 100, "out": 50, "cw": 10, "cr": 999, "msgs": 3},
        "2026-07-05": {"in": 1, "out": 2, "cw": 3, "cr": 0, "msgs": 1},
    }}
    assert m.device_month_tokens(cache, "2026-07") == 166   # cr excluded, June excluded
    assert m.device_month_tokens(cache, "2026-05") == 0
    assert m.device_month_tokens({}, "2026-07") == 0
    assert m.device_month_tokens(None, "2026-07") == 0


def test_ensure_team_device(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "TEAM_PATH", tmp_path / "team.json")
    m.save_json(m.TEAM_PATH, {"v": 1, "role": "member", "url": "http://x", "team_id": "t" * 8,
                              "member_id": "m" * 8, "member_token": "k" * 32})
    ident = m.ensure_team_device(m.load_team_identity())
    assert ident["did"] and len(ident["did"]) >= 8
    assert ident["device"]                      # hostname, non-empty
    again = m.ensure_team_device(m.load_team_identity())
    assert again["did"] == ident["did"]         # stable across calls (persisted)


# ---- org binding --------------------------------------------------------------

def test_join_code_carries_org():
    p = {"u": "https://r.example", "t": "T" * 16, "m": "M" * 16, "k": "K" * 32,
         "n": "P", "o": "org-uuid-1"}
    assert m.team_parse_join(_code(p))["o"] == "org-uuid-1"


def test_join_refuses_wrong_org(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "TEAM_PATH", tmp_path / "team.json")
    monkeypatch.setattr(m, "fetch_profile_org", lambda: "org-B")
    p = {"u": "http://127.0.0.1:9", "t": "t" * 8, "m": "m" * 8, "k": "k" * 32,
         "n": "P", "o": "org-A"}
    res = m.team_join(_code(p))
    assert isinstance(res, str) and "org" in res.lower()
    assert m.load_team_identity() is None


def test_join_allows_matching_or_unknown_org(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "TEAM_PATH", tmp_path / "team.json")
    monkeypatch.setattr(m, "fetch_profile_org", lambda: "org-A")
    p = {"u": "http://127.0.0.1:9", "t": "t" * 8, "m": "m" * 8, "k": "k" * 32,
         "n": "P", "o": "org-A"}
    assert isinstance(m.team_join(_code(p)), dict)      # match → join
    m.team_leave()
    monkeypatch.setattr(m, "fetch_profile_org", lambda: None)   # offline → warn, allow
    assert isinstance(m.team_join(_code(p)), dict)


def test_team_overview_compact():
    merged = {
        "tz": "Europe/Athens",
        "kpis": {"org_spend": 169.2, "member_count": 2,
                 "near": [{"name": "A", "window": "5h", "pct": 99.0}]},
        "members": [
            {"mid": "a", "name": "A", "month_spend": 70.8, "month_tokens": 45,
             "account": {"fh_pct": 99.0, "sd_pct": 19.0, "extra": {"currency": "EUR"}}},
            {"mid": "b", "name": "B", "month_spend": 0.0, "month_tokens": 7,
             "account": {"fh_pct": 10.0, "sd_pct": 85.0, "extra": None}},
        ],
    }
    c = m.team_overview_compact(merged)
    assert c["org_spend"] == 169.2 and c["member_count"] == 2 and c["tz"] == "Europe/Athens"
    assert c["near"] == [{"name": "A", "window": "5h", "pct": 99.0}]
    assert c["members"][0] == {"name": "A", "fh_pct": 99.0, "sd_pct": 19.0,
                               "month_spend": 70.8, "month_tokens": 45, "currency": "EUR"}
    assert c["members"][1]["currency"] is None      # no extra block → currency None
    assert m.team_overview_compact(None) is None


# ---- sync throttle ----------------------------------------------------------

def test_teamsync_due_throttle():
    ts = m.TeamSync()
    assert ts.due(1000.0, 900)
    assert not ts.due(1500.0, 900)
    assert ts.due(1901.0, 900)
    ts.reset_throttle()
    assert ts.due(1902.0, 900)


def test_teamsync_disabled_without_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "TEAM_PATH", tmp_path / "absent.json")
    assert m.TeamSync.enabled({}) is False
