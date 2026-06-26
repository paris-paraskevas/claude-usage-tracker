"""Unit tests for the pure-logic core (no GUI / network)."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_usage_tracker as m  # noqa: E402


def test_coerce_pct():
    assert m._coerce_pct(80) == 80
    assert m._coerce_pct(0.5) == 0.5          # already 0-100, not a fraction
    assert m._coerce_pct(150) == 100          # clamped
    assert m._coerce_pct(-5) == 0
    assert m._coerce_pct(True) is None        # bool rejected
    assert m._coerce_pct("x") is None


def test_parse_reset():
    assert m._parse_reset(None) is None
    dt = m._parse_reset("2026-06-26T12:00:00Z")
    assert dt is not None and dt.tzinfo is not None
    assert m._parse_reset(1782490000) is not None     # epoch seconds


def test_parse_windows():
    data = {
        "five_hour": {"utilization": 80.0, "resets_at": "2026-06-26T12:00:00Z"},
        "seven_day": {"utilization": 40.0, "resets_at": "2026-06-29T08:00:00Z"},
        "seven_day_opus": None,
        "seven_day_sonnet": {"utilization": 0.0, "resets_at": None},
    }
    w = m.parse_windows(data)
    assert round(w["five_hour"]["pct"]) == 80
    assert round(w["seven_day"]["pct"]) == 40
    assert "seven_day_opus" not in w                  # null skipped
    assert "seven_day_sonnet" not in w                # 0% + no reset skipped


def test_parse_extra():
    data = {"spend": {"enabled": True, "percent": 15,
                      "used": {"amount_minor": 771, "exponent": 2, "currency": "EUR"},
                      "limit": {"amount_minor": 5000, "exponent": 2, "currency": "EUR"}}}
    e = m.parse_extra(data)
    assert e["enabled"] and e["currency"] == "EUR"
    assert abs(e["used"] - 7.71) < 1e-6 and abs(e["limit"] - 50.0) < 1e-6
    assert e["pct"] == 15
    assert m.parse_extra({}) is None


def test_project():
    now = 1_000_000
    ts = [now - 1800 + i * 300 for i in range(7)]
    rising = [10, 15, 20, 25, 30, 35, 40]
    rate, eta = m.project(ts, rising, 40, None, now)
    assert rate and rate > 0 and eta and eta > 0
    rate2, eta2 = m.project(ts, [40] * 7, 40, None, now)
    assert eta2 is None                               # flat -> no ETA


def test_bucket_of():
    assert m.bucket_of(80, 20) == 80
    assert m.bucket_of(99, 20) == 80
    assert m.bucket_of(100, 20) == 100
    assert m.bucket_of(19, 20) == 0


def test_compute_verdict():
    assert m.compute_verdict([{"key": "five_hour", "pct": 10}])["level"] == "ok"
    assert m.compute_verdict([{"key": "five_hour", "pct": 85}])["level"] == "caution"
    assert m.compute_verdict([{"key": "seven_day", "pct": 97}])["level"] == "stop"
    assert m.compute_verdict([{"key": "five_hour", "pct": 10, "eta_seconds": 3000}])["level"] == "caution"


def test_vtuple():
    assert m._vtuple("0.1.10") > m._vtuple("0.1.9")
    assert m._vtuple("0.2.0") > m._vtuple("0.1.99")


def test_scan_sessions(tmp_path, monkeypatch):
    proj = tmp_path / "C--Dev-foo"
    proj.mkdir()
    rows_in = []
    for i in range(3):
        rows_in.append(json.dumps({
            "timestamp": f"2026-06-26T12:00:0{i}.000Z",
            "cwd": r"C:\Dev\foo",
            "message": {"model": "claude-opus-4-8", "usage": {
                "input_tokens": 1000, "output_tokens": 500,
                "cache_creation_input_tokens": 200, "cache_read_input_tokens": 300000}},
        }))
    (proj / "sess.jsonl").write_text("\n".join(rows_in), encoding="utf-8")
    monkeypatch.setattr(m, "PROJECTS_DIR", tmp_path)

    rows = m.scan_sessions({}, time.time(), window_s=10 ** 9)
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "foo"
    assert r["tokens"] == (1000 + 500 + 200) * 3        # cache_read excluded
    assert r["context_tokens"] == 1000 + 200 + 300000   # full last prompt
    assert 25 <= r["context_pct"] <= 35                 # 301200 / 1M -> ~30%
