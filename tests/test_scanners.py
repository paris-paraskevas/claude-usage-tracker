"""Incremental session-log scanners: byte offsets, partial lines, and commit-on-success."""
import datetime as _dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_usage_tracker as m  # noqa: E402

NOW = _dt.datetime(2026, 6, 26, 18, 0, 0).timestamp()


def _rec(i, model="claude-opus-4-8", sid="s1", inp=100):
    return json.dumps({
        "timestamp": f"2026-06-26T12:00:{i:02d}.000Z", "cwd": r"C:\Dev\foo", "sessionId": sid,
        "message": {"model": model, "usage": {
            "input_tokens": inp, "output_tokens": 10,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}})


def _setup(tmp_path, monkeypatch):
    proj = tmp_path / "C--Dev-foo"
    proj.mkdir()
    monkeypatch.setattr(m, "PROJECTS_DIR", tmp_path)
    return proj / "sess.jsonl"


def test_read_appended_stops_at_end_and_partial_line(tmp_path):
    f = tmp_path / "x.jsonl"
    a, b, c = _rec(0) + "\n", _rec(1) + "\n", _rec(2)[:25]          # c = a half-written line
    f.write_bytes((a + b + c).encode())
    pos = [0]
    got = list(m._read_appended(f, 0, len(a), pos))                  # stat'd size covers only `a`
    assert len(got) == 1 and pos[0] == len(a)
    pos = [0]
    got = list(m._read_appended(f, 0, len(a + b + c), pos))
    assert len(got) == 2 and pos[0] == len(a + b)                    # the half line is not consumed


def test_alltime_append_between_scans_no_double_count(tmp_path, monkeypatch):
    f = _setup(tmp_path, monkeypatch)
    f.write_text(_rec(0) + "\n" + _rec(1) + "\n", encoding="utf-8")
    cache = {}
    m.scan_all_time(cache, NOW)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(_rec(2) + "\n")
    a = m.scan_all_time(cache, NOW)
    assert a["total"]["msgs"] == 3
    assert a["periods"]["all"]["tokens"] == 3 * 110
    assert cache["files"][str(f)] == f.stat().st_size
    a = m.scan_all_time(cache, NOW)                                   # unchanged file: no-op
    assert a["total"]["msgs"] == 3


def test_alltime_half_line_then_completed(tmp_path, monkeypatch):
    f = _setup(tmp_path, monkeypatch)
    first = _rec(0) + "\n"
    second = _rec(1)
    f.write_text(first + second[:40], encoding="utf-8")              # writer is mid-line
    cache = {}
    a = m.scan_all_time(cache, NOW)
    assert a["total"]["msgs"] == 1
    assert cache["files"][str(f)] == len(first.encode())             # offset parks before the half line
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(second[40:] + "\n")
    a = m.scan_all_time(cache, NOW)
    assert a["total"]["msgs"] == 2                                    # completed line counted once, whole
    assert a["periods"]["all"]["tokens"] == 2 * 110


def test_alltime_error_mid_file_commits_nothing(tmp_path, monkeypatch):
    f = _setup(tmp_path, monkeypatch)
    f.write_text(_rec(0) + "\n" + _rec(1, model="claude-boom-1") + "\n", encoding="utf-8")
    real = m.pretty_model

    def flaky(raw):
        if raw == "claude-boom-1":
            raise RuntimeError("boom")
        return real(raw)
    monkeypatch.setattr(m, "pretty_model", flaky)
    cache = {}
    a = m.scan_all_time(cache, NOW)
    assert a["total"]["msgs"] == 0                                    # partial totals NOT kept
    assert str(f) not in cache["files"]                               # offset not advanced
    monkeypatch.setattr(m, "pretty_model", real)
    a = m.scan_all_time(cache, NOW)
    assert a["total"]["msgs"] == 2                                    # re-read cleanly, counted once


def test_scan_sessions_half_line_and_append(tmp_path, monkeypatch):
    f = _setup(tmp_path, monkeypatch)
    first, second = _rec(0) + "\n", _rec(1)
    f.write_text(first + second[:30], encoding="utf-8")
    cache = {}
    rows = m.scan_sessions(cache, NOW, window_s=10 ** 9)
    assert rows[0]["tokens"] == 110
    assert cache[str(f)]["size"] == len(first.encode())
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(second[30:] + "\n" + _rec(2) + "\n")
    rows = m.scan_sessions(cache, NOW, window_s=10 ** 9)
    assert rows[0]["tokens"] == 3 * 110                               # no double count, nothing lost
    rows = m.scan_sessions(cache, NOW, window_s=10 ** 9)
    assert rows[0]["tokens"] == 3 * 110


def test_heatmap_scales_to_displayed_range():
    today = _dt.date(2026, 6, 26)
    cache = m._empty_alltime_cache()
    old = (today - _dt.timedelta(days=800)).strftime("%Y-%m-%d")      # outside the ~1-year grid
    cache["days"][old] = {"in": 10 ** 9, "out": 0, "cw": 0, "cr": 0, "msgs": 1}
    cache["days"]["2026-06-25"] = {"in": 1000, "out": 0, "cw": 0, "cr": 0, "msgs": 1}
    cache["first_ts"] = _dt.datetime.combine(today - _dt.timedelta(days=800), _dt.time()).timestamp()
    hm = m._heatmap(cache, today)
    assert hm["max"] == 1000
    assert next(c for c in hm["days"] if c["d"] == "2026-06-25")["lvl"] == 4
