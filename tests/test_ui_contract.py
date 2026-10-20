"""The engine's lazy topics and settings match the response shapes the UI was built against
(tests/ui/fixtures, written by the UI work). Skipped when those fixtures aren't present."""
import json
import time
from pathlib import Path

import pytest

from conftest import feed

import ctu.team as team

FIX = Path(__file__).resolve().parent / "ui" / "fixtures"
pytestmark = pytest.mark.skipif(not (FIX / "settings.json").exists(), reason="UI fixtures not present")


def keys(name):
    d = json.loads((FIX / name).read_text(encoding="utf-8"))
    d.pop("_doc", None)
    return d


def test_settings_shape(make_engine):
    ui, ours = keys("settings.json"), make_engine().settings()
    assert set(ui) <= set(ours)
    for group, v in ui.items():
        if isinstance(v, dict):
            assert set(v) <= set(ours[group]), group


def test_alltime_shape(make_engine):
    ui = keys("alltime.json")
    e = make_engine()
    now = time.time()
    feed(e, "five_hour", [(now - 600, 10.0)], int(now + 3600))
    ours = e.alltime_view("30d")
    assert set(ui) == set(ours)
    assert set(ui["usage"]) == set(ours["usage"]) and set(ui["heatmap"]) == set(ours["heatmap"])


def test_history_shape(make_engine):
    ui = json.loads((FIX / "history-five_hour.json").read_text(encoding="utf-8"))
    e = make_engine()
    now = time.time()
    feed(e, "five_hour", [(now - 600, 10.0), (now - 60, 11.0)], int(now + 3600))
    ours = e.history_view("five_hour")
    assert isinstance(ui, list) and isinstance(ours, list) and len(ours[0]) == len(ui[0]) == 2


def test_ledger_shape(monkeypatch):
    ui = keys("ledger.json")
    led = {"accounts": {"me@x.com": "Me", "b@x.com": "B"}, "finals": {},
           "days": {"2026-10-01": {"me@x.com": {"d1": {"ts": 1, "tok_month": 5, "extra": {"used": 1.0, "limit": 50.0,
                                                                                         "currency": "EUR"}}}},
                    "2026-10-02": {"me@x.com": {"d1": {"ts": 2, "tok_month": 9, "extra": {"used": 4.0, "limit": 50.0,
                                                                                         "currency": "EUR"}}},
                                   "b@x.com": {"d2": {"ts": 2, "extra": {"used": 2.0, "currency": "USD"}}}}}}
    monkeypatch.setattr(team.pool, "signed_in", lambda: True)
    monkeypatch.setattr(team.pool, "read_ledger", lambda m: led if m == "2026-10" else {"days": {}, "finals": {}})
    ours = team.ledger_view("2026-10", "UTC", "ME@x.com")
    assert set(ui) - {"_doc"} == set(ours)
    assert set(ui["rows"][0]) == set(ours["rows"][0])
    me = next(r for r in ours["rows"] if r["is_me"])
    assert me == {"email": "me@x.com", "name": "Me", "spend": 3.0, "currency": "EUR", "tokens": 9,
                  "meter_end": 4.0, "cap": 50.0, "days": 2, "state": me["state"], "is_me": True}
    assert {t["currency"] for t in ours["totals"]} == {"EUR"}         # B's first sample only seeds: no spend
    assert team.ledger_view("2026-1", "UTC")["ok"] is False
    monkeypatch.setattr(team.pool, "read_ledger", lambda m: (_ for _ in ()).throw(team.pool.PoolError("x")))
    assert team.ledger_view("2026-10", "UTC") == {"ok": False, "error": "unreachable"}
