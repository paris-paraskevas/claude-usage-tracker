"""The log tailer and its consumers: byte offsets, partial lines, commit-on-success."""
import datetime as _dt
import json

from ctu import alltime, logtail, paths, sessions

NOW = _dt.datetime(2026, 6, 26, 18, 0, 0).timestamp()


def _rec(i, model="claude-opus-4-8", sid="s1", inp=100):
    return json.dumps({
        "timestamp": f"2026-06-26T12:00:{i:02d}.000Z", "cwd": r"C:\Dev\foo", "sessionId": sid,
        "message": {"model": model, "usage": {
            "input_tokens": inp, "output_tokens": 10,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}})


def _setup(data_dir):
    proj = paths.PROJECTS_DIR / "C--Dev-foo"
    proj.mkdir(parents=True)
    return proj / "sess.jsonl"


def test_read_appended_stops_at_end_and_partial_line(tmp_path):
    f = tmp_path / "x.jsonl"
    a, b, c = _rec(0) + "\n", _rec(1) + "\n", _rec(2)[:25]          # c = a half-written line
    f.write_bytes((a + b + c).encode())
    pos = [0]
    got = list(logtail.read_appended(f, 0, len(a), pos))             # stat'd size covers only `a`
    assert len(got) == 1 and pos[0] == len(a)
    pos = [0]
    got = list(logtail.read_appended(f, 0, len(a + b + c), pos))
    assert len(got) == 2 and pos[0] == len(a + b)                    # the half line is not consumed


def test_alltime_append_between_scans_no_double_count(data_dir):
    f = _setup(data_dir)
    f.write_text(_rec(0) + "\n" + _rec(1) + "\n", encoding="utf-8", newline="")
    cache = {}
    alltime.scan_all_time(cache, NOW)
    with open(f, "a", encoding="utf-8", newline="") as fh:
        fh.write(_rec(2) + "\n")
    a = alltime.scan_all_time(cache, NOW)
    assert a["total"]["msgs"] == 3
    assert a["periods"]["all"]["tokens"] == 3 * 110
    assert cache["files"][str(f)] == f.stat().st_size
    a = alltime.scan_all_time(cache, NOW)
    assert a["total"]["msgs"] == 3


def test_alltime_half_line_then_completed(data_dir):
    f = _setup(data_dir)
    first, second = _rec(0) + "\n", _rec(1)
    f.write_text(first + second[:40], encoding="utf-8", newline="")              # writer is mid-line
    cache = {}
    a = alltime.scan_all_time(cache, NOW)
    assert a["total"]["msgs"] == 1
    assert cache["files"][str(f)] == len(first.encode())             # offset parks before the half line
    with open(f, "a", encoding="utf-8", newline="") as fh:
        fh.write(second[40:] + "\n")
    a = alltime.scan_all_time(cache, NOW)
    assert a["total"]["msgs"] == 2                                    # completed line counted once, whole
    assert a["periods"]["all"]["tokens"] == 2 * 110


def test_alltime_error_mid_file_commits_nothing(data_dir, monkeypatch):
    f = _setup(data_dir)
    f.write_text(_rec(0) + "\n" + _rec(1, model="claude-boom-1") + "\n", encoding="utf-8", newline="")
    real = alltime.pretty_model

    def flaky(raw):
        if raw == "claude-boom-1":
            raise RuntimeError("boom")
        return real(raw)
    monkeypatch.setattr(alltime, "pretty_model", flaky)
    cache = {}
    a = alltime.scan_all_time(cache, NOW)
    assert a["total"]["msgs"] == 0                                    # partial totals NOT kept
    assert str(f) not in cache["files"]                               # offset not advanced
    monkeypatch.setattr(alltime, "pretty_model", real)
    a = alltime.scan_all_time(cache, NOW)
    assert a["total"]["msgs"] == 2                                    # re-read cleanly, counted once


def test_scan_sessions_half_line_and_append(data_dir):
    f = _setup(data_dir)
    first, second = _rec(0) + "\n", _rec(1)
    f.write_text(first + second[:30], encoding="utf-8", newline="")
    tr = sessions.SessionTracker(window_s=10 ** 9)
    tail = logtail.LogTailer([tr])

    def tokens():
        tail.poll(NOW)
        return sessions.v1_rows(tr.build(NOW), NOW)[0]["tokens"]
    assert tokens() == 110
    assert tr.files[str(f)]["offset"] == len(first.encode())
    with open(f, "a", encoding="utf-8", newline="") as fh:
        fh.write(second[30:] + "\n" + _rec(2) + "\n")
    assert tokens() == 3 * 110                                        # no double count, nothing lost
    assert tokens() == 3 * 110


def test_one_read_feeds_both_consumers(data_dir):
    """A single tailer pass feeds sessions and all-time; each keeps its own offset, and a consumer
    that starts later (all-time restored from disk) never sees bytes it already counted."""
    f = _setup(data_dir)
    f.write_text(_rec(0) + "\n" + _rec(1) + "\n", encoding="utf-8", newline="")
    at = alltime.AllTime()
    at.cache["files"][str(f)] = len((_rec(0) + "\n").encode())       # restored offset: rec 0 counted before
    tr = sessions.SessionTracker(window_s=10 ** 9)
    assert logtail.LogTailer([tr, at]).poll(NOW)
    assert at.cache["models"]["Opus 4.8"]["msgs"] == 1                # only the new record
    assert sessions.v1_rows(tr.build(NOW), NOW)[0]["tokens"] == 2 * 110   # sessions backfilled both
    assert at.cache["files"][str(f)] == tr.files[str(f)]["offset"] == f.stat().st_size


def test_failing_consumer_does_not_block_the_other(data_dir, monkeypatch):
    f = _setup(data_dir)
    f.write_text(_rec(0, model="claude-boom-1") + "\n", encoding="utf-8", newline="")
    monkeypatch.setattr(alltime, "pretty_model", lambda raw: (_ for _ in ()).throw(RuntimeError("boom")))
    at, tr = alltime.AllTime(), sessions.SessionTracker(window_s=10 ** 9)
    logtail.LogTailer([at, tr]).poll(NOW)
    assert str(f) not in at.cache["files"]
    assert tr.files[str(f)]["offset"] == f.stat().st_size


def test_heatmap_scales_to_displayed_range():
    today = _dt.date(2026, 6, 26)
    cache = alltime.empty_cache()
    old = (today - _dt.timedelta(days=800)).strftime("%Y-%m-%d")      # outside the ~1-year grid
    cache["days"][old] = {"in": 10 ** 9, "out": 0, "cw": 0, "cr": 0, "msgs": 1}
    cache["days"]["2026-06-25"] = {"in": 1000, "out": 0, "cw": 0, "cr": 0, "msgs": 1}
    cache["first_ts"] = _dt.datetime.combine(today - _dt.timedelta(days=800), _dt.time()).timestamp()
    hm = alltime.heatmap(cache, today)
    assert hm["max"] == 1000
    assert next(c for c in hm["days"] if c["d"] == "2026-06-25")["lvl"] == 4


def test_heatmap_is_a_full_sunday_aligned_year_even_for_new_users():
    today = _dt.date(2026, 10, 20)                                      # a Tuesday
    cache = alltime.empty_cache()
    cache["days"]["2026-10-20"] = {"in": 500, "out": 0, "cw": 0, "cr": 0, "msgs": 1}
    cache["first_ts"] = _dt.datetime.combine(today, _dt.time()).timestamp()
    days = alltime.heatmap(cache, today)["days"]
    first = _dt.date.fromisoformat(days[0]["d"])
    assert first.weekday() == 6 and days[-1]["d"] == "2026-10-20"        # Sunday .. today
    assert 365 <= len(days) <= 378 and sum(d["lvl"] > 0 for d in days) == 1


def test_alltime_handles_crlf_line_endings(data_dir):
    f = _setup(data_dir)
    first, second = _rec(0) + "\r\n", _rec(1)
    f.write_bytes((first + second[:40]).encode())                     # Windows-style, mid-line
    cache = {}
    a = alltime.scan_all_time(cache, NOW)
    assert a["total"]["msgs"] == 1
    assert cache["files"][str(f)] == len(first.encode())             # offset includes the \r\n
    with open(f, "ab") as fh:
        fh.write((second[40:] + "\r\n").encode())
    assert alltime.scan_all_time(cache, NOW)["total"]["msgs"] == 2
