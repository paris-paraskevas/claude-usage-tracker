"""Unit tests for the pure-logic core (no GUI / network)."""
import json
import sys
import time
from pathlib import Path

import pytest

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
    v = m.compute_verdict([{"key": "five_hour", "pct": 100}])
    assert v["level"] == "over" and v["text"] == "At limit"          # 100% is no longer "Near limit"
    assert m.compute_verdict([{"key": "five_hour", "pct": 100},
                              {"key": "seven_day", "pct": 30}])["level"] == "over"


def test_check_danger(monkeypatch):
    from datetime import datetime, timezone
    fired = []
    monkeypatch.setattr(m, "notify", lambda *a, **k: fired.append(a[0]))
    reset = datetime(2026, 6, 27, 23, 59, tzinfo=timezone.utc)
    state, cfg = {}, {"danger_alerts": True}

    def win(pct):
        return {"five_hour": {"pct": pct, "resets_at": reset}}

    m.check_danger(win(90), state, cfg);  assert not fired          # below the zone -> silent
    m.check_danger(win(95), state, cfg);  assert len(fired) == 1    # crossing into 95
    m.check_danger(win(95), state, cfg);  assert len(fired) == 1    # same percent -> no repeat
    m.check_danger(win(94), state, cfg);  assert len(fired) == 1    # dropping -> no fire
    m.check_danger(win(98), state, cfg);  assert len(fired) == 2    # new high -> one fire (no backfill spam)
    m.check_danger(win(100), state, cfg); assert len(fired) == 3 and "100%" in fired[-1]
    m.check_danger(win(99), {}, {"danger_alerts": False})           # disabled -> silent (fresh state)
    assert len(fired) == 3


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


def test_pretty_model():
    assert m.pretty_model("claude-opus-4-8") == "Opus 4.8"
    assert m.pretty_model("claude-fable-5") == "Fable 5"
    assert m.pretty_model("claude-sonnet-4-6") == "Sonnet 4.6"
    assert m.pretty_model("claude-haiku-4-5-20251001") == "Haiku 4.5"   # date suffix dropped
    assert m.pretty_model("<synthetic>") == "Other"
    assert m.pretty_model(None) == "Other"


def test_make_icon_image_small_pct():
    # Regression: tiny non-zero pct (just after a window reset) used to invert the
    # fill rectangle -> Pillow "y1 must be greater than or equal to y0" -> icon froze.
    pytest.importorskip("PIL")   # GUI dep; CI's logic-core job runs without Pillow
    for pct in (0.0, 0.3, 0.5, 1, 2, 3, 3.9, 4, 50, 99.9, 100):
        img = m.make_icon_image({"five_hour": {"pct": pct}, "seven_day": {"pct": pct}})
        assert img.size == (64, 64) and img.mode == "RGBA"
    assert m.make_icon_image({}, error=True).size == (64, 64)


def test_read_transcript(tmp_path, monkeypatch):
    proj = tmp_path / "C--Dev-foo"
    proj.mkdir()
    lines = [
        json.dumps({"timestamp": "2026-06-26T12:00:00Z", "cwd": r"C:\Dev\foo",
                    "message": {"role": "user", "content": "hello claude"}}),
        json.dumps({"timestamp": "2026-06-26T12:00:01Z", "cwd": r"C:\Dev\foo",
                    "message": {"role": "assistant", "content": [
                        {"type": "text", "text": "hi there"}, {"type": "tool_use", "name": "Read"}]}}),
        json.dumps({"timestamp": "2026-06-26T12:00:02Z", "cwd": r"C:\Dev\foo",
                    "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}}),
    ]
    (proj / "s.jsonl").write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(m, "PROJECTS_DIR", tmp_path)

    t = m.read_transcript()
    assert t["name"] == "foo"
    assert [x["role"] for x in t["messages"]] == ["user", "assistant"]   # tool_result-only msg skipped
    assert t["messages"][0]["text"] == "hello claude"
    assert "hi there" in t["messages"][1]["text"] and "[ran Read]" in t["messages"][1]["text"]


def test_scan_all_time(tmp_path, monkeypatch):
    proj = tmp_path / "C--Dev-foo"
    proj.mkdir()
    lines = []
    for i in range(2):                                   # 2 Opus messages, session s1
        lines.append(json.dumps({
            "timestamp": f"2026-06-26T12:00:0{i}.000Z", "cwd": r"C:\Dev\foo", "sessionId": "s1",
            "message": {"model": "claude-opus-4-8", "usage": {
                "input_tokens": 100, "output_tokens": 50,
                "cache_creation_input_tokens": 10, "cache_read_input_tokens": 9000}}}))
    lines.append(json.dumps({                            # 1 Fable message, session s1
        "timestamp": "2026-06-26T13:00:00.000Z", "cwd": r"C:\Dev\foo", "sessionId": "s1",
        "message": {"model": "claude-fable-5", "usage": {
            "input_tokens": 200, "output_tokens": 30,
            "cache_creation_input_tokens": 5, "cache_read_input_tokens": 1000}}}))
    f = proj / "sess.jsonl"
    f.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(m, "PROJECTS_DIR", tmp_path)

    # Pin "now" to the data's own day so the period windows deterministically cover it.
    import datetime as _dt
    now = _dt.datetime(2026, 6, 26, 18, 0, 0).timestamp()

    cache = {}
    a = m.scan_all_time(cache, now)
    allp = a["periods"]["all"]
    assert allp["tokens"] == (100 + 50 + 10) * 2 + (200 + 30 + 5)   # cache reads excluded
    assert a["total"]["cr"] == 9000 * 2 + 1000                      # tracked separately
    assert a["total"]["msgs"] == 3 and allp["messages"] == 3
    assert allp["sessions"] == 1                                    # one distinct sessionId
    assert {r["name"] for r in allp["models"]} == {"Opus 4.8", "Fable 5"}
    assert allp["fav_model"] == "Opus 4.8"                          # most tokens
    assert a["projects"][0]["name"] == "foo"
    assert a["peak_hour"] is not None
    assert a["streak_current"] >= 1 and a["streak_longest"] >= 1
    assert a["heatmap"]["days"]                                     # non-empty grid

    # Incremental: a new-session line is folded in once (no re-read, no double count).
    with open(f, "a", encoding="utf-8") as fh:
        fh.write("\n" + json.dumps({
            "timestamp": "2026-06-26T14:00:00.000Z", "cwd": r"C:\Dev\foo", "sessionId": "s2",
            "message": {"model": "claude-opus-4-8", "usage": {
                "input_tokens": 1, "output_tokens": 1,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}))
    a2 = m.scan_all_time(cache, now)
    assert a2["total"]["msgs"] == 4
    assert a2["periods"]["all"]["tokens"] == (100 + 50 + 10) * 2 + (200 + 30 + 5) + 2
    assert a2["periods"]["all"]["sessions"] == 2                    # two distinct sessions

    # A second scan with no changes is a no-op (totals stable).
    a3 = m.scan_all_time(cache, now)
    assert a3["periods"]["all"]["tokens"] == a2["periods"]["all"]["tokens"]
    assert a3["total"]["msgs"] == 4


def test_read_transcripts_lists_recent_conversations(tmp_path, monkeypatch):
    """Bug 1 (phone "can't pick which session to chat in"): the desktop must mirror the
    recent conversations newest-first, each with its cwd, so the phone can pick one."""
    import os
    monkeypatch.setattr(m, "PROJECTS_DIR", tmp_path)

    def write(proj, cwd, msgs, mtime):
        d = tmp_path / proj
        d.mkdir()
        f = d / "session.jsonl"
        lines = [{"cwd": cwd, "timestamp": "2026-06-30T10:00:00.000Z",
                  "message": {"role": r, "content": c}} for (r, c) in msgs]
        f.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
        os.utime(f, (mtime, mtime))

    write("a", r"C:\Dev\projA", [("user", "hello A"), ("assistant", "hi A")], 1000)
    write("b", r"C:\Dev\projB", [("user", "hello B")], 2000)   # more recently active

    ts = m.read_transcripts(limit=6)
    assert [t["name"] for t in ts] == ["projB", "projA"]        # newest first
    assert ts[0]["cwd"] == r"C:\Dev\projB"
    assert ts[0]["messages"][0]["text"] == "hello B"
    assert m.read_transcript()["name"] == "projB"               # back-compat single reader


def test_run_remote_prompt_nonhanging_readonly(monkeypatch):
    """Bug 2 ("prompt took too long"): headless plan mode hangs waiting for plan approval.
    The command must use dontAsk (auto-deny, never prompt) + --bare, keep the read-only
    allowlist, and never block on stdin."""
    import subprocess
    monkeypatch.setattr(m, "_claude_cli", lambda: "claude")
    captured = {}

    class FakeProc:
        stdout = '{"type":"result","result":"hi from claude"}'
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = m.run_remote_prompt("what does foo() do?", cwd=None)
    assert out == "hi from claude"

    cmd = captured["cmd"]
    assert "plan" not in cmd                                    # the hang cause — gone
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert "--bare" in cmd and "-p" in cmd
    assert m.REMOTE_PROMPT_TOOLS in cmd                         # still read-only
    assert captured["kwargs"].get("stdin") == subprocess.DEVNULL
