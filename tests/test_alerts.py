"""Alerts (V2-SPEC §4): one per episode, mute, quiet hours, no threshold / danger pings."""
import time

import pytest

from conftest import feed

from ctu import alerts

NOW = 1_792_496_880.0
ON = {k: True for k in alerts.SEVERITY}


def wh(resets=1000, title="Limit around 16:26"):
    return alerts.cond("will_hit", "will_hit:five_hour", resets, title, "14 min before the 16:40 refill.")


def test_one_alert_per_episode():
    b = alerts.AlertBook()
    assert [a["id"] for a in b.evaluate([wh()], NOW, False, ON)] == ["will_hit:five_hour:1000"]
    assert b.evaluate([wh(title="Limit around 16:30")], NOW + 10, False, ON) == []      # same episode: silent
    assert b.view()[0]["title"] == "Limit around 16:30"                                 # text kept current
    assert b.view()[0]["severity"] == "warning" and b.view()[0]["muted"] is False


def test_episode_ends_when_condition_clears_then_new_episode_fires():
    b = alerts.AlertBook()
    b.evaluate([wh()], NOW, False, ON)
    assert b.evaluate([], NOW + 60, False, ON) == [] and b.view() == []                 # level fell
    again = b.evaluate([wh()], NOW + 120, False, ON)
    assert len(again) == 1 and again[0]["id"] == "will_hit:five_hour:1000#2"            # a new episode


def test_window_reset_starts_a_new_episode():
    b = alerts.AlertBook()
    b.evaluate([wh(1000)], NOW, False, ON)
    new = b.evaluate([wh(19000)], NOW + 10, False, ON)
    assert [a["id"] for a in new] == ["will_hit:five_hour:19000"] and len(b.view()) == 1


def test_muted_alerts_stay_in_state_but_are_not_delivered():
    b = alerts.AlertBook()
    assert b.evaluate([wh()], NOW, True, ON) == []
    assert b.view()[0]["muted"] is True
    assert b.evaluate([wh()], NOW + 10, False, ON) == []          # unmuting doesn't replay the episode


def test_disabled_kinds_are_not_raised():
    b = alerts.AlertBook()
    assert b.evaluate([wh()], NOW, False, {**ON, "will_hit": False}) == [] and b.view() == []


def test_persisted_episodes_do_not_refire_after_restart():
    b = alerts.AlertBook()
    b.evaluate([wh()], NOW, False, ON)
    b2 = alerts.AlertBook(b.to_json())
    assert b2.evaluate([wh()], NOW + 5, False, ON) == []


def test_mute_modes():
    b = alerts.AlertBook()
    b.set_mute("hour", NOW)
    assert b.mute_state(NOW, None) == {"until": int(NOW + 3600), "reason": "hour"}
    assert b.mute_state(NOW + 3601, None) == {"until": None, "reason": None}             # expires
    b.set_mute("refill", NOW, refill_at=NOW + 900)
    assert b.mute_state(NOW, None) == {"until": int(NOW + 900), "reason": "refill"}
    b.set_mute("off", NOW)
    assert b.mute_state(NOW, None)["until"] is None


def _local(h, m):
    lt = time.localtime(NOW)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))


@pytest.mark.parametrize("start,end,at,inside", [
    ("22:00", "08:00", (23, 30), True), ("22:00", "08:00", (7, 59), True), ("22:00", "08:00", (12, 0), False),
    ("09:00", "17:00", (12, 0), True), ("09:00", "17:00", (17, 0), False),
])
def test_quiet_hours(start, end, at, inside):
    q = {"enabled": True, "start": start, "end": end}
    t = _local(*at)
    until = alerts.quiet_until(q, t)
    assert (until is not None) == inside
    if inside:
        lt = time.localtime(until)
        assert (lt.tm_hour, lt.tm_min) == tuple(int(x) for x in end.split(":"))
        b = alerts.AlertBook()
        assert b.mute_state(t, q)["reason"] == "quiet_hours"
    assert alerts.quiet_until({**q, "enabled": False}, t) is None


def test_phone_push_kinds_match_the_spec():
    assert alerts.PHONE_PUSH == {"will_hit", "limited", "refilled", "overage_high", "session_waiting", "needs_approval"}
    assert alerts.SEVERITY["limited"] == "critical" and alerts.SEVERITY["incident"] == "info"


# ---- engine level ----

def delivered(e):
    out = []
    while not e._deliveries.empty():
        out.append(e._deliveries.get_nowait())
    return [a for a in out if a]


def test_no_threshold_or_danger_pings(make_engine):
    """Crossing 20/40/60/80% and every percent 95..99 used to toast; now only a forecast does."""
    e = make_engine()
    now = time.time()
    reset = int(now + 4 * 3600)
    for i, pct in enumerate([10, 21, 41, 61, 81, 95, 96, 97, 98, 99]):
        feed(e, "seven_day", [(now - 3000 + i * 300, float(pct))], int(now + 6 * 86400))
    feed(e, "five_hour", [(now - 30, 20.0)], reset)
    assert delivered(e) == []
    assert e.state["limits"][1]["level"] == "watch"                # visible in state, but watch never toasts


def test_will_hit_then_limited_then_refilled(make_engine):
    e = make_engine()
    now = time.time()
    reset = int(now + 3)
    feed(e, "five_hour", [(now - 1800 + i * 60, 60 + i) for i in range(30)], int(now + 3600))
    got = delivered(e)
    assert [a["kind"] for a in got] == ["will_hit"]
    feed(e, "five_hour", [(now - 1, 89.0)], int(now + 3600))
    assert delivered(e) == []                                      # same episode: one toast
    feed(e, "five_hour", [(now, 100.0)], int(now + 3600))
    assert [a["kind"] for a in delivered(e)] == ["limited"]
    assert e.state["decision"]["level"] == "limited" and e.state["decision"]["title"] == "5-hour limit reached"
    with e.lock:                                                   # the window's reset time passes
        e.readings["five_hour"]["resets_at"] = reset - 10
        e.recompute()
    kinds = [a["kind"] for a in delivered(e)]
    assert kinds == ["refilled"]
    assert e.state["limits"][0]["level"] == "ok"


def test_muted_engine_does_not_deliver(make_engine):
    e = make_engine()
    e.action("mute", {"for": "hour"})
    now = time.time()
    feed(e, "five_hour", [(now - 1800 + i * 60, 60 + i) for i in range(30)], int(now + 3600))
    assert delivered(e) == [] and e.state["alerts"][0]["muted"] is True


def test_context_and_waiting_alerts(make_engine):
    e = make_engine()
    e.on_hook({"hook_event_name": "Stop", "session_id": "s1", "cwd": "C:\\Dev\\app"})
    assert delivered(e) == []                                      # not yet away for 60 s
    with e.lock:
        e.sessions.hooks["s1"]["at"] -= 61
        e.recompute()
    got = delivered(e)
    assert [a["kind"] for a in got] == ["session_waiting"] and got[0]["title"] == "app is waiting for you"
    e.on_hook({"hook_event_name": "Notification", "session_id": "s2", "notification_type": "permission_prompt",
               "cwd": "C:\\Dev\\b"})
    assert [a["kind"] for a in delivered(e)] == ["needs_approval"]
    e.on_hook({"hook_event_name": "Stop", "session_id": "s3", "cwd": "C:\\Dev\\old"})
    with e.lock:
        e.sessions.hooks["s3"]["at"] -= 3600                       # waiting since long before this start
        e.recompute()
    assert delivered(e) == [] and not any(a["id"].startswith("session_waiting:s3") for a in e.state["alerts"])


def test_incident_and_overage_alerts(make_engine):
    from ctu import config
    e = make_engine(alerts={**config.DEFAULTS["alerts"], "incident": True})
    with e.lock:
        e.status_raw = {"indicator": "major", "description": "Elevated errors", "url": "u",
                        "components": [{"name": "Claude Code", "status": "partial_outage"}]}
        e.extra = {"enabled": True, "used": 41.0, "limit": 50.0, "currency": "EUR", "pct": 82.0}
        e.recompute()
    got = {a["kind"]: a for a in delivered(e)}
    assert set(got) == {"incident", "overage_high"}
    assert got["incident"]["body"] == "Affected: Claude Code." and got["overage_high"]["body"] == "€41.00 of €50.00 used."
    with e.lock:
        e.extra["pct"] = 78.0                                      # hysteresis: still the same episode
        e.recompute()
    assert any(a["kind"] == "overage_high" for a in e.state["alerts"])
    with e.lock:
        e.extra["pct"] = 70.0
        e.recompute()
    assert not any(a["kind"] == "overage_high" for a in e.state["alerts"])


def test_housekeeping_is_never_a_toast(make_engine, monkeypatch):
    from ctu import claude_cli
    monkeypatch.setattr(claude_cli, "claude_cli", lambda: None)
    e = make_engine()
    code, res = e.action("login", {})
    assert code == 200 and res["ok"] is False and "wasn't found" in res["message"]
    assert delivered(e) == []
