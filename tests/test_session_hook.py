"""Session-waiting alerts: idle-hook install/remove + the --session-hook forwarder."""
import io
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_usage_tracker as m  # noqa: E402


def test_install_migrates_to_stop_and_preserves_other_hooks(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(m, "CLAUDE_SETTINGS", settings)
    settings.write_text(json.dumps({
        "model": "opus",
        "hooks": {"Notification": [
            {"matcher": "permission_prompt", "hooks": [{"type": "command", "command": "echo hi"}]},
            {"matcher": "idle_prompt", "hooks": [{"type": "command", "command": "old --session-hook"}]},
        ]},
    }), encoding="utf-8")

    assert m.install_session_hook() is True
    d = json.loads(settings.read_text(encoding="utf-8"))
    assert d["model"] == "opus"                                     # unrelated top-level key kept
    notif = d["hooks"]["Notification"]
    assert any(g.get("matcher") == "permission_prompt" for g in notif)   # unrelated hook kept
    assert all("--session-hook" not in h.get("command", "")              # legacy idle hook migrated out
               for g in notif for h in g.get("hooks", []))
    ours = [h for g in d["hooks"]["Stop"] for h in g.get("hooks", []) if "--session-hook" in h.get("command", "")]
    assert len(ours) == 1                                            # now lives on the Stop event
    assert settings.with_name("settings.json.cut-bak").exists()      # backup made

    m.install_session_hook()                                         # idempotent
    stop2 = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["Stop"]
    assert sum(1 for g in stop2 for h in g.get("hooks", []) if "--session-hook" in h.get("command", "")) == 1

    m.remove_session_hook()
    allhooks = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
    assert all("--session-hook" not in h.get("command", "")
               for ev in allhooks.values() for g in ev for h in g.get("hooks", []))
    assert any(g.get("matcher") == "permission_prompt" for g in allhooks.get("Notification", []))  # still there


def test_install_creates_settings_when_absent(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(m, "CLAUDE_SETTINGS", settings)
    assert m.install_session_hook() is True
    d = json.loads(settings.read_text(encoding="utf-8"))
    g = d["hooks"]["Stop"][0]
    assert "matcher" not in g and "--session-hook" in g["hooks"][0]["command"]


def test_remove_when_only_ours_drops_the_block(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(m, "CLAUDE_SETTINGS", settings)
    m.install_session_hook()
    m.remove_session_hook()
    d = json.loads(settings.read_text(encoding="utf-8"))
    assert "hooks" not in d                                          # cleaned up entirely


def test_run_session_hook_posts_to_tray(tmp_path, monkeypatch):
    got = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            got["path"] = self.path
            got["body"] = json.loads(self.rfile.read(n))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(m, "PORT_PATH", tmp_path / "server_port")
        (tmp_path / "server_port").write_text(str(port), encoding="utf-8")
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
            {"cwd": "C:/Dev/acme/web-app", "session_id": "sess-1",
             "stop_hook_active": False, "hook_event_name": "Stop"})))
        assert m.run_session_hook() == 0
        time.sleep(0.2)
    finally:
        srv.shutdown()
    assert got["path"] == "/api/session-waiting"
    assert got["body"]["cwd"] == "C:/Dev/acme/web-app"
    assert got["body"]["session_id"] == "sess-1"
    assert got["body"]["stop_hook_active"] is False


def test_run_session_hook_no_port_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "PORT_PATH", tmp_path / "absent")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert m.run_session_hook() == 0          # no server, no crash
