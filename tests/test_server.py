"""Loopback dashboard server: Origin/Host guard on every GET + POST, read-only pairing QR."""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_usage_tracker as m  # noqa: E402


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "REMOTE_PATH", tmp_path / "remote.json")
    monkeypatch.setattr(m, "_remote_id_cache", None)
    monkeypatch.setattr(m, "CONFIG_PATH", tmp_path / "config.json")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), m.DashboardHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def _get(port, path, headers=None, method="GET", body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=body,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_get_allows_loopback_callers(server):
    assert _get(server, "/")[0] == 200                                   # dashboard page
    assert _get(server, "/widget")[0] == 200                             # overlays
    st, body = _get(server, "/api/usage", {"Referer": f"http://127.0.0.1:{server}/"})
    assert st == 200 and "windows" in json.loads(body)
    assert _get(server, "/api/usage", {"Host": f"localhost:{server}"})[0] == 200


def test_get_rejects_rebinding_and_cross_origin(server):
    assert _get(server, "/api/usage", {"Host": "evil.example:8787"})[0] == 403
    assert _get(server, "/api/usage", {"Origin": "https://evil.example"})[0] == 403
    assert _get(server, "/api/pair-qr", {"Referer": "https://evil.example/x"})[0] == 403
    assert _get(server, "/api/team/whoami", {"Host": "evil.example"})[0] == 403
    assert _get(server, "/api/refresh", {"Origin": "https://evil.example"}, "POST", b"{}")[0] == 403


def test_pair_qr_get_never_creates_identity(server, tmp_path):
    st, _ = _get(server, "/api/pair-qr")
    assert st == 404                                                     # nothing to show yet
    assert not (tmp_path / "remote.json").exists()                       # and no key minted by a GET


def test_embedded_js_parses():
    """The dashboard/widget scripts are Python strings — make sure they still parse."""
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    for html in (m.DASHBOARD_HTML, m.WIDGET_HTML):
        js = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
        r = subprocess.run([node, "--check", "-"], input=js, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_dashboard_static_contracts():
    d = m.DASHBOARD_HTML
    assert ".sdot2{" in d                                                # Home status dot is styled
    assert "t.name" not in d and "esc(t.team" in d                       # "Member of <team>"
    assert "escrow" not in d                                             # escrow is gone
    assert '$("cd-"' not in d                                            # dead countdown lookup removed
    assert ":focus-visible" in d and ":focus-visible" in m.WIDGET_HTML
    assert "outline:none" not in m.WIDGET_HTML
