"""Engine plumbing: the scheduler, change-only disk writes, preload, the usage-source policy."""
import json
import threading
import time

from conftest import feed

from ctu import engine as eng
from ctu import history, io, paths, usage


def test_scheduler_runs_one_job_per_kind_and_keeps_kicks():
    s = eng.Scheduler(workers=4)
    live, peak, runs = [0], [0], []
    gate = threading.Event()

    def job():
        live[0] += 1
        peak[0] = max(peak[0], live[0])
        runs.append(time.time())
        gate.wait(0.3)
        live[0] -= 1
    s.add("slow", job, interval=60)
    s.start()
    time.sleep(0.05)
    for _ in range(5):
        s.kick("slow")                  # while running: coalesced into one re-run
    time.sleep(0.9)
    s.stop()
    assert peak[0] == 1 and len(runs) == 2


def test_history_appends_only_on_change(data_dir):
    h = history.History()
    t0 = 1_000_000.0
    assert h.observe("five_hour", t0, 10.0)
    assert not h.observe("five_hour", t0 + 10, 10.0)          # unchanged: no disk write, no sample yet
    assert not h.observe("five_hour", t0 + 61, 10.0)          # unchanged: in-memory heartbeat sample
    assert h.observe("five_hour", t0 + 70, 11.0)
    lines = paths.HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x) for x in lines] == [[1000000, "five_hour", 10.0], [1000070, "five_hour", 11.0]]
    assert [v for _, v in h.samples("five_hour")] == [10.0, 10.0, 11.0]
    assert not h.observe("five_hour", t0 + 5, 50.0)           # out of order: ignored
    h2 = history.History()
    h2.load(t0 + 100)
    assert h2.samples("five_hour") == [(t0, 10.0), (t0 + 70, 11.0)]
    h3 = history.History()
    h3.load(t0 + history.KEEP_S + 80)                         # capped by time, not count
    assert h3.samples("five_hour") == []


def test_history_migrates_the_legacy_file(data_dir):
    paths.DATA_DIR.mkdir(parents=True)
    now = time.time()
    paths.LEGACY_HISTORY_PATH.write_text(json.dumps({"t": [now - 30, now - 20, now - 10],
                                                     "five_hour": [5, 5, 6], "seven_day": [1, 1, 1]}))
    h = history.History()
    h.load(now)
    assert [v for _, v in h.samples("five_hour")] == [5.0, 6.0] and len(h.samples("seven_day")) == 1
    assert paths.HISTORY_PATH.exists()


def test_ticks_write_nothing_when_nothing_changed(make_engine):
    e = make_engine()
    now = time.time()
    feed(e, "five_hour", [(now - 60, 30.0)], int(now + 3600))
    e.job_tick()
    sigs = {p: io.file_sig(p) for p in (paths.STATE_PATH, paths.HISTORY_PATH, paths.SNAPSHOT_PATH, paths.ALLTIME_PATH)}
    for _ in range(5):
        e.job_tick()
    assert {p: io.file_sig(p) for p in sigs} == sigs
    assert not paths.SNAPSHOT_PATH.exists()                   # preload: on exit and every 5 min only


def test_state_and_preload_survive_a_restart(make_engine):
    e = make_engine()
    now = time.time()
    feed(e, "five_hour", [(now - 1800 + i * 60, 60 + i) for i in range(30)], int(now + 3600))
    assert e.state["decision"]["level"] == "will_hit"
    e.stop()
    assert json.loads(paths.STATE_PATH.read_text())["levels"]["five_hour"]["level"] == "will_hit"
    e2 = make_engine()
    st = e2.snapshot()
    assert st["limits"][0]["used_pct"] == 89.0 and st["source"]["updated_at"] == int(now - 1800 + 29 * 60)
    assert st["alerts"][0]["kind"] == "will_hit" and e2._deliveries.empty()   # no re-toast after restart


def test_usage_api_policy(make_engine, monkeypatch):
    calls = []
    now = time.time()

    def fake(timeout):
        calls.append(time.time())
        return usage.FetchResult(True, windows={
            "five_hour": {"pct": 12.0, "resets_at": int(now + 3600)},
            "seven_day_opus": {"pct": 58.0, "resets_at": int(now + 86400)}},
            extra={"enabled": True, "used": 1.0, "limit": 10.0, "currency": "EUR", "pct": 10.0})
    monkeypatch.setattr(usage, "fetch_usage", fake)
    e = make_engine()
    e.job_usage()
    assert len(calls) == 1                                    # nothing fresh: fall back to the API
    e.job_usage()
    assert len(calls) == 1                                    # ...at most every 5 min
    with e.lock:
        e.sl_ts = time.time()                                 # the statusline is live now
        e.api_last = time.time() - 400
    e.job_usage()
    assert len(calls) == 1                                    # extras only every 30 min
    e.refresh()
    with e.lock:
        e.api_last = time.time() - 31
    e.job_usage()
    assert len(calls) == 2                                    # a manual refresh forces one (30 s floor)
    ids = [l["id"] for l in e.state["limits"]]
    assert "seven_day_opus" in ids and e.state["overage"]["pct"] == 10.0   # scoped windows kept with statusline


def test_failed_fetch_marks_stale_with_error(make_engine, monkeypatch):
    e = make_engine()
    now = time.time()
    feed(e, "five_hour", [(now - 400, 30.0)], int(now + 3600), src="api")
    e.apply_fetch(usage.FetchResult(False, error="Rate limited by the API (429)"), now)
    src = e.snapshot()["source"]
    assert src["stale"] is True and src["error"] == "Rate limited by the API (429)"
    e.apply_fetch(usage.FetchResult(False, error="Auth rejected (HTTP 401)", token_state="expired"), now + 1)
    assert e.snapshot()["source"]["auth"] == "expired"


def test_v1_snapshot_is_built_from_state(make_engine):
    e = make_engine()
    now = time.time()
    feed(e, "five_hour", [(now - 60, 30.0)], int(now + 3600))
    s = e.v1_snapshot()
    assert s["windows"][0]["pct"] == 30.0 and s["ok"] is True and s["decision"]["title"] == "On pace"
    assert s["team"]["in_team"] is False and s["ui"] == {"status_components": [], "remote_transcript": False}


def test_start_notifies_listeners_off_the_lock_and_stop_persists(make_engine):
    e = make_engine()
    seen, held = [], []
    e.listeners.append(lambda st: (seen.append(st["rev"]), held.append(e.lock._is_owned())))
    e.start()
    try:
        now = time.time()
        feed(e, "five_hour", [(now - 30, 12.0)], int(now + 3600))
        deadline = time.time() + 3
        while not seen and time.time() < deadline:
            time.sleep(0.02)
        assert seen and seen[-1] == e.rev and held == [False] * len(held)
    finally:
        e.stop()
    assert paths.STATE_PATH.exists() and paths.SNAPSHOT_PATH.exists()


def test_start_upgrades_a_03_stop_only_hook(make_engine):
    paths.CLAUDE_SETTINGS.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": '"C:/old/pythonw.exe" "C:/old/claude_usage_tracker.py" --session-hook'}]}]}}))
    e = make_engine(session_hook=True)
    e.start()
    e.stop()
    d = json.loads(paths.CLAUDE_SETTINGS.read_text())
    assert {"Stop", "Notification", "UserPromptSubmit", "SessionEnd"} <= set(d["hooks"])
    assert sum("--session-hook" in h["command"] for g in d["hooks"]["Stop"] for h in g["hooks"]) == 1
