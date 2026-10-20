"""`--statusline`: snapshot merging, the printed line, the engine reading it, and installing it
into ~/.claude/settings.json without clobbering anyone's status line."""
import io
import json
import sys
import time

import pytest

from ctu import hooks, paths, statusline, usage

NOW = 1_792_496_880.0


def pl(fh=None, sd=None, sid="s1", ctx=None):
    d = {"session_id": sid, "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
         "workspace": {"current_dir": "C:\\Dev\\x"}, "rate_limits": {}}
    if fh:
        d["rate_limits"]["five_hour"] = {"used_percentage": fh[0], "resets_at": fh[1]}
    if sd:
        d["rate_limits"]["seven_day"] = {"used_percentage": sd[0], "resets_at": sd[1]}
    if ctx:
        d["context_window"] = {"context_window_size": ctx[0], "used_percentage": ctx[1], "total_input_tokens": ctx[2]}
    return d


def test_first_reading_and_rl_ts():
    s = statusline.merge(None, pl(fh=(64, NOW + 3600)), NOW)
    assert s["rate_limits"]["five_hour"] == {"used_percentage": 64.0, "resets_at": int(NOW + 3600)}
    assert s["rl_ts"] == int(NOW)
    s2 = statusline.merge(s, {"session_id": "s1"}, NOW + 30)       # no rate limits: freshness unchanged
    assert s2["rl_ts"] == int(NOW) and "five_hour" in s2["rate_limits"]


def test_same_window_keeps_the_max_and_older_windows_are_ignored():
    s = statusline.merge(None, pl(fh=(64, NOW + 3600)), NOW)
    s = statusline.merge(s, pl(fh=(50, NOW + 3610), sid="idle"), NOW + 5)    # an idle session's older number
    assert s["rate_limits"]["five_hour"]["used_percentage"] == 64.0
    s = statusline.merge(s, pl(fh=(99, NOW - 15000), sid="ancient"), NOW + 6)  # previous window entirely
    assert s["rate_limits"]["five_hour"]["used_percentage"] == 64.0
    s = statusline.merge(s, pl(fh=(3, NOW + 3600 + 18000)), NOW + 7)        # a newer window replaces
    assert s["rate_limits"]["five_hour"]["used_percentage"] == 3.0


def test_expired_windows_drop_and_sessions_are_capped():
    s = statusline.merge(None, pl(fh=(64, NOW + 10), sd=(41, NOW + 86400)), NOW)
    s = statusline.merge(s, {"session_id": "s2"}, NOW + 20)
    assert "five_hour" not in s["rate_limits"] and "seven_day" in s["rate_limits"]
    for i in range(30):
        s = statusline.merge(s, pl(sid=f"x{i}", ctx=(1000000, 12.5, 125000)), NOW + 30 + i)
    assert len(s["sessions"]) == statusline.MAX_SESSIONS
    assert s["sessions"]["x29"] == {"ts": int(NOW + 59), "model": "claude-opus-5-5", "cwd": "C:\\Dev\\x",
                                    "window": 1000000, "used_pct": 12.5, "tokens": 125000}


def test_format_line():
    d = pl(fh=(64, NOW + 3600), sd=(41, NOW + 86400), ctx=(200000, 73, 146000))
    line = statusline.format_line(d, statusline.merge(None, d, NOW))
    assert line.startswith("Opus · ctx 73% · 5h 64% ↻") and line.endswith("· 7d 41%")
    assert statusline.format_line({}, {}) == ""


def test_run_writes_snapshot_the_engine_reads(data_dir, monkeypatch, capsys, make_engine):
    now = time.time()
    payload = pl(fh=(64, now + 3600), sd=(41, now + 86400), ctx=(200000, 73, 146000))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    assert statusline.run() == 0
    assert "5h 64%" in capsys.readouterr().out
    sl = usage.read_statusline()
    assert sl["windows"]["five_hour"]["pct"] == 64.0 and sl["sessions"]["s1"]["window"] == 200000
    e = make_engine()
    e.job_statusline()
    st = e.snapshot()
    assert st["source"]["kind"] == "statusline" and st["limits"][0]["used_pct"] == 64.0
    assert st["source"]["stale"] is False


def test_run_survives_garbage(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"not json")))
    assert statusline.run() == 0


# ---- install into ~/.claude/settings.json ----

def test_install_when_absent_backs_up_and_is_idempotent(data_dir):
    paths.CLAUDE_SETTINGS.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    r = hooks.install_statusline()
    assert r["ok"] and r["installed"]
    d = json.loads(paths.CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    assert d["model"] == "opus" and d["statusLine"]["type"] == "command"
    assert d["statusLine"]["command"].endswith("--statusline")
    assert json.loads(paths.CLAUDE_SETTINGS.with_name("settings.json.cut-bak").read_text()) == {"model": "opus"}
    assert hooks.install_statusline()["installed"]
    assert hooks.statusline_status() == {"installed": True, "existing": None, "readable": True}
    assert hooks.remove_statusline() and "statusLine" not in json.loads(paths.CLAUDE_SETTINGS.read_text())


def test_never_clobbers_an_existing_statusline(data_dir):
    theirs = {"statusLine": {"type": "command", "command": "~/.claude/statusline.sh", "padding": 2}}
    paths.CLAUDE_SETTINGS.write_text(json.dumps(theirs), encoding="utf-8")
    r = hooks.install_statusline()
    assert not r["ok"] and r["error"] == "existing" and r["existing"] == "~/.claude/statusline.sh"
    assert json.loads(paths.CLAUDE_SETTINGS.read_text(encoding="utf-8")) == theirs
    assert hooks.remove_statusline() is True                         # not ours: left alone
    assert json.loads(paths.CLAUDE_SETTINGS.read_text(encoding="utf-8")) == theirs
    assert hooks.statusline_status()["existing"] == "~/.claude/statusline.sh"


@pytest.mark.parametrize("broken", ['{"model": "opus", ', "[1]"])
def test_never_writes_an_unparseable_file(data_dir, broken):
    paths.CLAUDE_SETTINGS.write_text(broken, encoding="utf-8")
    r = hooks.install_statusline()
    assert not r["ok"] and r["error"] == "unreadable"
    assert paths.CLAUDE_SETTINGS.read_text(encoding="utf-8") == broken
    assert hooks.remove_statusline() is False
