"""Dev harness for the v2 desktop UI (stdlib only).

Serves ctu/ui at the routes the engine uses and mocks every API route from
tests/ui/fixtures:

    python tests/ui/mock_server.py --variant will_hit --tick 5
    open http://127.0.0.1:8765/            (dashboard)
         http://127.0.0.1:8765/overlay     (overlay strip/panel)
         http://127.0.0.1:8765/flyout      (tray flyout)

Variants: will_hit ok watch limited stale gathering signed_out empty.
By default every epoch in the fixtures is shifted so the fixture's "now"
(14:48) is the real current time; --frozen keeps the fixture times (used by
the screenshot run, which also freezes the browser clock).

Dev-only control routes:
    GET /__variant?name=ok          switch variant (pushed to SSE clients)
    GET /__mode?fail_actions=1      toggle flags: fail_actions, ledger_error,
                                    alltime_notready, alltime_error, no_sse,
                                    settings_error, state_error
    GET /__drop                     close every open event stream
"""

from __future__ import annotations

import argparse
import copy
import json
import queue
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
UI = HERE.parents[1] / "ctu" / "ui"
FIXTURE_NOW = 1792496880
ATHENS = timezone(timedelta(hours=3))
VARIANTS = ["will_hit", "ok", "watch", "limited", "stale", "gathering", "signed_out", "empty"]
EPOCH_KEYS = {
    "generated_at", "updated_at", "resume_at", "resets_at", "eta_100", "since", "five_hour_resets_at",
    "at", "until", "last_sync", "first_ts", "last_ts", "expires_at",
}
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".json": "application/json"}


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class Mock:
    def __init__(self, variant: str, frozen: bool, tick: float):
        self.lock = threading.Lock()
        self.frozen = frozen
        self.tick = tick
        self.offset = 0 if frozen else int(time.time()) - FIXTURE_NOW
        self.flags = {k: False for k in ("fail_actions", "ledger_error", "alltime_notready", "alltime_error", "no_sse", "settings_error", "state_error")}
        self.settings = load("settings.json")
        self.settings.pop("_doc", None)
        self.clients: list[queue.Queue] = []
        self.set_variant(variant)

    # ---------- time shifting ----------
    def shift(self, obj):
        if self.offset == 0:
            return obj
        if isinstance(obj, dict):
            return {k: (v + self.offset if k in EPOCH_KEYS and isinstance(v, (int, float)) else self.shift(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.shift(v) for v in obj]
        return obj

    def now(self):
        return FIXTURE_NOW + self.offset

    # ---------- state ----------
    def set_variant(self, name: str):
        if name not in VARIANTS:
            raise ValueError(name)
        with self.lock:
            self.variant = name
            self.state = self.shift(load(f"state-{name}.json"))
        self.broadcast()

    def get_state(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def mutate(self, fn):
        with self.lock:
            fn(self.state)
            self.state["rev"] += 1
        self.broadcast()

    def broadcast(self):
        data = json.dumps(self.get_state())
        for q in list(self.clients):
            q.put(("state", data))

    def ticker(self):
        while True:
            time.sleep(self.tick)

            def bump(s):
                step = int(self.tick)
                s["generated_at"] += step
                if not s["source"]["stale"]:
                    s["source"]["updated_at"] = s["generated_at"]
                    s["source"]["age_s"] = 0
                    for lim in s["limits"]:
                        if lim["level"] != "limited" and lim["id"] == "five_hour":
                            lim["used_pct"] = round(min(99.4, lim["used_pct"] + 0.2), 1)
                    for ss in s["sessions"][:1]:
                        ss["context"]["pct"] = round(min(99.0, ss["context"]["pct"] + 0.3), 1)
            self.mutate(bump)

    # ---------- lazy topics ----------
    def history(self, window: str):
        st = self.get_state()
        lim = next((l for l in st["limits"] if l["id"] == window), None)
        if lim is None:
            return []
        src = load("history-five_hour.json" if window == "five_hour" else "history-seven_day.json")
        s0, s1 = src[0][0], src[-1][0]
        top = max(v for _, v in src) or 1
        start = lim["resets_at"] - lim["window_s"]
        end = min(st["generated_at"], lim["resets_at"])
        used = lim["used_pct"]
        if (lim.get("pace") or {}).get("confidence") == "gathering":
            n = 9
            return [[int(start + (end - start) * i / (n - 1)), round(used * i / (n - 1), 1)] for i in range(n)]
        out = []
        for t, v in src:
            f = (t - s0) / (s1 - s0)
            if used >= 100:
                tt = start + f * 0.86 * (end - start)
                vv = v / top * 100
            else:
                tt = start + f * (end - start)
                vv = v / top * used
            out.append([int(tt), round(vv, 1)])
        if used >= 100:
            out.append([int(end), 100.0])
        return out

    def alltime(self, rng: str):
        if self.flags["alltime_notready"]:
            return {"ready": False, "range": rng}
        d = load("alltime.json")
        d.pop("_doc", None)
        d["range"] = rng
        if rng == "7d":
            cut = FIXTURE_NOW - 7 * 86400
            idx = [i for i, t in enumerate(d["usage"]["t"]) if t >= cut]
            for k in ("t", "five_hour", "seven_day"):
                d["usage"][k] = [d["usage"][k][i] for i in idx]
            for k in ("tokens", "messages", "sessions"):
                d[k] = int(d[k] * 0.27)
            d["active_days"] = min(d["active_days"], 6)
            d["series"] = d["series"][-7:]
            for m in d["models"] + d["projects"]:
                m["tokens"] = int(m["tokens"] * 0.27)
            d["compare"] = {"x": 46, "name": "the Harry Potter series"}
        elif rng == "all":
            for k in ("tokens", "messages", "sessions"):
                d[k] = int(d[k] * 4.4)
            d["active_days"] = 97
            for m in d["models"] + d["projects"]:
                m["tokens"] = int(m["tokens"] * 4.4)
            d["compare"] = {"x": 750, "name": "the Harry Potter series"}
        d = self.shift(d)
        if self.offset:
            dd = round(self.offset / 86400)
            for x in d["heatmap"]["days"]:
                x["d"] = (datetime.fromisoformat(x["d"]) + timedelta(days=dd)).date().isoformat()
            d["usage"]["t"] = [t + self.offset for t in d["usage"]["t"]]
        return d

    def current_month(self):
        return datetime.fromtimestamp(self.now(), ATHENS).strftime("%Y-%m")

    def ledger(self, month: str):
        if self.flags["ledger_error"]:
            return 502, {"ok": False, "error": "unreachable"}
        cur = self.current_month()
        if not re.fullmatch(r"\d{4}-\d{2}", month or "") or month > cur:
            return 400, {"ok": False, "error": "Pick a month up to " + cur + "."}
        d = load("ledger.json")
        d.pop("_doc", None)
        d["month"], d["current_month"] = month, cur
        d["updated_at"] = self.now() - 60
        if month != cur:
            y, m = map(int, month.split("-"))
            cy, cm = map(int, cur.split("-"))
            back = (cy - y) * 12 + (cm - m)
            if back > 3:
                d["rows"], d["totals"] = [], []
            else:
                for r in d["rows"]:
                    r["spend"] = round(r["spend"] * (0.6 + 0.3 * back), 2)
                    r["meter_end"] = r["spend"]
                    r["state"] = "closed"
                    r["days"] = 28 + back % 3
                d["totals"] = [{"currency": "EUR", "spend": round(sum(r["spend"] for r in d["rows"]), 2)}]
        return 200, d

    # ---------- actions ----------
    def action(self, name: str, body: dict):
        print(f"[action] {name} {json.dumps(body)}", flush=True)
        if self.flags["fail_actions"] and name not in ("overlay_resize", "flyout_close"):
            return 500, {"ok": False, "error": "The mock was told to fail every action."}
        now = self.now()
        if name == "mute":
            def f(s):
                lim = next((l for l in s["limits"] if l["binding"]), s["limits"][0] if s["limits"] else None)
                if body.get("for") == "refill" and lim:
                    s["mute"] = {"until": lim["resets_at"], "reason": "refill"}
                elif body.get("for") == "hour":
                    s["mute"] = {"until": now + 3600, "reason": "hour"}
                else:
                    s["mute"] = {"until": None, "reason": None}
            self.mutate(f)
        elif name == "overlay":
            def f(s):
                if "show" in body:
                    s["app"]["overlay"]["shown"] = bool(body["show"])
                if "pin" in body:
                    s["app"]["overlay"]["pinned"] = bool(body["pin"])
            self.mutate(f)
        elif name == "team_login_start":
            if str(body.get("email", "")).endswith("@fail.dev"):
                return 400, {"ok": False, "error": "That email domain isn't set up for a team pool."}
        elif name == "team_login_verify":
            if body.get("code") == "000000":
                return 400, {"ok": False, "error": "That code is wrong or has expired. Send a new one."}
            if not body.get("username"):
                return 200, {"ok": True, "needs_username": True}
            team = self.shift(load("state-will_hit.json"))["team"]
            self.mutate(lambda s: s.__setitem__("team", team))
            return 200, {"ok": True, "signed_in": True}
        elif name == "team_logout":
            self.mutate(lambda s: s.__setitem__("team", None))
        elif name == "team_claim_device":
            self.mutate(lambda s: s["team"] and s["team"].__setitem__("device_ok", True))
        elif name == "team_connector_token":
            return 200, {"ok": True, "token": "ctu_conn_7Hq2mV9xR4pL1sK8wZ3nB6tY0cF5dJ", "expires_at": now + 30 * 86400}
        elif name == "remote_pair":
            return 200, {"ok": True, "pair": {"code": "493 118", "url": "ctu://pair?k=demo", "qr_svg": fake_qr(), "expires_at": now + 600}}
        elif name == "remote_unpair":
            self.mutate(lambda s: s["app"]["remote"].update(paired=False))
        elif name == "check_update":
            return 200, {"ok": True, "update": None, "message": "You're on the latest version."}
        elif name == "overlay_resize":
            print(f"[overlay] resize to {body.get('w')}x{body.get('h')}", flush=True)
        return 200, {"ok": True}

    def patch_settings(self, body: dict):
        if self.flags["settings_error"]:
            return 500, {"ok": False, "error": "Settings file is read-only (mock)."}
        with self.lock:
            for g, v in body.items():
                if g == "status_components" and isinstance(v, list):
                    self.settings[g] = [str(x) for x in v]
                elif isinstance(v, dict) and isinstance(self.settings.get(g), dict):
                    self.settings[g].update(v)
                else:
                    return 400, {"ok": False, "error": f"Unknown setting group: {g}"}
            ov = self.settings["overlay"]
            ov["opacity"] = max(30, min(100, int(ov.get("opacity", 90))))
            return 200, copy.deepcopy(self.settings)


def fake_qr() -> str:
    import random
    rnd = random.Random(42)
    n = 25
    cells = []
    for y in range(n):
        for x in range(n):
            finder = any(x0 <= x < x0 + 7 and y0 <= y < y0 + 7 for x0, y0 in ((0, 0), (n - 7, 0), (0, n - 7)))
            if finder:
                fx, fy = (x % (n - 7)) if x >= n - 7 else x, (y % (n - 7)) if y >= n - 7 else y
                on = fx in (0, 6) or fy in (0, 6) or (2 <= fx <= 4 and 2 <= fy <= 4)
            else:
                on = rnd.random() < 0.48
            if on:
                cells.append(f'<rect x="{x}" y="{y}" width="1" height="1"/>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges"><g fill="#000">{"".join(cells)}</g></svg>'


MOCK: Mock


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/api/v2/events" not in (self.path or ""):
            sys.stderr.write("%s\n" % (fmt % args))

    def send_json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path: Path):
        if not path.is_file():
            return self.send_json(404, {"ok": False, "error": "not found"})
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if p in ("/", "/dashboard"):
            return self.send_file(UI / "dashboard.html")
        if p == "/overlay":
            return self.send_file(UI / "overlay.html")
        if p == "/flyout":
            return self.send_file(UI / "flyout.html")
        m = re.fullmatch(r"/assets/([A-Za-z0-9._-]+)", p)
        if m:
            return self.send_file(UI / "assets" / m.group(1))
        if p == "/api/v2/state":
            if MOCK.flags["state_error"]:
                return self.send_json(503, {"ok": False, "error": "starting"})
            return self.send_json(200, MOCK.get_state())
        if p == "/api/v2/events":
            return self.sse()
        if p == "/api/v2/history":
            return self.send_json(200, MOCK.history(q.get("window", "five_hour")))
        if p == "/api/v2/alltime":
            if MOCK.flags["alltime_error"]:
                return self.send_json(500, {"ok": False, "error": "The all-time scan failed. Details are in the log."})
            return self.send_json(200, MOCK.alltime(q.get("range", "30d")))
        if p == "/api/v2/team/ledger":
            code, obj = MOCK.ledger(q.get("month", ""))
            return self.send_json(code, obj)
        if p == "/api/v2/settings":
            with MOCK.lock:
                return self.send_json(200, copy.deepcopy(MOCK.settings))
        if p == "/__variant":
            try:
                MOCK.set_variant(q.get("name", "will_hit"))
            except ValueError:
                return self.send_json(400, {"ok": False, "error": "variants: " + ", ".join(VARIANTS)})
            return self.send_json(200, {"ok": True, "variant": MOCK.variant})
        if p == "/__drop":
            for q in list(MOCK.clients):
                q.put(("__close__", ""))
            return self.send_json(200, {"ok": True})
        if p == "/__mode":
            for k, v in q.items():
                if k in MOCK.flags:
                    MOCK.flags[k] = v not in ("0", "false", "")
            return self.send_json(200, {"ok": True, "flags": MOCK.flags})
        return self.send_json(404, {"ok": False, "error": "not found"})

    def do_PATCH(self):
        if urlparse(self.path).path == "/api/v2/settings":
            code, obj = MOCK.patch_settings(self.body())
            return self.send_json(code, obj)
        return self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        m = re.fullmatch(r"/api/v2/actions/([a-z_]+)", p)
        if m:
            code, obj = MOCK.action(m.group(1), self.body())
            return self.send_json(code, obj)
        if p == "/api/v2/hooks/claude":
            return self.send_json(200, {"ok": True})
        return self.send_json(404, {"ok": False, "error": "not found"})

    def sse(self):
        if MOCK.flags["no_sse"]:
            return self.send_json(404, {"ok": False, "error": "no sse"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q: queue.Queue = queue.Queue()
        MOCK.clients.append(q)
        try:
            self.wfile.write(b"retry: 3000\n")
            self.wfile.write(f"event: state\ndata: {json.dumps(MOCK.get_state())}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    ev, data = q.get(timeout=15)
                    if ev == "__close__":
                        break
                    self.wfile.write(f"event: {ev}\ndata: {data}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.close_connection = True
            if q in MOCK.clients:
                MOCK.clients.remove(q)


def main():
    global MOCK
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--variant", default="will_hit", choices=VARIANTS)
    ap.add_argument("--tick", type=float, default=0, help="bump rev every N seconds with small changes")
    ap.add_argument("--frozen", action="store_true", help="keep fixture times (no shift to the real clock)")
    ap.add_argument("--fail-actions", action="store_true")
    ap.add_argument("--ledger-error", action="store_true")
    ap.add_argument("--no-sse", action="store_true", help="404 the event stream to exercise polling")
    a = ap.parse_args()
    MOCK = Mock(a.variant, a.frozen, a.tick)
    MOCK.flags["fail_actions"] = a.fail_actions
    MOCK.flags["ledger_error"] = a.ledger_error
    MOCK.flags["no_sse"] = a.no_sse
    if a.tick > 0:
        threading.Thread(target=MOCK.ticker, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"mock UI server on http://{a.host}:{a.port}/  variant={a.variant} frozen={a.frozen}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
