"""Forecast and decision rules (V2-SPEC §3)."""
import pytest

from ctu import forecast as f

NOW = 1_792_496_880.0


def series(rate_h, minutes, start_pct, step_s=60, end=NOW):
    """Samples ending at `end`, rising `rate_h` %/h, every `step_s` seconds over `minutes`."""
    n = int(minutes * 60 // step_s)
    return [(end - (n - i) * step_s, start_pct + rate_h * (i * step_s) / 3600.0) for i in range(n + 1)]


def lim(lid="five_hour", level="ok", used=50.0, eta=None, resets=None, scope=None):
    return {"id": lid, "label": f.LABELS[lid], "scope": scope or f.SCOPES[lid], "used_pct": used,
            "resets_at": resets, "window_s": f.WINDOW_S[lid], "elapsed_pct": None, "level": level,
            "binding": False, "pace": {"rate_pct_h": None, "sustainable_pct_h": None,
                                       "projected_at_reset": None, "eta_100": eta, "confidence": "gathering"}}


def test_lsq_slope():
    assert f.lsq_slope([(0, 1), (1, 3), (2, 5)]) == pytest.approx(2.0)
    assert f.lsq_slope([(0, 0), (1, 0), (2, 3)]) == pytest.approx(1.5)        # least squares, not endpoints
    assert f.lsq_slope([(1, 1), (1, 2)]) is None
    assert f.lsq_slope([(1, 1)]) is None


def test_least_squares_rate_over_trailing_window():
    s = series(22.0, 30, 53.0)
    rate, conf = f.fit_rate("five_hour", s, NOW + 6720, NOW)
    assert rate == pytest.approx(22.0, abs=0.01) and conf == "ok"
    old = [(NOW - 3 * 3600, 5.0), (NOW - 2.5 * 3600, 30.0)]                    # outside the 30-min fit
    assert f.fit_rate("five_hour", old + s, NOW + 6720, NOW)[0] == pytest.approx(22.0, abs=0.01)


def test_integer_steps_fit_smoothly():
    s = [(t, float(int(v))) for t, v in series(12.0, 30, 40.0)]              # API reports whole percents
    assert f.fit_rate("five_hour", s, NOW + 3600, NOW)[0] == pytest.approx(12.0, abs=1.0)


def test_gathering_rules():
    reset = NOW + 3600
    assert f.fit_rate("five_hour", [], reset, NOW) == (None, "gathering")
    two = [(NOW - 900, 10.0), (NOW, 12.0)]
    assert f.fit_rate("five_hour", two, reset, NOW) == (None, "gathering")    # < 3 samples
    short = [(NOW - 300, 10.0), (NOW - 150, 11.0), (NOW, 12.0)]
    assert f.fit_rate("five_hour", short, reset, NOW) == (None, "gathering")  # spans < 10 min
    ok = [(NOW - 600, 10.0), (NOW - 300, 11.0), (NOW, 12.0)]
    assert f.fit_rate("five_hour", ok, reset, NOW)[1] == "low"                # just enough
    wk = series(0.5, 50, 40.0, step_s=600)
    assert f.fit_rate("seven_day", wk, NOW + 86400, NOW)[0] is None           # weekly needs >= 1 h
    wk = series(0.5, 300, 40.0, step_s=600)
    assert f.fit_rate("seven_day", wk, NOW + 86400, NOW) == (pytest.approx(0.5, abs=0.01), "ok")


def test_only_samples_after_the_last_reset():
    reset = NOW + 600                                          # this window started 4 h 50 min ago
    prev_window = series(40.0, 30, 10.0, end=reset - 18000 - 400)
    cur = [(NOW - 1200, 3.0), (NOW - 600, 3.0), (NOW, 3.0)]
    rate, _ = f.fit_rate("five_hour", prev_window + cur, reset, NOW)
    assert rate == pytest.approx(0.0)


def test_negative_slope_clamps_and_flat_is_zero():
    s = [(NOW - 1200, 20.0), (NOW - 600, 19.0), (NOW, 18.0)]   # a correction can lower the reading
    assert f.fit_rate("five_hour", s, NOW + 3600, NOW)[0] == 0.0
    flat = series(0.0, 30, 40.0)
    assert f.fit_rate("five_hour", flat, NOW + 3600, NOW)[0] == 0.0           # flat -> 0 ("steady"), not None


def test_pace_matches_the_spec_example():
    resets = 1_792_503_600
    p = f.pace("five_hour", 64.0, resets, series(22.0, 30, 53.0), NOW)
    pc = p["pace"]
    assert p["elapsed_pct"] == pytest.approx(62.7, abs=0.05)
    assert pc["sustainable_pct_h"] == pytest.approx(19.29, abs=0.01)
    assert pc["projected_at_reset"] == pytest.approx(105.1, abs=0.05)
    assert abs(pc["eta_100"] - 1_792_502_771) <= 2
    assert pc["confidence"] == "ok"


def test_eta_only_before_reset_and_not_when_limited():
    resets = NOW + 3600
    slow = f.pace("five_hour", 50.0, resets, series(20.0, 30, 40.0), NOW)["pace"]
    assert slow["eta_100"] is None and slow["projected_at_reset"] == pytest.approx(70.0, abs=0.1)
    full = f.pace("five_hour", 100.0, resets, series(20.0, 30, 90.0), NOW)["pace"]
    assert full["eta_100"] is None and full["sustainable_pct_h"] == 0.0
    nodata = f.pace("five_hour", 30.0, resets, [], NOW)["pace"]
    assert nodata["eta_100"] is None and nodata["projected_at_reset"] is None
    assert nodata["sustainable_pct_h"] == pytest.approx(70.0)                 # known without a rate
    expired = f.pace("five_hour", 0.0, None, series(20.0, 30, 40.0), NOW)
    assert expired["elapsed_pct"] is None and expired["pace"]["rate_pct_h"] is None


@pytest.mark.parametrize("used,pace,level", [
    (100.0, {"eta_100": None}, "limited"),
    (60.0, {"eta_100": NOW + 100, "projected_at_reset": 120.0, "rate_pct_h": 30.0}, "will_hit"),
    (60.0, {"eta_100": None, "projected_at_reset": 85.0, "rate_pct_h": 5.0}, "watch"),
    (60.0, {"eta_100": None, "projected_at_reset": 79.9, "rate_pct_h": 5.0}, "ok"),
    (91.0, {"eta_100": None, "projected_at_reset": None, "rate_pct_h": None}, "watch"),
    (89.0, {"eta_100": None, "projected_at_reset": None, "rate_pct_h": None}, "ok"),
    (91.0, {"eta_100": None, "projected_at_reset": 91.0, "rate_pct_h": 0.0}, "watch"),
])
def test_levels(used, pace, level):
    assert f.raw_level(used, pace) == level


def test_hysteresis():
    h = f.apply_hysteresis
    assert h("will_hit", "watch", 97.0, 60.0, False) == "will_hit"          # within 5 of 100: hold
    assert h("will_hit", "watch", 95.0, 60.0, False) == "watch"
    assert h("will_hit", "ok", 78.0, 60.0, False) == "watch"                # lands in watch (within 5 of 80)
    assert h("will_hit", "ok", 70.0, 60.0, False) == "ok"
    assert h("watch", "ok", 76.0, 60.0, False) == "watch"
    assert h("watch", "ok", 75.0, 60.0, False) == "ok"
    assert h("watch", "ok", 76.0, 60.0, True) == "ok"                       # the window reset
    assert h("ok", "will_hit", 120.0, 60.0, False) == "will_hit"            # rising is immediate
    assert h("will_hit", "ok", None, 97.0, False) == "will_hit"             # unknown rate: used % as lower bound
    assert h("limited", "ok", None, 0.0, True) == "ok"


def test_binding_highest_level_then_eta_then_used():
    a = lim("five_hour", "will_hit", 60, eta=NOW + 900)
    b = lim("seven_day", "will_hit", 80, eta=NOW + 600)
    assert f.pick_binding([a, b], None) == "seven_day"                      # earliest eta
    c = lim("five_hour", "watch", 70)
    d = lim("seven_day", "watch", 85)
    assert f.pick_binding([c, d], None) == "seven_day"                      # then highest used
    assert f.pick_binding([lim("five_hour", "ok", 99), lim("seven_day", "limited", 100)], None) == "seven_day"
    assert f.pick_binding([], None) is None


def test_scoped_limits_bind_only_for_the_active_model():
    fh = lim("five_hour", "ok", 30)
    opus = lim("seven_day_opus", "limited", 100, resets=NOW + 7200)
    assert f.pick_binding([fh, opus], "sonnet") == "five_hour"
    assert f.pick_binding([fh, opus], None) == "five_hour"
    assert f.pick_binding([fh, opus], "opus") == "seven_day_opus"
    assert f.scope_of_model("claude-opus-5-5") == "opus" and f.scope_of_model("claude-sonnet-4-6") == "sonnet"
    assert f.scope_of_model("claude-haiku-4-5") is None


def test_resume_at_is_latest_reset_of_limited():
    a = lim("five_hour", "limited", 100, resets=NOW + 600)
    b = lim("seven_day", "limited", 100, resets=NOW + 86400)
    assert f.resume_at([a, b], None) == NOW + 86400
    assert f.resume_at([a, lim("seven_day", "watch", 85, resets=NOW + 86400)], None) == NOW + 600
    assert f.resume_at([lim("five_hour", "ok", 10, resets=NOW)], None) is None


def test_texts():
    resets = 1_792_503_600
    eta = 1_792_502_760
    w = lim("five_hour", "will_hit", 64.0, eta=eta, resets=resets)
    w["pace"]["sustainable_pct_h"] = 19.29
    assert f.texts(w, NOW) == (f"Limit around {f.fmt_clock(eta, NOW)}",
                               f"14 min before the {f.fmt_clock(resets, NOW)} refill. Slow to 19%/h to make it.")
    watch = lim("five_hour", "watch", 70.0, resets=resets)
    watch["pace"]["projected_at_reset"] = 92.4
    assert f.texts(watch, NOW) == (f"About 92% by {f.fmt_clock(resets, NOW)}", "Close to the 5-hour limit at this pace.")
    ok = lim("five_hour", "ok", 38.0, resets=resets)
    assert f.texts(ok, NOW) == ("On pace", f"5-hour 38% · refills {f.fmt_clock(resets, NOW)}.")
    full = lim("five_hour", "limited", 100.0, resets=resets)
    assert f.texts(full, NOW) == ("5-hour limit reached", f"Back at {f.fmt_clock(resets, NOW)}.")
    over = {"enabled": True, "used": 12.4, "limit": 50.0, "currency": "EUR", "pct": 24.8}
    assert f.texts(full, NOW, over)[1].endswith("Extra usage is on (€12.40 of €50.00).")
    wk = lim("seven_day_opus", "limited", 100.0, resets=resets)
    assert f.texts(wk, NOW)[0] == "Weekly Opus limit reached"
    assert f.texts(None, NOW)[0] == "Waiting for data"


def test_fmt_helpers():
    assert f.fmt_dur(840) == "14 min" and f.fmt_dur(3900) == "1 h 05 min" and f.fmt_dur(3600) == "1 h"
    assert f.fmt_dur(2 * 86400 + 3600 * 4) == "2 d 4 h" and f.fmt_dur(10) == "1 min"
    assert f.fmt_rate(19.29) == "19" and f.fmt_rate(1.38) == "1.4"
    assert f.fmt_money(12.4, "EUR") == "€12.40" and f.fmt_money(3, "CHF") == "3.00 CHF"


def test_suggestion():
    pool = [
        {"email": "ops@acme.dev", "five_hour_pct": 18.0, "seven_day_pct": 33.0, "level": "ok", "is_me": False},
        {"email": "me@acme.dev", "five_hour_pct": 64.0, "seven_day_pct": 41.0, "level": "will_hit", "is_me": True},
        {"email": "full@acme.dev", "five_hour_pct": 0.0, "seven_day_pct": 100.0, "level": "limited", "is_me": False},
    ]
    s = f.suggestion(pool, 64.0, "will_hit", NOW)
    assert s == {"email": "ops@acme.dev", "five_hour_left_pct": 82, "label": "Switch to ops@acme.dev — 82% left"}
    assert f.suggestion(pool, 70.0, "ok", NOW) is None                     # on pace: nothing to switch for
    assert f.suggestion(pool, 5.0, "watch", NOW) is None                   # < 20 points more headroom
    reset = [{"email": "r@x", "five_hour_pct": 95.0, "five_hour_resets_at": NOW - 1, "seven_day_pct": 1.0,
              "level": "watch", "is_me": False}]
    assert f.suggestion(reset, 90.0, "will_hit", NOW)["five_hour_left_pct"] == 100   # its window refilled
    assert f.suggestion([], 90.0, "will_hit", NOW) is None


def test_staleness():
    assert f.is_stale(None, NOW, 300, False)
    assert not f.is_stale(NOW - 600, NOW, 300, False)
    assert f.is_stale(NOW - 901, NOW, 300, False)                          # 15 min floor
    assert not f.is_stale(NOW - 1700, NOW, 600, False) and f.is_stale(NOW - 1801, NOW, 600, False)
    assert f.is_stale(NOW - 10, NOW, 300, True)                            # last fetch failed


def test_fmt_clock_follows_the_windows_clock_setting(monkeypatch):
    import time as _t
    ts = _t.mktime((2026, 10, 20, 16, 40, 0, 0, 0, -1))
    monkeypatch.setattr(f, "CLOCK_12H", False)
    assert f.fmt_clock(ts, ts - 600) == "16:40"
    monkeypatch.setattr(f, "CLOCK_12H", True)
    assert f.fmt_clock(ts, ts - 600) == "4:40 PM"
