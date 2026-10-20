"""Pure-logic core: parsing, models, transcripts, update check, config hygiene."""
import json
import os
from pathlib import Path

import pytest

from ctu import alltime, claude_cli, config, logtail, paths, remote, sessions, update, usage, v1


def test_coerce_pct():
    assert usage._coerce_pct(80) == 80
    assert usage._coerce_pct(0.5) == 0.5          # already 0-100, not a fraction
    assert usage._coerce_pct(150) == 100          # clamped
    assert usage._coerce_pct(-5) == 0
    assert usage._coerce_pct(True) is None        # bool rejected
    assert usage._coerce_pct("x") is None


def test_parse_reset():
    assert usage.parse_reset(None) is None
    assert usage.parse_reset("2026-06-26T12:00:00Z") == 1782475200
    assert usage.parse_reset("2026-06-26T12:00:00.400+00:00") == 1782475200
    assert usage.parse_reset(1782490000) == 1782490000          # epoch seconds
    assert usage.parse_reset(1782490000123) == 1782490000       # epoch ms
    assert usage.parse_reset("garbage") is None


def test_parse_windows():
    data = {
        "five_hour": {"utilization": 80.0, "resets_at": "2026-06-26T12:00:00Z"},
        "seven_day": {"utilization": 40.0, "resets_at": "2026-06-29T08:00:00Z"},
        "seven_day_opus": None,
        "seven_day_sonnet": {"utilization": 0.0, "resets_at": None},
    }
    w = usage.parse_windows(data)
    assert round(w["five_hour"]["pct"]) == 80 and w["five_hour"]["resets_at"] == 1782475200
    assert round(w["seven_day"]["pct"]) == 40
    assert "seven_day_opus" not in w                  # null skipped
    assert "seven_day_sonnet" not in w                # 0% + no reset skipped


def test_parse_extra():
    data = {"spend": {"enabled": True, "percent": 15,
                      "used": {"amount_minor": 771, "exponent": 2, "currency": "EUR"},
                      "limit": {"amount_minor": 5000, "exponent": 2, "currency": "EUR"}}}
    e = usage.parse_extra(data)
    assert e["enabled"] and e["currency"] == "EUR"
    assert abs(e["used"] - 7.71) < 1e-6 and abs(e["limit"] - 50.0) < 1e-6
    assert e["pct"] == 15
    assert usage.parse_extra({}) is None


def test_read_token_and_plan_follow_the_file(data_dir):
    assert usage.read_token() == (None, usage.TokenState.MISSING)
    paths.CREDS_PATH.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 1,
                                                              "subscriptionType": "pro"}}), encoding="utf-8")
    assert usage.read_token() == ("t", usage.TokenState.EXPIRED)
    assert usage.read_plan() == "pro"
    paths.CREDS_PATH.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t2", "expiresAt": 9e15,
                                                              "subscriptionType": "max"}}), encoding="utf-8")
    os.utime(paths.CREDS_PATH, (1, 1))                 # a different mtime -> re-read
    assert usage.read_token() == ("t2", usage.TokenState.OK) and usage.read_plan() == "max"


def test_vtuple():
    assert update.vtuple("0.1.10") > update.vtuple("0.1.9")
    assert update.vtuple("0.2.0") > update.vtuple("0.1.99")


def test_scan_sessions(data_dir):
    proj = paths.PROJECTS_DIR / "C--Dev-foo"
    proj.mkdir(parents=True)
    rows_in = [json.dumps({
        "timestamp": f"2026-06-26T12:00:0{i}.000Z", "cwd": r"C:\Dev\foo",
        "message": {"model": "claude-opus-4-8", "usage": {
            "input_tokens": 1000, "output_tokens": 500,
            "cache_creation_input_tokens": 200, "cache_read_input_tokens": 300000}},
    }) for i in range(3)]
    (proj / "sess.jsonl").write_text("\n".join(rows_in), encoding="utf-8")
    tr = sessions.SessionTracker(window_s=10 ** 9)
    import time
    now = time.time()
    logtail.LogTailer([tr]).poll(now)
    rows = sessions.v1_rows(tr.build(now), now)
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "foo"
    assert r["tokens"] == (1000 + 500 + 200) * 3        # cache_read excluded
    assert r["context_tokens"] == 1000 + 200 + 300000   # full last prompt
    assert 25 <= r["context_pct"] <= 35                 # 301200 / 1M -> ~30%


def test_pretty_model():
    assert alltime.pretty_model("claude-opus-4-8") == "Opus 4.8"
    assert alltime.pretty_model("claude-fable-5") == "Fable 5"
    assert alltime.pretty_model("claude-opus-5-5") == "Opus 5.5"
    assert alltime.pretty_model("claude-sonnet-5-5") == "Sonnet 5.5"
    assert alltime.pretty_model("claude-fable-5-1") == "Fable 5.1"
    assert alltime.pretty_model("claude-sonnet-4-6") == "Sonnet 4.6"
    assert alltime.pretty_model("claude-haiku-4-5-20251001") == "Haiku 4.5"   # date suffix dropped
    assert alltime.pretty_model("<synthetic>") == "Other"
    assert alltime.pretty_model(None) == "Other"


def test_read_transcript(data_dir):
    proj = paths.PROJECTS_DIR / "C--Dev-foo"
    proj.mkdir(parents=True)
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
    t = remote.read_transcripts(limit=1)[0]
    assert t["name"] == "foo"
    assert [x["role"] for x in t["messages"]] == ["user", "assistant"]   # tool_result-only msg skipped
    assert t["messages"][0]["text"] == "hello claude"
    assert "hi there" in t["messages"][1]["text"] and "[ran Read]" in t["messages"][1]["text"]


def test_scan_all_time(data_dir):
    proj = paths.PROJECTS_DIR / "C--Dev-foo"
    proj.mkdir(parents=True)
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
    import datetime as _dt
    now = _dt.datetime(2026, 6, 26, 18, 0, 0).timestamp()

    cache = {}
    a = alltime.scan_all_time(cache, now)
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

    with open(f, "a", encoding="utf-8") as fh:                      # incremental: folded once
        fh.write("\n" + json.dumps({
            "timestamp": "2026-06-26T14:00:00.000Z", "cwd": r"C:\Dev\foo", "sessionId": "s2",
            "message": {"model": "claude-opus-4-8", "usage": {
                "input_tokens": 1, "output_tokens": 1,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}))
    a2 = alltime.scan_all_time(cache, now)
    assert a2["total"]["msgs"] == 4
    assert a2["periods"]["all"]["tokens"] == (100 + 50 + 10) * 2 + (200 + 30 + 5) + 2
    assert a2["periods"]["all"]["sessions"] == 2
    a3 = alltime.scan_all_time(cache, now)                          # unchanged file: no-op
    assert a3["periods"]["all"]["tokens"] == a2["periods"]["all"]["tokens"]
    assert a3["total"]["msgs"] == 4
    v = alltime.view(cache, now, "7d")
    assert v["range"] == "7d" and len(v["series"]) == 7 and v["tokens"] == a2["periods"]["7"]["tokens"]
    assert v["peak_hour"] in range(24) and v["projects_scope"] == "all" and v["compare"] is None
    c = alltime.compact(cache, now)
    assert set(c["periods"]["all"]) >= {"tokens", "messages", "sessions", "fav_model"}


def test_read_transcripts_lists_recent_conversations(data_dir):
    def write(proj, cwd, msgs, mtime):
        d = paths.PROJECTS_DIR / proj
        d.mkdir(parents=True)
        f = d / "session.jsonl"
        lines = [{"cwd": cwd, "timestamp": "2026-06-30T10:00:00.000Z",
                  "message": {"role": r, "content": c}} for (r, c) in msgs]
        f.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
        os.utime(f, (mtime, mtime))

    write("a", r"C:\Dev\projA", [("user", "hello A"), ("assistant", "hi A")], 1000)
    write("b", r"C:\Dev\projB", [("user", "hello B")], 2000)
    ts = remote.read_transcripts(limit=6)
    assert [t["name"] for t in ts] == ["projB", "projA"]        # newest first
    assert ts[0]["cwd"] == r"C:\Dev\projB"
    assert ts[0]["messages"][0]["text"] == "hello B"


def test_project_name():
    assert logtail.project_name(r"C:\Dev\foo", "fb") == "foo"
    assert logtail.project_name("/home/u/bar", "fb") == "bar"
    assert logtail.project_name("C:\\Dev\\foo\\", "fb") == "foo"
    assert logtail.project_name("", "fb") == "fb"
    assert logtail.project_name(None, "fb") == "fb"
    assert logtail.project_name("/", "sess") == "sess"


def test_safe_login_email_blocks_shell_injection():
    assert claude_cli.safe_login_email("user@example.com") == "user@example.com"
    assert claude_cli.safe_login_email('x" & calc.exe & "y@e.com') is None
    assert claude_cli.safe_login_email("a b@e.com") is None
    assert claude_cli.safe_login_email("no-at-sign") is None
    assert claude_cli.safe_login_email(None) is None
    assert claude_cli.safe_login_email("") is None


def test_remote_sync_pushes_on_change_plus_heartbeat():
    rs = remote.RemoteSync()
    snap = {"ok": True, "windows": [{"key": "five_hour", "pct": 40, "resets_at": 1, "level": "ok"}]}
    assert rs.due(snap, 100.0, 10) is True             # never pushed
    rs._last_hash, rs._last_push = remote.significant(snap), 100.0
    assert rs.due(snap, 105.0, 10) is False            # inside the min gap
    t = 100.0 + remote.HEARTBEAT_S - 5
    assert rs.due(snap, t, 10) is False                # unchanged, before the heartbeat
    drift = {**snap, "windows": [{**snap["windows"][0], "pace": {"sustainable_pct_h": 9.9}}]}
    assert rs.due(drift, t, 10) is False               # pace drift alone isn't a change
    changed = {**snap, "windows": [{**snap["windows"][0], "pct": 41}]}
    assert rs.due(changed, t, 10) is True
    assert remote.HEARTBEAT_S + 10 <= 60               # job cadence + heartbeat: the phone sees <= 60 s gaps
    assert rs.due(snap, 100.0 + remote.HEARTBEAT_S, 10) is True
    rs.force()
    assert rs.due(snap, 101.0, 10) is True             # manual "sync now"
    assert remote.RemoteSync.enabled({}) is False


def test_read_transcripts_exposes_session_id(data_dir):
    d = paths.PROJECTS_DIR / "proj"
    d.mkdir(parents=True)
    (d / "abc123.jsonl").write_text(
        json.dumps({"cwd": r"C:\Dev\foo", "timestamp": "2026-06-30T10:00:00.000Z",
                    "message": {"role": "user", "content": "hi"}}), encoding="utf-8")
    ts = remote.read_transcripts(limit=1)
    assert ts and ts[0]["session_id"] == "abc123"


def test_usage_bands_are_distinct():
    cols = [v1.usage_style(p)[0] for p in (10, 65, 85, 95, 100)]
    assert len(set(cols)) == 5


def test_v1_verdict_maps_decision_levels():
    assert v1.verdict({"level": "ok", "title": "On pace"}) == {"level": "ok", "color": "#5e9e72", "text": "On pace"}
    assert v1.verdict({"level": "watch", "title": "x"})["level"] == "caution"
    assert v1.verdict({"level": "will_hit", "title": "x"})["level"] == "stop"
    assert v1.verdict({"level": "limited", "title": "5-hour limit reached"})["level"] == "over"


def test_check_latest_uses_pypi_for_pip_installs(monkeypatch):
    monkeypatch.setattr(update, "check_github_latest", lambda: ("9.9.9", "https://x/Setup.exe"))
    monkeypatch.setattr(update, "check_pypi_latest", lambda: "0.3.0")
    monkeypatch.setattr(update, "is_installed_pkg", lambda: True)
    assert update.check_latest() == ("0.3.0", None)
    monkeypatch.setattr(update, "is_installed_pkg", lambda: False)
    assert update.check_latest() == ("9.9.9", "https://x/Setup.exe")


def test_config_has_no_dead_keys():
    for k in ("team_share_token", "alltime_days", "threshold_step", "danger_alerts", "widget_width",
              "bar_fields", "open_as_window", "show_widget_on_start", "history_cap"):
        assert k not in config.DEFAULTS and k not in config.SCHEMA
    assert not hasattr(remote, "remote_decrypt")


def test_version_bumped():
    import ctu
    import claude_usage_tracker as shim
    assert ctu.__version__ == shim.__version__ == "0.4.0"
    toml = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.4.0"' in toml


@pytest.mark.parametrize("argv", [["--version"]])
def test_shim_main(argv, capsys):
    import claude_usage_tracker as shim
    assert shim.main(argv) == 0
    assert "0.4.0" in capsys.readouterr().out
