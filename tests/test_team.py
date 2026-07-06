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
    row = m.build_team_report(snap)
    assert row["fh_pct"] == 99.0 and row["sd_pct"] == 19.0
    assert row["fh_resets_at"] and row["fh_resets_at"].startswith("20")
    assert row["sd_resets_at"] is None
    assert row["extra"] == {"enabled": True, "used": 70.8, "limit": 75.0,
                            "currency": "EUR", "pct": 94.4}
    assert row["ts"] > 0


def test_build_team_report_minimal():
    row = m.build_team_report({"windows": [], "extra": None})
    assert row["fh_pct"] is None and row["sd_pct"] is None and row["extra"] is None
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


def test_ledger_computed():
    led = {
        "members": {"a": "A", "b": "B"},
        "days": {
            "2026-07-01": {"a": {"extra": {"used": 10.0}}, "b": {"extra": {"used": 1.0}}},
            "2026-07-02": {"a": {"extra": {"used": 12.5}}},
            "2026-07-03": {"a": {"extra": {"used": 2.0}}, "b": {"extra": {"used": 4.0}}},
        },
        "finals": {},
    }
    prev = {
        "days": {"2026-06-30": {"a": {"extra": {"used": 9.0}}}},
        "finals": {"b": {"extra": {"used": 0.5}}},
    }
    out = m.team_ledger_computed(led, prev)
    # a: baseline 9 → 10(+1) → 12.5(+2.5) → 2(reset: +2)  = 5.5
    # b: baseline 0.5 (prev FINAL preferred) → 1(+0.5) → 4(+3) = 3.5
    assert out["a"] == 5.5 and out["b"] == 3.5


def test_ledger_computed_no_prev_month():
    led = {"members": {"a": "A"},
           "days": {"2026-07-01": {"a": {"extra": {"used": 3.0}}},
                    "2026-07-02": {"a": {"extra": {"used": 7.0}}}},
           "finals": {}}
    assert m.team_ledger_computed(led)["a"] == 4.0


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
