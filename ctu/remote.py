"""Phone relay (opt-in, end-to-end encrypted; docs/REMOTE.md). The desktop encrypts the v1
snapshot with a key the relay never sees (libsodium secretbox) and PUTs the ciphertext; the
phone decrypts it. Pairing ships the key in a QR code on the loopback-only dashboard. Everything
no-ops if PyNaCl is missing or the feature is off."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from ctu import __version__, io, paths
from ctu.logtail import parse_iso, project_name

HEARTBEAT_S = 50        # + the 10 s job cadence: a payload every <= 60 s (phone: "offline" after 120 s)
_remote_id_cache = None


def remote_available() -> bool:
    return importlib.util.find_spec("nacl") is not None


def _b64u(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def load_remote_identity(create: bool = False):
    """{account_id, read_token, e2ee_key} from remote.json; minted when create=True."""
    global _remote_id_cache
    if _remote_id_cache is not None:
        return _remote_id_cache
    data = io.load_json(paths.REMOTE_PATH, None)
    if isinstance(data, dict) and data.get("account_id") and data.get("read_token") and data.get("e2ee_key"):
        _remote_id_cache = data
        return data
    if not create or not remote_available():
        return None
    import base64
    import os
    ident = {"v": 1, "account_id": _b64u(os.urandom(16)), "read_token": _b64u(os.urandom(32)),
             "e2ee_key": base64.b64encode(os.urandom(32)).decode()}
    io.save_json(paths.REMOTE_PATH, ident)
    _remote_id_cache = ident
    io.log("remote: generated a new pairing identity")
    return ident


def rotate_remote_identity():
    """Fresh read_token + key: old relay data becomes unreadable and the phone must re-pair."""
    unpair_remote()
    return load_remote_identity(create=True)


def unpair_remote() -> None:
    global _remote_id_cache
    _remote_id_cache = None
    try:
        paths.REMOTE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def remote_encrypt(obj):
    """The wire blob {v, nonce, ct, ts} for `obj`, or None."""
    import base64
    from nacl.secret import SecretBox
    from nacl.utils import random as nrandom
    ident = load_remote_identity(create=True)
    if not ident:
        return None
    box = SecretBox(base64.b64decode(ident["e2ee_key"]))
    enc = box.encrypt(json.dumps(obj).encode("utf-8"), nrandom(SecretBox.NONCE_SIZE))
    return {"v": 1, "nonce": base64.b64encode(enc.nonce).decode(),
            "ct": base64.b64encode(enc.ciphertext).decode(), "ts": int(time.time())}


def remote_pair_uri(cfg: dict, create: bool = True):
    """`cutpair1:<base64url(json{u,a,t,k})>`. create=False never mints (read paths)."""
    import base64
    ident = load_remote_identity(create=create)
    url = (cfg.get("remote_relay_url") or "").rstrip("/")
    if not ident or not url:
        return None
    payload = {"u": url, "a": ident["account_id"], "t": ident["read_token"], "k": ident["e2ee_key"]}
    return "cutpair1:" + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def remote_pair_qr_png(cfg: dict, create: bool = False):
    uri = remote_pair_uri(cfg, create=create)
    if not uri:
        return None
    try:
        import io as _io
        import qrcode
        buf = _io.BytesIO()
        qrcode.make(uri).save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        io.log(f"remote: pair QR failed: {exc}")
        return None


def remote_pair_qr_svg(cfg: dict, create: bool = False):
    """The pairing QR as an SVG string (the dashboard inlines it), or None."""
    uri = remote_pair_uri(cfg, create=create)
    if not uri:
        return None
    try:
        import qrcode
        import qrcode.image.svg
        return qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage).to_string(encoding="unicode")
    except Exception as exc:
        io.log(f"remote: pair QR failed: {exc}")
        return None


def _relay_call(method: str, cfg: dict, path: str, body=None, timeout: int = 8):
    url = (cfg.get("remote_relay_url") or "").rstrip("/")
    ident = load_remote_identity(create=False)
    if not url or not ident:
        return None
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + ident["read_token"])
    req.add_header("User-Agent", f"claude-usage-tracker/{__version__}")  # Cloudflare blocks bare urllib (1010)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None                      # RemoteSync logs the ok <-> failing transitions


def remote_sync_snapshot(snap: dict, cfg: dict) -> bool:
    ident = load_remote_identity(create=False)
    if not ident:
        return False
    blob = remote_encrypt(snap)
    return bool(blob) and _relay_call("PUT", cfg, f"/v1/acct/{ident['account_id']}/snapshot", blob) == 204


def remote_push(cfg: dict, title: str, body: str, tag: str = "") -> None:
    """Encrypted push payload; the relay fans it out via FCM."""
    ident = load_remote_identity(create=False)
    if not ident:
        return
    blob = remote_encrypt({"title": title, "body": body, "tag": tag})
    if blob:
        _relay_call("POST", cfg, f"/v1/acct/{ident['account_id']}/push", blob)


# ---- conversation mirror (opt-in `remote_transcript`; the only reader of message text) ----

def _extract_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if isinstance(b, str):
            parts.append(b)
        elif isinstance(b, dict):
            if b.get("type") == "text" and isinstance(b.get("text"), str):
                parts.append(b["text"])
            elif b.get("type") == "tool_use":
                parts.append(f"[ran {b.get('name', 'tool')}]")
    return "\n".join(p for p in parts if p).strip()


def _read_one_transcript(f: Path, max_msgs: int, max_chars: int, tail_bytes: int = 400_000):
    try:
        size = f.stat().st_size
    except Exception:
        return None
    cwd, msgs = None, []
    try:
        with open(f, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(8):                       # cwd is on an early line
                ln = fh.readline()
                if not ln:
                    break
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                if isinstance(o.get("cwd"), str):
                    cwd = o["cwd"]
                    break
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()
            else:
                fh.seek(0)
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                if cwd is None and isinstance(o.get("cwd"), str):
                    cwd = o["cwd"]
                msg = o.get("message")
                if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
                    continue
                text = _extract_text(msg.get("content"))
                if text:
                    msgs.append({"role": msg["role"], "text": text[:max_chars], "ts": parse_iso(o.get("timestamp"))})
    except Exception:
        return None
    if not msgs:
        return None
    return {"name": project_name(cwd, f.parent.name), "cwd": cwd, "session_id": f.stem, "messages": msgs[-max_msgs:]}


def read_transcripts(limit: int = 6, max_msgs: int = 30, max_chars: int = 2500) -> list:
    """Recent conversations, newest first, one per project (the phone picks one)."""
    try:
        files = sorted(paths.PROJECTS_DIR.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return []
    now, out, seen = time.time(), [], set()
    for f in files:
        if len(out) >= max(1, limit):
            break
        t = _read_one_transcript(f, max_msgs, max_chars)
        if not t:
            continue
        key = t.get("cwd") or str(f)
        if key in seen:
            continue
        seen.add(key)
        try:
            t["active"] = (now - f.stat().st_mtime) <= 600
        except Exception:
            t["active"] = False
        out.append(t)
    return out


def significant(snap: dict) -> str:
    """Hash of the parts of a v1 snapshot worth an immediate push (not time-drifting pace numbers)."""
    keep = {
        "ok": snap.get("ok"), "error": snap.get("error"),
        "windows": [(w.get("key"), w.get("pct"), w.get("resets_at"), w.get("level")) for w in snap.get("windows") or []],
        "decision": {k: (snap.get("decision") or {}).get(k) for k in ("level", "binding", "title", "resume_at")},
        "sessions": [(s.get("name"), s.get("active"), s.get("context_pct")) for s in snap.get("sessions") or []],
        "extra": snap.get("extra"), "status": (snap.get("status") or {}).get("indicator"),
        "team": snap.get("team"), "team_overview": snap.get("team_overview"), "ui": snap.get("ui"),
    }
    return hashlib.sha256(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()


class RemoteSync:
    """Relay push on content change (significant()) plus a heartbeat, never closer than min_gap."""

    def __init__(self):
        self._last_hash = None
        self._last_push = 0.0
        self.last_ok: bool | None = None
        self.last_sync: int | None = None

    @staticmethod
    def enabled(cfg: dict) -> bool:
        return bool(cfg.get("remote_enabled") and cfg.get("remote_relay_url") and remote_available())

    def due(self, snap: dict, now: float, min_gap: float) -> bool:
        if now - self._last_push < min_gap:
            return False
        return significant(snap) != self._last_hash or now - self._last_push >= HEARTBEAT_S

    def force(self) -> None:
        self._last_hash = None
        self._last_push = 0.0

    def sync(self, snap: dict, cfg: dict, now: float | None = None) -> None:
        now = time.time() if now is None else now
        h = significant(snap)
        self._last_push = now
        prev = self.last_ok
        try:
            if cfg.get("remote_transcript"):
                ts = read_transcripts()
                if ts:   # a copy: message text never enters local state
                    snap = {**snap, "transcripts": ts, "transcript": ts[0]}
            self.last_ok = remote_sync_snapshot(snap, cfg)
            if self.last_ok:
                self._last_hash, self.last_sync = h, int(now)
        except Exception:
            self.last_ok = False
            if prev is not False:
                io.log("remote: sync error:\n" + traceback.format_exc())
        if self.last_ok != prev:
            io.log(f"remote: relay sync {'ok' if self.last_ok else 'failing'}")

    def push(self, cfg: dict, title: str, msg: str, tag: str = "") -> None:
        if self.enabled(cfg) and load_remote_identity(create=False):
            threading.Thread(target=remote_push, args=(cfg, title, msg, tag or title[:40]), daemon=True).start()
