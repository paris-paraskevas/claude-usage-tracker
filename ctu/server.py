"""Local API on 127.0.0.1 (ThreadingHTTPServer). Every route sits behind the loopback
Origin/Host guard, so a web page (CSRF) or a DNS-rebinding attack can't read or drive it."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ctu import io, paths, remote

SSE_HEARTBEAT_S = 15
PAGES = {"/": "dashboard.html", "/index.html": "dashboard.html", "/overlay": "overlay.html", "/flyout": "flyout.html"}
CTYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".json": "application/json", ".map": "application/json", ".svg": "image/svg+xml",
          ".png": "image/png", ".ico": "image/x-icon", ".webp": "image/webp", ".jpg": "image/jpeg",
          ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf", ".txt": "text/plain; charset=utf-8"}
_LOCAL = ("127.0.0.1", "localhost", "::1")


def safe_asset(root: Path, name: str):
    """root/name if it stays inside root and is a file, else None (no traversal, no absolute paths)."""
    if not name or "\\" in name or "\x00" in name or name.startswith("/") or ":" in name:
        return None
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    try:
        root = root.resolve()
        p = root.joinpath(*parts).resolve()
        p.relative_to(root)
    except Exception:
        return None
    return p if p.is_file() else None


class Handler(BaseHTTPRequestHandler):
    engine = None                       # set by make_server
    timeout = 10                        # drop slow/hung local clients (SSE clears it)
    server_version = "ClaudeUsageTracker"

    def log_message(self, *args):
        pass

    # ---- plumbing ----
    def _send(self, code: int, ctype: str, body: bytes, extra: dict | None = None) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, "application/json", json.dumps(obj).encode("utf-8"))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n <= 0:
                return {}
            if n > 100_000:
                return None
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None

    def _origin_ok(self) -> bool:
        """A genuine caller is our own page (Origin/Referer on our loopback host) or a non-browser
        client (no Origin). A foreign Origin/Referer or a non-loopback Host is rejected."""
        for hdr in ("Origin", "Referer"):
            v = self.headers.get(hdr)
            if v:
                try:
                    if urlsplit(v).hostname not in _LOCAL:
                        return False
                except Exception:
                    return False
        host = (self.headers.get("Host") or "").strip()
        if host.startswith("["):
            host = host[1:].split("]", 1)[0]
        else:
            host = host.rsplit(":", 1)[0]
        return not (host and host not in _LOCAL)

    def _guard(self) -> bool:
        if self.client_address[0] in ("127.0.0.1", "::1") and self._origin_ok():
            return True
        self._send(403, "text/plain", b"forbidden")
        return False

    def _query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    # ---- GET ----
    def do_GET(self):
        if not self._guard():
            return
        path = urlsplit(self.path).path
        e = self.engine
        if path in PAGES:
            return self._static(PAGES[path])
        if path.startswith("/assets/"):
            return self._static("assets/" + path[len("/assets/"):], asset=True)
        if path == "/api/v2/state":
            return self._json(e.snapshot())
        if path == "/api/v2/events":
            return self._sse()
        if path == "/api/v2/history":
            return self._json(e.history_view(self._query().get("window", "five_hour")))
        if path == "/api/v2/alltime":
            return self._json(e.alltime_view(self._query().get("range", "7d")))
        if path == "/api/v2/team/ledger":
            return self._json(e.ledger_view(self._query().get("month", "")))
        if path == "/api/team/ledger":                                   # v1 (MCP)
            from ctu import team
            return self._json(team.ledger(self._query().get("month", "")))
        if path == "/api/v2/settings":
            from ctu import config
            return self._json({"schema": config.API_SCHEMA} if "schema" in self._query() else e.settings())
        if path in ("/api/v2/remote/qr", "/api/pair-qr"):
            png = remote.remote_pair_qr_png(e.cfg, create=False)       # a read never mints a key
            return self._send(200, "image/png", png) if png else self._send(404, "text/plain", b"pairing unavailable")
        if path == "/api/usage":
            return self._json(e.v1_snapshot())
        if path == "/api/team/overview":
            return self._json(self._team_overview())
        if path == "/api/team/whoami":
            from ctu.team import pool
            return self._json({"signed_in": pool.signed_in(), "email": pool.email(), "team": pool.team(),
                               "is_admin": pool.is_admin()})
        if path == "/favicon.ico":
            p = paths.ICO_PATH
            return self._send(200, "image/x-icon", p.read_bytes()) if p.exists() else self._send(204, "image/x-icon", b"")
        self._send(404, "text/plain", b"not found")

    def _static(self, rel: str, asset: bool = False) -> None:
        p = safe_asset(paths.ui_dir(), rel)
        if p is None:
            return self._send(404, "text/plain", b"not found")
        try:
            body = p.read_bytes()
        except Exception:
            return self._send(404, "text/plain", b"not found")
        ctype = CTYPES.get(p.suffix.lower(), "application/octet-stream")
        extra = {"Content-Security-Policy": "frame-ancestors 'none'"} if p.suffix == ".html" else None
        self._send(200, ctype, body, extra)

    def _team_overview(self) -> dict:
        from ctu import team
        from ctu.team import pool
        if not pool.signed_in():
            return {"error": "not_signed_in"}
        try:
            merged = team.supabase_team_overview(self.engine.cfg.get("team_tz"))
            return merged if merged else {"error": "not_signed_in"}
        except Exception as exc:
            return {"error": "unreachable", "detail": str(exc)}

    def _sse(self) -> None:
        """`event: state` with the full state on connect and on every rev change; a comment
        heartbeat every 15 s. Ends when the client goes away or the engine stops."""
        e = self.engine
        try:
            self.connection.settimeout(None)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True

            def send(st):
                data = json.dumps(st, separators=(",", ":"))
                self.wfile.write(f"event: state\nid: {st['rev']}\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()

            self.wfile.write(b"retry: 2000\n\n")
            st = e.snapshot()
            send(st)
            rev = st["rev"]
            while not e.stopping:
                new = e.wait_change(rev, SSE_HEARTBEAT_S)
                if e.stopping:
                    break
                if new is None:
                    self.wfile.write(b": hb\n\n")
                    self.wfile.flush()
                    continue
                st = e.snapshot()
                rev = st["rev"]
                send(st)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    # ---- POST / PATCH ----
    def do_POST(self):
        if not self._guard():
            return
        path = urlsplit(self.path).path
        e = self.engine
        body = self._body()
        if body is None:
            return self._json({"ok": False, "error": "invalid JSON body"}, 400)
        if path.startswith("/api/v2/actions/"):
            code, res = e.action(path[len("/api/v2/actions/"):], body)
            return self._json(res, code)
        if path in ("/api/v2/hooks/claude", "/api/session-waiting"):
            if path == "/api/session-waiting":                       # <= 0.3 hook clients: a Stop
                body = {"hook_event_name": "Stop", **body}
            e.on_hook(body)
            return self._json({"ok": True})
        if path == "/api/refresh":
            e.refresh()
            return self._json({"ok": True})
        if path == "/api/v2/settings":
            return self._patch_settings(body)
        self._send(404, "text/plain", b"not found")

    def do_PATCH(self):
        if not self._guard():
            return
        if urlsplit(self.path).path != "/api/v2/settings":
            return self._send(404, "text/plain", b"not found")
        body = self._body()
        if body is None:
            return self._json({"ok": False, "errors": {"": "invalid JSON body"}}, 400)
        self._patch_settings(body)

    def _patch_settings(self, body) -> None:
        ok, res = self.engine.patch_settings(body)      # the full settings object, or {ok: false, errors}
        self._json(res, 200 if ok else 400)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def make_server(engine, port: int = 0) -> Server:
    handler = type("BoundHandler", (Handler,), {"engine": engine})
    return Server(("127.0.0.1", port), handler)


def start(engine, port: int):
    """Bind the first free port in [port, port+10), serve on a daemon thread, publish the port."""
    for p in range(port, port + 10):
        try:
            srv = make_server(engine, p)
        except OSError:
            continue
        threading.Thread(target=srv.serve_forever, name="ctu-http", daemon=True).start()
        engine.port = p
        try:
            paths.ensure_data_dir()
            paths.PORT_PATH.write_text(str(p), encoding="utf-8")
        except Exception:
            pass
        io.log(f"local API on http://127.0.0.1:{p}/")
        return srv
    io.log("could not bind a local API port")
    return None
