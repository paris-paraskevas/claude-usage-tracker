"""Local API: loopback guard on every route, static UI files, state + SSE, settings, actions,
hooks and the v1 adapters (MCP / Android)."""
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from conftest import feed

from ctu import paths, server


class StubHost:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, a))


@pytest.fixture
def api(make_engine):
    e = make_engine()
    e.host = StubHost()
    srv = server.make_server(e, 0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    e.port = srv.server_address[1]
    yield e
    e.stopping = True
    with e.lock:
        e.changed.notify_all()
    srv.shutdown()


def call(e, path, headers=None, method="GET", body=None):
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    req = urllib.request.Request(f"http://127.0.0.1:{e.port}{path}", method=method, data=data,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as err:
        return err.code, err.read(), dict(err.headers)


def jcall(e, *a, **k):
    st, body, _ = call(e, *a, **k)
    return st, json.loads(body)


# ---- guard ----

GUARDED = [("GET", "/"), ("GET", "/overlay"), ("GET", "/assets/app.js"), ("GET", "/api/v2/state"),
           ("GET", "/api/v2/events"), ("GET", "/api/v2/history"), ("GET", "/api/v2/alltime"),
           ("GET", "/api/v2/settings"), ("GET", "/api/v2/team/ledger?month=2026-10"), ("GET", "/api/v2/remote/qr"),
           ("GET", "/api/usage"), ("GET", "/api/pair-qr"), ("GET", "/api/team/whoami"),
           ("POST", "/api/v2/actions/refresh"), ("POST", "/api/v2/hooks/claude"), ("POST", "/api/refresh"),
           ("PATCH", "/api/v2/settings")]


@pytest.mark.parametrize("method,path", GUARDED)
@pytest.mark.parametrize("hdr", [{"Origin": "https://evil.example"}, {"Host": "evil.example:8787"},
                                 {"Referer": "https://evil.example/x"}])
def test_every_route_rejects_cross_origin_and_rebinding(api, method, path, hdr):
    st, _, _ = call(api, path, hdr, method, {} if method != "GET" else None)
    assert st == 403


def test_loopback_callers_pass(api):
    assert jcall(api, "/api/v2/state", {"Referer": f"http://127.0.0.1:{api.port}/"})[0] == 200
    assert jcall(api, "/api/v2/state", {"Origin": f"http://localhost:{api.port}"})[0] == 200
    assert jcall(api, "/api/usage", {"Host": f"localhost:{api.port}"})[0] == 200


def test_pair_qr_get_never_creates_identity(api):
    assert call(api, "/api/v2/remote/qr")[0] == 404
    assert call(api, "/api/pair-qr")[0] == 404
    assert not paths.REMOTE_PATH.exists()


# ---- static UI ----

@pytest.fixture
def ui(tmp_path, monkeypatch):
    root = tmp_path / "ui"
    (root / "assets" / "fonts").mkdir(parents=True)
    for name in ("dashboard.html", "overlay.html", "flyout.html"):
        (root / name).write_text(f"<!doctype html><title>{name}</title>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("export const x = 1;", encoding="utf-8")
    (root / "assets" / "app.css").write_text("body{}", encoding="utf-8")
    (root / "assets" / "fonts" / "a.woff2").write_bytes(b"wOF2")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    monkeypatch.setattr(paths, "ui_dir", lambda: root)
    return root


@pytest.mark.parametrize("path,ctype,needle", [
    ("/", "text/html", b"dashboard.html"), ("/overlay", "text/html", b"overlay.html"),
    ("/flyout", "text/html", b"flyout.html"), ("/assets/app.js", "text/javascript", b"export"),
    ("/assets/app.css", "text/css", b"body"), ("/assets/fonts/a.woff2", "font/woff2", b"wOF2"),
])
def test_static_files(api, ui, path, ctype, needle):
    st, body, headers = call(api, path)
    assert st == 200 and headers["Content-Type"].startswith(ctype) and needle in body
    assert headers["X-Content-Type-Options"] == "nosniff"


def _raw_get(port, path):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
        return s.recv(4096).split(b" ", 2)[1]


@pytest.mark.parametrize("path", ["/assets/../secret.txt", "/assets/..%2fsecret.txt", "/assets/%2e%2e/secret.txt",
                                  "/assets/fonts/../../dashboard.html", "/assets//etc/passwd", "/assets/C:/x",
                                  "/assets/..\\secret.txt", "/assets/", "/assets/fonts", "/assets/missing.js"])
def test_static_rejects_traversal(api, ui, path):
    assert _raw_get(api.port, path) == b"404"


def test_safe_asset(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.js").write_text("1")
    assert server.safe_asset(tmp_path, "a/x.js") == (tmp_path / "a" / "x.js").resolve()
    for bad in ("../x", "a/../../x", "/etc/passwd", "C:/x", "a\\x.js", "a//x.js", "", "a/./x.js", "a"):
        assert server.safe_asset(tmp_path, bad) is None


def test_static_404_when_ui_missing(api, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "ui_dir", lambda: tmp_path / "nothing")
    assert call(api, "/")[0] == 404 and call(api, "/assets/app.js")[0] == 404


# ---- state, SSE, lazy topics ----

def test_state_and_history(api):
    now = time.time()
    feed(api, "five_hour", [(now - 1200 + i * 60, 30 + i * 0.2) for i in range(20)], int(now + 3600))
    st, s = jcall(api, "/api/v2/state")
    assert st == 200 and s["schema"] == 2 and s["limits"][0]["id"] == "five_hour"
    st, h = jcall(api, "/api/v2/history?window=five_hour")
    assert len(h) == 20 and h[0] == [int(now - 1200), 30.0]                 # [[t, used_pct], ...]
    assert jcall(api, "/api/v2/history?window=seven_day")[1] == []
    st, a = jcall(api, "/api/v2/alltime?range=30d")
    assert st == 200 and a["range"] == "30d" and len(a["series"]) == 30
    assert a["usage"]["t"] and a["usage"]["five_hour"][-1] == pytest.approx(33.8)
    assert jcall(api, "/api/v2/team/ledger?month=2026-10")[1] == {"ok": False, "error": "signed_out"}
    assert jcall(api, "/api/team/ledger?month=2026-10")[1] == {"error": "not_signed_in"}   # v1 (MCP)


def _sse_events(port, n, timeout=8, after=None):
    """Read `n` SSE `state` events (calling `after()` once the first arrives)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall(f"GET /api/v2/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
    buf, events, fired = b"", [], False
    deadline = time.time() + timeout
    while len(events) < n and time.time() < deadline:
        buf += s.recv(65536)
        while b"\n\n" in buf:
            chunk, buf = buf.split(b"\n\n", 1)
            if b"event: state" in chunk:
                data = [ln[6:] for ln in chunk.split(b"\n") if ln.startswith(b"data: ")][0]
                events.append(json.loads(data))
                if after and not fired:
                    fired = True
                    after()
    s.close()
    return events


def test_sse_sends_state_on_connect_and_on_rev_change(api):
    def change():
        api.on_hook({"hook_event_name": "Stop", "session_id": "s9", "cwd": "C:\\Dev\\x"})
    ev = _sse_events(api.port, 2, after=change)
    assert len(ev) == 2 and ev[0]["schema"] == 2
    assert ev[1]["rev"] > ev[0]["rev"] and ev[1]["sessions"][0]["state"] == "waiting"


def test_sse_heartbeat_and_client_disconnect(api, monkeypatch):
    monkeypatch.setattr(server, "SSE_HEARTBEAT_S", 0.2)
    s = socket.create_connection(("127.0.0.1", api.port), timeout=5)
    s.sendall(f"GET /api/v2/events HTTP/1.1\r\nHost: 127.0.0.1:{api.port}\r\n\r\n".encode())
    buf = b""
    deadline = time.time() + 5
    while b": hb" not in buf and time.time() < deadline:
        buf += s.recv(65536)
    assert b"text/event-stream" in buf and b": hb" in buf
    s.close()                                                    # the handler must notice and exit
    time.sleep(0.6)
    assert jcall(api, "/api/v2/state")[0] == 200                 # server still healthy


# ---- settings ----

def test_settings_get_and_patch(api):
    st, s = jcall(api, "/api/v2/settings")
    assert st == 200 and s["overlay"]["shown"] is True and s["about"]["version"] == "0.4.0"
    assert set(s) == {"alerts", "quiet_hours", "phone_push", "status_components", "status_components_available",
                      "overlay", "remote", "team", "data", "about"}
    assert s["data"] == {"refresh_s": 300, "session_hook": False, "statusline_installed": False}
    st, r = jcall(api, "/api/v2/settings", method="PATCH",
                  body={"quiet_hours": {"enabled": True, "start": "23:00"}, "overlay": {"opacity": 70},
                        "remote": {"transcript": True}, "team": {"tz": "UTC"}, "data": {"refresh_s": 120},
                        "about": {"version": "ignored", "update_check": False}})
    assert st == 200 and r["quiet_hours"] == {"enabled": True, "start": "23:00", "end": "08:00"}
    assert r["overlay"]["opacity"] == 70 and r["overlay"]["shown"] is True and r["remote"]["transcript"] is True
    assert r["team"]["tz"] == "UTC" and r["data"]["refresh_s"] == 120 and r["about"] == {"version": "0.4.0",
                                                                                          "update_check": False}
    saved = json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["quiet_hours"]["start"] == "23:00" and saved["overlay"]["opacity"] == 70
    assert saved["team_tz"] == "UTC" and saved["api_fallback_interval_seconds"] == 120
    assert ("apply_overlay", (api.cfg["overlay"],)) in api.host.calls
    assert "remote" in jcall(api, "/api/v2/settings?schema=1")[1]["schema"]


@pytest.mark.parametrize("patch,key", [
    ({"quiet_hours": {"start": "25:00"}}, "quiet_hours.start"),
    ({"overlay": {"opacity": 5}}, "overlay.opacity"),
    ({"overlay": {"fields": ["5h", "5h"]}}, "overlay.fields"),
    ({"overlay": {"fields": ["nope"]}}, "overlay.fields[0]"),
    ({"alerts": {"will_hit": "yes"}}, "alerts.will_hit"),
    ({"alerts": {"bogus": True}}, "alerts.bogus"),
    ({"remote": {"relay_url": "http://insecure.example"}}, "remote.relay_url"),
    ({"remote": "on"}, "remote"),
    ({"team": {"tz": "Not A Zone"}}, "team.tz"),
    ({"data": {"refresh_s": 5}}, "data.refresh_s"),
    ({"data": {"statusline_installed": "yes"}}, "data.statusline_installed"),
    ({"dashboard_port": 1}, "dashboard_port"),                     # internal keys aren't settings
    ({"status_components": "x"}, "status_components"),
])
def test_settings_validation_rejects_and_applies_nothing(api, patch, key):
    before = json.dumps(api.cfg, sort_keys=True)
    st, r = jcall(api, "/api/v2/settings", method="PATCH", body={**patch, "phone_push": False})
    assert st == 400 and r["ok"] is False and key in r["errors"] and r["error"]
    assert json.dumps(api.cfg, sort_keys=True) == before          # atomic: valid parts not applied either


def test_settings_session_hook_installs_the_hook(api):
    st, r = jcall(api, "/api/v2/settings", method="PATCH", body={"data": {"session_hook": True}})
    assert st == 200 and r["data"]["session_hook"] is True
    d = json.loads(paths.CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    assert {"Stop", "Notification", "UserPromptSubmit", "SessionEnd"} <= set(d["hooks"])
    assert api.snapshot()["app"]["hook"]["installed"] is True
    paths.CLAUDE_SETTINGS.write_text("{broken", encoding="utf-8")
    st, r = jcall(api, "/api/v2/settings", method="PATCH", body={"data": {"session_hook": False}})
    assert st == 400 and "data.session_hook" in r["errors"] and api.cfg["session_hook"] is True


def test_settings_statusline_toggle(api):
    st, r = jcall(api, "/api/v2/settings", method="PATCH", body={"data": {"statusline_installed": True}})
    assert st == 200 and r["data"]["statusline_installed"] is True and r["data"]["session_hook"] is True
    st, r = jcall(api, "/api/v2/settings", method="PATCH", body={"data": {"statusline_installed": False}})
    assert st == 200 and r["data"]["statusline_installed"] is False
    assert "statusLine" not in json.loads(paths.CLAUDE_SETTINGS.read_text(encoding="utf-8"))


# ---- actions ----

def test_unknown_action_and_bad_body(api):
    assert jcall(api, "/api/v2/actions/format_disk", method="POST", body={})[0] == 404
    assert call(api, "/api/v2/actions/refresh", method="POST", body=b"{nope")[0] == 400


def test_mute_actions(api):
    st, r = jcall(api, "/api/v2/actions/mute", method="POST", body={"for": "hour"})
    assert r["ok"] and r["mute"]["reason"] == "hour" and r["mute"]["until"] > time.time() + 3500
    assert api.snapshot()["mute"]["reason"] == "hour"
    assert jcall(api, "/api/v2/actions/mute", method="POST", body={"for": "off"})[1]["mute"]["until"] is None
    assert not jcall(api, "/api/v2/actions/mute", method="POST", body={"for": "ever"})[1]["ok"]


def test_overlay_and_window_actions_reach_the_host(api):
    st, r = jcall(api, "/api/v2/actions/overlay", method="POST", body={"show": False, "pin": True})
    assert r["ok"] and r["overlay"]["shown"] is False and r["overlay"]["pinned"] is True
    assert api.snapshot()["app"]["overlay"] == {"shown": False, "pinned": True}
    st, r = jcall(api, "/api/v2/actions/overlay_resize", method="POST", body={"w": 5000, "h": 3})
    assert r == {"ok": True, "w": 800, "h": 24}                    # clamped
    assert ("resize_overlay", (800, 24)) in api.host.calls and api.cfg["overlay_size"] == [800, 24]
    assert not jcall(api, "/api/v2/actions/overlay_resize", method="POST", body={"w": "x"})[1]["ok"]
    jcall(api, "/api/v2/actions/flyout_close", method="POST", body={})
    jcall(api, "/api/v2/actions/open_dashboard", method="POST", body={"view": "sessions"})
    jcall(api, "/api/v2/actions/open_dashboard", method="POST", body={"view": "x'); alert(1"})
    assert ("close_flyout", ()) in api.host.calls
    assert ("open_dashboard", ("sessions",)) in api.host.calls
    assert ("open_dashboard", ("xalert1",)) in api.host.calls      # sanitised view


def test_install_statusline_action(api):
    st, r = jcall(api, "/api/v2/actions/install_statusline", method="POST", body={})
    assert r["ok"] and r["installed"] and r["hook"]                # the session hooks come with it
    st_ = api.snapshot()["app"]
    assert st_["statusline"]["installed"] and st_["hook"]["installed"] and api.cfg["session_hook"] is True
    paths.CLAUDE_SETTINGS.write_text(json.dumps({"statusLine": {"type": "command", "command": "mine.sh"}}))
    st, r = jcall(api, "/api/v2/actions/install_statusline", method="POST", body={})
    assert not r["ok"] and r["error"] == "existing" and r["existing"] == "mine.sh" and r["message"]
    assert r["hook"] and json.loads(paths.CLAUDE_SETTINGS.read_text())["statusLine"]["command"] == "mine.sh"
    assert api.snapshot()["app"]["statusline"] == {"installed": False, "existing": "mine.sh"}


# ---- hooks ----

def test_hook_endpoint_drives_session_state(api):
    post = lambda b: jcall(api, "/api/v2/hooks/claude", method="POST", body=b)   # noqa: E731
    post({"hook_event_name": "Notification", "session_id": "s1", "notification_type": "permission_prompt",
          "cwd": "C:\\Dev\\proj"})
    s = api.snapshot()["sessions"][0]
    assert s["state"] == "needs_approval" and s["project"] == "proj"
    assert any(a["kind"] == "needs_approval" for a in api.snapshot()["alerts"])
    post({"hook_event_name": "UserPromptSubmit", "session_id": "s1"})
    assert not any(s["state"] == "needs_approval" for s in api.snapshot()["sessions"])
    jcall(api, "/api/session-waiting", method="POST", body={"session_id": "s2", "cwd": "C:\\Dev\\old"})   # 0.3 client
    assert any(s["id"] == "s2" and s["state"] == "waiting" for s in api.snapshot()["sessions"])


# ---- v1 adapters ----

SNAPSHOT_KT_FIELDS = {"ok", "error", "token_state", "account", "subscription", "updated_at", "verdict", "windows",
                      "context", "sessions", "status", "alltime", "extra", "team", "team_overview", "history",
                      "ui", "decision", "source"}


def test_v1_usage_keeps_the_phone_and_mcp_contract(api):
    now = time.time()
    with api.lock:
        api.extra = {"enabled": True, "used": 5.0, "limit": 50.0, "currency": "EUR", "pct": 10.0}
        api.status_raw = {"indicator": "minor", "description": "Degraded", "url": "u",
                          "components": [{"name": "API", "status": "degraded_performance"}]}
    feed(api, "five_hour", [(now - 1800 + i * 60, 40 + i) for i in range(31)], int(now + 3600))
    feed(api, "seven_day", [(now - 7200, 20.0)], int(now + 86400))
    st, d = jcall(api, "/api/usage")
    assert SNAPSHOT_KT_FIELDS <= set(d)
    w = d["windows"][0]
    assert {"key", "label", "pct", "resets_at", "color", "rate_per_hour", "eta_seconds"} <= set(w)
    assert w["key"] == "five_hour" and w["label"] == "5h" and w["resets_at"] == int(now + 3600) * 1000   # ms
    assert w["level"] == "will_hit" and w["pace"]["eta_100"] and w["elapsed_pct"] is not None
    assert w["eta_seconds"] == pytest.approx(w["pace"]["eta_100"] - now, abs=2)
    assert d["decision"]["binding"] == "five_hour" and d["decision"]["title"].startswith("Limit around")
    assert d["verdict"] == {"level": "stop", "color": "#dd5f42", "text": d["decision"]["title"]}
    assert set(d["source"]) == {"kind", "updated_at", "age_s", "stale", "auth", "error"}
    assert abs(d["updated_at"] - time.time()) < 3                   # build time = desktop liveness
    assert d["status"]["components"][0]["name"] == "API" and d["ui"]["status_components"] == []
    assert d["history"]["t"] and len(d["history"]["t"]) == len(d["history"]["five_hour"]) == len(d["history"]["seven_day"])
    assert d["extra"]["currency"] == "EUR"
    assert jcall(api, "/api/team/overview")[1] == {"error": "not_signed_in"}
    assert jcall(api, "/api/refresh", method="POST", body={})[1] == {"ok": True}
