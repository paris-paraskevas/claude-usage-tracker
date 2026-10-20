"""`--selftest`: build a full state from fixtures in a temp dir — no network, no real user data —
and exercise the local API, SSE, the v1 adapter and the tray renderer. Exit 0 when all pass."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import traceback
import urllib.request
from pathlib import Path

from ctu import paths

_REBIND = ("PROJECTS_DIR", "CLAUDE_JSON", "CREDS_PATH", "CLAUDE_SETTINGS")


def _fixtures(root: Path, now: float) -> None:
    (root / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "emailAddress": "dev@example.com", "displayName": "Dev", "organizationName": "Example"}}), encoding="utf-8")
    (root / "creds.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "fixture", "expiresAt": int((now + 3600) * 1000), "subscriptionType": "max"}}), encoding="utf-8")
    proj = root / "projects" / "C--Dev-demo"
    proj.mkdir(parents=True)
    lines = []
    for i in range(6):
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 1200 + i * 200))
        lines.append({"type": "user", "timestamp": ts, "cwd": "C:\\Dev\\demo", "sessionId": "s-demo",
                      "message": {"role": "user", "content": "fixture prompt"}})
        lines.append({"type": "assistant", "timestamp": ts, "cwd": "C:\\Dev\\demo", "sessionId": "s-demo",
                      "message": {"role": "assistant", "model": "claude-opus-5-5", "stop_reason": "end_turn",
                                  "content": [{"type": "text", "text": "ok"}],
                                  "usage": {"input_tokens": 1000, "output_tokens": 400,
                                            "cache_creation_input_tokens": 2000,
                                            "cache_read_input_tokens": 60000 + i * 10000}}})
    (proj / "s-demo.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def run() -> int:
    from ctu import statusline
    tmp = Path(tempfile.mkdtemp(prefix="ctu-selftest-"))
    saved = {k: getattr(paths, k) for k in _REBIND}
    saved_data = paths.DATA_DIR
    checks, engine, srv = [], None, None

    def check(name, cond, info=""):
        checks.append((name, bool(cond), info))

    try:
        now = time.time()
        _fixtures(tmp, now)
        paths.use_data_dir(tmp / "data")
        paths.PROJECTS_DIR, paths.CLAUDE_JSON = tmp / "projects", tmp / ".claude.json"
        paths.CREDS_PATH, paths.CLAUDE_SETTINGS = tmp / "creds.json", tmp / "settings.json"
        from ctu import config, remote, server
        from ctu.engine import Engine
        remote._remote_id_cache = None
        resets = int(now + 5400)
        engine = Engine(cfg=json.loads(json.dumps(config.DEFAULTS)), network=False)
        # A climbing 5-hour window, 35% -> 65% over the last 30 minutes, via the statusline path.
        paths.ensure_data_dir()
        snap = None
        for k in range(0, 31, 2):
            payload = {"session_id": "s-demo", "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
                       "context_window": {"context_window_size": 200000, "used_percentage": 40},
                       "rate_limits": {"five_hour": {"used_percentage": 35 + k, "resets_at": resets},
                                       "seven_day": {"used_percentage": 41, "resets_at": int(now + 4 * 86400)}}}
            snap = statusline.merge(snap, payload, now - 1800 + k * 60)
            paths.STATUSLINE_PATH.write_text(json.dumps(snap), encoding="utf-8")
            engine.sl_sig = None
            engine.job_statusline()
        engine.tailer.poll(time.time())
        engine.on_hook({"hook_event_name": "Stop", "session_id": "s-demo", "cwd": "C:\\Dev\\demo"})
        with engine.lock:
            engine.recompute()
        st = engine.snapshot()
        fh = next((l for l in st["limits"] if l["id"] == "five_hour"), None)
        check("state schema 2", st.get("schema") == 2)
        check("five_hour limit", fh is not None and fh["used_pct"] == 65.0, fh and fh["used_pct"])
        check("pace fitted", fh and fh["pace"]["rate_pct_h"] and fh["pace"]["confidence"] in ("low", "ok"),
              fh and fh["pace"])
        check("decision text", st["decision"]["title"] and st["decision"]["binding"] == "five_hour",
              st["decision"]["title"])
        check("sessions", st["sessions"] and st["sessions"][0]["state"] == "waiting", st["sessions"][:1])
        check("account + plan", st["account"]["email"] == "dev@example.com" and st["account"]["plan"] == "max")
        s1 = engine.v1_snapshot()
        check("v1 adapter", s1["windows"] and "pace" in s1["windows"][0] and s1["decision"] == st["decision"])
        try:
            from ctu import tray
            imgs = {tray.render_icon(n, "64", lv, False, "dark").tobytes() for n in (16, 20, 24, 32)
                    for lv in ("ok", "watch", "will_hit")}
            check("tray renderer", len(imgs) == 12)
        except ImportError:
            check("tray renderer (Pillow missing, skipped)", True)
        srv = server.make_server(engine, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v2/state", timeout=5) as r:
            body = json.loads(r.read())
        check("GET /api/v2/state", body.get("rev") == st["rev"])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v2/events", timeout=5) as r:
            first = b""
            while b"\n\n" not in first.split(b"retry: 2000\n\n", 1)[-1]:
                first += r.read1(65536)
        check("SSE first event", b"event: state" in first)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/usage", timeout=5) as r:
            check("GET /api/usage (v1)", "windows" in json.loads(r.read()))
        print(f"decision: {st['decision']['title']} / {st['decision']['detail']}")
    except Exception:
        check("selftest crashed", False, traceback.format_exc())
    finally:
        try:
            if srv:
                srv.shutdown()
            if engine:
                engine.stopping = True
        except Exception:
            pass
        for k, v in saved.items():
            setattr(paths, k, v)
        paths.use_data_dir(saved_data)
        shutil.rmtree(tmp, ignore_errors=True)
    width = max(len(n) for n, _, _ in checks)
    for name, ok, info in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {'' if ok else info}")
    ok = all(c[1] for c in checks)
    print("SELFTEST OK" if ok else "SELFTEST FAILED")
    return 0 if ok else 1
