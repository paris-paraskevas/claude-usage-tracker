"""Claude Code hooks: settings.json install/remove, the `--session-hook` client and its fast path."""
import io
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ctu import hookclient, hooks, install, paths

ROOT = Path(__file__).resolve().parent.parent


def _ours(groups):
    return [h for g in groups or [] for h in g.get("hooks", []) if "--session-hook" in h.get("command", "")]


def test_install_registers_all_events_and_preserves_other_hooks(data_dir):
    settings = paths.CLAUDE_SETTINGS
    settings.write_text(json.dumps({
        "model": "opus",
        "hooks": {"Notification": [
            {"matcher": "permission_prompt", "hooks": [{"type": "command", "command": "echo hi"}]},
            {"matcher": "idle_prompt", "hooks": [{"type": "command", "command": "old --session-hook"}]},
        ]},
    }), encoding="utf-8")

    assert hooks.install_session_hook() is True
    d = json.loads(settings.read_text(encoding="utf-8"))
    assert d["model"] == "opus"                                     # unrelated top-level key kept
    notif = d["hooks"]["Notification"]
    assert any(h.get("command") == "echo hi" for g in notif for h in g.get("hooks", []))   # theirs kept
    assert not any(g.get("matcher") == "idle_prompt" for g in notif)                         # legacy one migrated out
    ours = [g for g in notif if _ours([g])]
    assert len(ours) == 1 and ours[0]["matcher"] == "permission_prompt"
    for ev in ("Stop", "UserPromptSubmit", "SessionEnd"):
        assert len(_ours(d["hooks"][ev])) == 1
    assert settings.with_name("settings.json.cut-bak").exists()     # backup made
    assert hooks.hook_installed()

    hooks.install_session_hook()                                    # idempotent
    d2 = json.loads(settings.read_text(encoding="utf-8"))
    for ev in hooks.HOOK_EVENTS:
        assert len(_ours(d2["hooks"][ev])) == 1

    hooks.remove_session_hook()
    allhooks = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
    assert all(not _ours(groups) for groups in allhooks.values())   # gone from every event
    assert any(g.get("matcher") == "permission_prompt" for g in allhooks.get("Notification", []))  # theirs stays
    assert not hooks.hook_installed()


def test_install_creates_settings_when_absent(data_dir):
    assert hooks.install_session_hook() is True
    d = json.loads(paths.CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    g = d["hooks"]["Stop"][0]
    assert "matcher" not in g and "--session-hook" in g["hooks"][0]["command"]


def test_remove_when_only_ours_drops_the_block(data_dir):
    hooks.install_session_hook()
    hooks.remove_session_hook()
    d = json.loads(paths.CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    assert "hooks" not in d                                          # cleaned up entirely


def test_install_refuses_unparseable_settings(data_dir):
    settings = paths.CLAUDE_SETTINGS
    broken = '{"model": "opus", "hooks": {'
    settings.write_text(broken, encoding="utf-8")
    assert hooks.install_session_hook() is False
    assert settings.read_text(encoding="utf-8") == broken           # untouched — not clobbered
    assert hooks.remove_session_hook() is False
    assert settings.read_text(encoding="utf-8") == broken
    settings.write_text("[1, 2]", encoding="utf-8")                  # valid JSON, wrong shape
    assert hooks.install_session_hook() is False
    assert settings.read_text(encoding="utf-8") == "[1, 2]"


def test_backup_keeps_the_original(data_dir):
    settings = paths.CLAUDE_SETTINGS
    original = json.dumps({"model": "opus"})
    settings.write_text(original, encoding="utf-8")
    hooks.install_session_hook()
    hooks.install_session_hook()
    hooks.remove_session_hook()
    assert settings.with_name("settings.json.cut-bak").read_text(encoding="utf-8") == original


def test_uninstall_paths_remove_hook_and_only_startup(data_dir, monkeypatch):
    removed = []
    monkeypatch.setattr(install, "remove_shortcuts", lambda *a: removed.append(list(a[0]) if a else "all"))
    hooks.install_session_hook()
    install.do_uninstall()
    assert removed == ["all"]
    assert "hooks" not in json.loads(paths.CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    removed.clear()
    install.uninstall_autostart()
    assert removed == [["Startup"]]                                  # desktop / start menu untouched


def test_remove_hook_cli_flag(data_dir, monkeypatch):
    import claude_usage_tracker as shim
    hooks.install_session_hook()
    assert hooks.install_statusline()["installed"]
    monkeypatch.setattr(sys, "argv", ["claude_usage_tracker.py", "--remove-hook"])
    assert shim.main() == 0
    d = json.loads(paths.CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    assert "hooks" not in d and "statusLine" not in d               # the uninstaller removes both


def test_hook_command_shape(monkeypatch):
    cmd = hooks.hook_command()
    assert cmd.endswith(" --session-hook") and "\\" not in cmd       # forward slashes for Git Bash
    assert hooks.statusline_command().endswith(" --statusline")


# ---- the hook client ----

class _Recorder:
    def __init__(self):
        self.got = []

        class H(BaseHTTPRequestHandler):
            def log_message(s, *a):
                pass

            def do_POST(s):
                n = int(s.headers.get("Content-Length", 0))
                self.got.append((s.path, json.loads(s.rfile.read(n)), s.headers.get("Origin")))
                s.send_response(200)
                s.send_header("Content-Length", "2")
                s.end_headers()
                s.wfile.write(b"{}")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]


@pytest.fixture
def recorder(data_dir):
    r = _Recorder()
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    paths.PORT_PATH.write_text(str(r.port), encoding="utf-8")
    yield r
    r.srv.shutdown()


@pytest.mark.parametrize("payload,expected", [
    ({"hook_event_name": "Stop", "session_id": "s1", "cwd": "C:/Dev/a", "stop_hook_active": False,
      "last_assistant_message": "SECRET"}, {"hook_event_name": "Stop", "session_id": "s1", "cwd": "C:/Dev/a",
                                            "stop_hook_active": False}),
    ({"hook_event_name": "Notification", "session_id": "s1", "notification_type": "permission_prompt",
      "message": "Claude needs your permission"}, {"hook_event_name": "Notification", "session_id": "s1",
                                                   "notification_type": "permission_prompt"}),
    ({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "SECRET"},
     {"hook_event_name": "UserPromptSubmit", "session_id": "s1"}),
    ({"hook_event_name": "SessionEnd", "session_id": "s1", "reason": "clear"},
     {"hook_event_name": "SessionEnd", "session_id": "s1", "reason": "clear"}),
])
def test_session_hook_forwards_minimal_fields(recorder, monkeypatch, payload, expected):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hookclient.run_session_hook() == 0
    time.sleep(0.1)
    path, body, origin = recorder.got[-1]
    assert path == "/api/v2/hooks/claude" and origin is None
    assert body == expected                                         # prompt / message text never sent


def test_session_hook_no_port_is_noop(data_dir, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"hook_event_name": "Stop"}'))
    assert hookclient.run_session_hook() == 0                      # no server, no crash
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert hookclient.run_session_hook() == 0


def test_action_uri_parsing():
    assert hookclient.parse_action_uri("claude-usage-tracker:open/now") == ("open_dashboard", {"view": "now"})
    assert hookclient.parse_action_uri('"claude-usage-tracker:mute/hour"') == ("mute", {"for": "hour"})
    assert hookclient.parse_action_uri("claude-usage-tracker:open") == ("open_dashboard", {})
    assert hookclient.parse_action_uri("claude-usage-tracker:mute/forever") is None
    assert hookclient.parse_action_uri("claude-usage-tracker:quit") is None
    assert hookclient.parse_action_uri("claude-usage-tracker:open/<script>") == ("open_dashboard", {"view": "script"})


def test_hook_fast_path_imports_nothing_heavy(tmp_path):
    """`--session-hook` runs on every turn: no http.server / urllib / argparse / supabase / GUI
    imports, and no data-dir creation."""
    env = {**os.environ, "LOCALAPPDATA": str(tmp_path / "lad")}
    code = ("import sys, json; sys.argv=['x','--session-hook']; import claude_usage_tracker as m; rc=m.main();"
            "print(json.dumps(sorted(sys.modules)))")
    r = subprocess.run([sys.executable, "-c", code], input='{"hook_event_name":"Stop","session_id":"s"}',
                       capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    mods = set(json.loads(r.stdout.strip().splitlines()[-1]))
    heavy = {"http.server", "http.client", "urllib.request", "argparse", "email", "ctu.engine", "ctu.team",
             "ctu.team.pool", "ctu.server", "webview", "pystray", "PIL", "winotify", "keyring", "nacl"}
    assert not (mods & heavy), mods & heavy
    assert not (tmp_path / "lad").exists()


def test_statusline_fast_path_imports_nothing_heavy(tmp_path):
    env = {**os.environ, "LOCALAPPDATA": str(tmp_path / "lad")}
    code = ("import sys, json; sys.argv=['x','--statusline']; import claude_usage_tracker as m; rc=m.main();"
            "sys.stdout.write(json.dumps(sorted(sys.modules)))")
    payload = {"session_id": "s", "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
               "rate_limits": {"five_hour": {"used_percentage": 12, "resets_at": int(time.time()) + 3600}}}
    r = subprocess.run([sys.executable, "-c", code], input=json.dumps(payload), capture_output=True,
                       text=True, cwd=str(ROOT), env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    line, mods = r.stdout.split("\n", 1)
    assert line.startswith("Opus") and "5h 12%" in line
    assert not (set(json.loads(mods)) & {"http.server", "urllib.request", "argparse", "ctu.engine", "PIL"})
    snap = json.loads((tmp_path / "lad" / "ClaudeUsageTracker" / "statusline.json").read_text(encoding="utf-8"))
    assert snap["rate_limits"]["five_hour"]["used_percentage"] == 12
