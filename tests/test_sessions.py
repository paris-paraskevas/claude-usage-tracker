"""Session states: hooks (Stop / Notification / UserPromptSubmit / SessionEnd) and log inference."""
import json
import time

from ctu import logtail, paths, sessions

NOW = time.time()


def rec(t, kind, sid="s1", model="claude-opus-5-5", ctx=0):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))
    base = {"timestamp": ts, "cwd": "C:\\Dev\\proj", "sessionId": sid}
    if kind == "prompt":
        return {**base, "type": "user", "message": {"role": "user", "content": "do it"}}
    if kind == "tool_result":
        return {**base, "type": "user", "message": {"role": "user", "content": [{"type": "tool_result"}]}}
    if kind == "interrupt":
        return {**base, "type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "[Request interrupted by user]"}]}}
    content = [{"type": "tool_use", "name": "Bash"}] if kind == "tool" else [{"type": "text", "text": "done"}]
    return {**base, "type": "assistant", "message": {
        "role": "assistant", "model": model, "content": content, "stop_reason": "tool_use" if kind == "tool" else "end_turn",
        "usage": {"input_tokens": ctx, "output_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}


def tracker(data_dir, records, sid="s1"):
    d = paths.PROJECTS_DIR / "C--Dev-proj"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    tr = sessions.SessionTracker()
    logtail.LogTailer([tr]).poll(NOW)
    return tr


def state(tr, sl=None):
    return tr.build(time.time(), sl)[0]


def test_inferred_from_the_log(data_dir):
    tr = tracker(data_dir, [rec(NOW - 100, "prompt"), rec(NOW - 90, "tool"), rec(NOW - 80, "tool_result")])
    s = state(tr)
    assert s["state"] == "working" and s["since"] == int(NOW - 100) and s["active"] and s["model"] == "claude-opus-5-5"
    tr = tracker(data_dir, [rec(NOW - 100, "prompt"), rec(NOW - 50, "end", ctx=150000)], sid="s2")
    s = next(x for x in tr.build(time.time()) if x["id"] == "s2")
    assert s["state"] == "waiting" and s["since"] == int(NOW - 50)
    assert s["context"] == {"pct": 75.0, "tokens": 150000, "window": 200000}


def test_interrupt_reads_as_waiting_and_old_turns_as_idle(data_dir):
    tr = tracker(data_dir, [rec(NOW - 100, "prompt"), rec(NOW - 60, "interrupt")])
    assert state(tr)["state"] == "waiting"
    tr = tracker(data_dir, [rec(NOW - 4 * 3600, "prompt"), rec(NOW - 4 * 3600 + 60, "tool")], sid="old")
    s = next(x for x in tr.build(time.time()) if x["id"] == "old")
    assert s["state"] == "idle" and not s["active"]


def test_hooks_drive_state(data_dir):
    tr = tracker(data_dir, [rec(NOW - 100, "prompt"), rec(NOW - 90, "tool")])
    assert tr.on_hook({"hook_event_name": "Notification", "session_id": "s1",
                       "notification_type": "permission_prompt"}, NOW)
    assert state(tr)["state"] == "needs_approval"
    assert not tr.on_hook({"hook_event_name": "Notification", "session_id": "s1",
                           "notification_type": "idle_prompt"}, NOW)                # other types ignored
    assert tr.on_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s1"}, NOW + 1)
    assert state(tr)["state"] == "working"
    assert tr.on_hook({"hook_event_name": "Stop", "session_id": "s1"}, NOW + 2)
    assert state(tr)["state"] == "waiting"
    assert not tr.on_hook({"hook_event_name": "Stop", "session_id": "s1", "stop_hook_active": True}, NOW + 3)
    assert tr.on_hook({"hook_event_name": "SessionEnd", "session_id": "s1", "reason": "clear"}, NOW + 4)
    assert state(tr)["state"] == "working"                                  # back to the log's view


def test_new_activity_clears_a_permission_prompt(data_dir):
    tr = tracker(data_dir, [rec(NOW - 100, "prompt")])
    tr.on_hook({"hook_event_name": "Notification", "session_id": "s1", "notification_type": "permission_prompt"},
               NOW - 50)
    with open(paths.PROJECTS_DIR / "C--Dev-proj" / "s1.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec(NOW - 10, "tool_result")) + "\n")
    logtail.LogTailer([tr]).poll(NOW)
    assert state(tr)["state"] == "working"


def test_statusline_window_and_ordering(data_dir):
    tr = tracker(data_dir, [rec(NOW - 100, "prompt"), rec(NOW - 90, "tool", ctx=300000)])
    assert state(tr)["context"]["window"] == 1_000_000                      # inferred: past 200k
    assert state(tr, {"s1": {"window": 2_000_000}})["context"]["window"] == 2_000_000   # statusline wins
    tr.on_hook({"hook_event_name": "Stop", "session_id": "zz", "cwd": "C:\\Dev\\other"}, time.time())
    rows = tr.build(time.time())
    assert [r["state"] for r in rows] == ["waiting", "working"] and rows[0]["project"] == "other"


def test_v1_rows_aggregate_per_project():
    s = [{"project": "a", "tokens_5h": 300, "active": True, "since": 10, "state": "working",
          "context": {"pct": 30.0, "tokens": 60000, "window": 200000}},
         {"project": "a", "tokens_5h": 100, "active": False, "since": 5, "state": "idle",
          "context": {"pct": 90.0, "tokens": 180000, "window": 200000}},
         {"project": "b", "tokens_5h": 100, "active": False, "since": 1, "state": "idle",
          "context": {"pct": 0.0, "tokens": 0, "window": 200000}}]
    rows = sessions.v1_rows(s, 100)
    assert rows[0] == {"name": "a", "tokens": 400, "active": True, "idle_s": 0, "context_pct": 30.0,
                       "context_tokens": 60000, "share": 80.0}
    assert rows[1]["name"] == "b" and rows[1]["idle_s"] == 99
