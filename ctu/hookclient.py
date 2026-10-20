"""`--session-hook` and `--action`: tiny stdlib-only clients for the running tray's local API.

They run on every Claude Code turn (hooks) or a toast button click, so they import nothing heavy
(no urllib / http / argparse) and never create the data dir. Always exit 0: never block Claude."""
from __future__ import annotations

import json
import os
import socket
import sys

from ctu import paths

# Fields forwarded to the tray. Prompt text and assistant messages are never sent.
_FIELDS = ("hook_event_name", "session_id", "cwd", "notification_type", "stop_hook_active", "reason")
_ACTIONS = {"open": "open_dashboard", "mute": "mute"}


def port():
    try:
        p = int(paths.PORT_PATH.read_text(encoding="utf-8").strip())
        return p if 0 < p < 65536 else None
    except Exception:
        return None


def post(path: str, body: dict, timeout: float = 2.0):
    """POST JSON to 127.0.0.1:<port><path>. Returns the HTTP status, or None."""
    p = port()
    if not p:
        return None
    data = json.dumps(body).encode("utf-8")
    head = (f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{p}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode("ascii")
    try:
        with socket.create_connection(("127.0.0.1", p), timeout=timeout) as s:
            s.sendall(head + data)
            line = s.recv(64).split(b"\r\n", 1)[0].split()
            return int(line[1]) if len(line) > 1 else None
    except Exception:
        return None


def _stdin_text() -> str:
    try:
        if sys.stdin is not None:
            return sys.stdin.read()
    except Exception:
        pass
    chunks = []
    try:                                    # windowed builds can lack sys.stdin; fd 0 is the pipe
        while True:
            b = os.read(0, 65536)
            if not b:
                break
            chunks.append(b)
    except Exception:
        pass
    return b"".join(chunks).decode("utf-8", errors="replace")


def payload(d: dict) -> dict:
    return {k: d.get(k) for k in _FIELDS if k in d}


def run_session_hook() -> int:
    try:
        raw = _stdin_text()
        d = json.loads(raw) if raw.strip() else {}
    except Exception:
        d = {}
    if isinstance(d, dict) and d.get("hook_event_name"):
        post("/api/v2/hooks/claude", payload(d))
    return 0


def parse_action_uri(uri: str):
    """`claude-usage-tracker:open/now` -> ("open_dashboard", {"view": "now"});
    `claude-usage-tracker:mute/hour` -> ("mute", {"for": "hour"}). Anything else -> None."""
    s = str(uri or "").strip().strip('"')
    if ":" in s:
        s = s.split(":", 1)[1]
    s = s.strip("/")
    verb, _, arg = s.partition("/")
    name = _ACTIONS.get(verb)
    arg = "".join(c for c in arg if c.isalnum() or c in "_-")[:32]
    if name == "open_dashboard":
        return name, ({"view": arg} if arg else {})
    if name == "mute" and arg in ("hour", "refill", "off"):
        return name, {"for": arg}
    return None


def run_action(uri: str) -> int:
    parsed = parse_action_uri(uri)
    if parsed:
        post(f"/api/v2/actions/{parsed[0]}", parsed[1])
    return 0
