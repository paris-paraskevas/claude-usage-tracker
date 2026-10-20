"""Claude Code sessions: per-session tokens (5 h), context fill, model and state, from the log
tailer (token counts, timestamps, record kinds — never message text) plus the hook events.

state: needs_approval (Notification[permission_prompt]) > waiting (Stop hook, or inferred from
the log: the last turn ended / was interrupted) > working (activity in the last 10 min) > idle."""
from __future__ import annotations

import threading
from pathlib import Path

from ctu.logtail import Consumer, parse_iso, project_name, usage_of

ACTIVE_S = 600
WINDOW_S = 5 * 3600
WAITING_TTL = 2 * 3600       # a turn left unanswered this long reads as idle (closed terminal, no SessionEnd)


def _kind(o: dict):
    """What a record says about the turn: prompt / tool / end / interrupt / None."""
    if o.get("isSidechain") or o.get("isMeta"):
        return None
    t = o.get("type")
    msg = o.get("message") if isinstance(o.get("message"), dict) else {}
    content = msg.get("content")
    if t == "user":
        if isinstance(content, list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                return "tool"
            text = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        else:
            text = content if isinstance(content, str) else ""
        if "[Request interrupted by user" in text:
            return "interrupt"
        return "prompt" if text.strip() else None
    if t == "assistant":
        blocks = content if isinstance(content, list) else []
        if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks):
            return "tool"
        if msg.get("stop_reason") in ("end_turn", "stop_sequence", "max_tokens") or (
                msg.get("stop_reason") is None and any(isinstance(b, dict) and b.get("type") == "text" for b in blocks)):
            return "end"
        return "tool"
    return None


class SessionTracker(Consumer):
    name = "sessions"

    def __init__(self, window_s: int = WINDOW_S):
        self.window_s = window_s
        self.files: dict = {}      # path -> per-file state (see begin)
        self.hooks: dict = {}      # session_id -> {"state", "at", "cwd"}
        self.lock = threading.Lock()

    # ---- log consumer ----
    def want(self, key, st, now):
        ent = self.files.get(key)
        if ent is not None:
            if st.st_size < ent["offset"]:                  # rotated / truncated
                with self.lock:
                    self.files.pop(key, None)
                return 0
            return ent["offset"]
        if st.st_mtime < now - self.window_s:
            return None                                     # untouched within the window
        if st.st_size > 32 * 1024 * 1024:
            return (st.st_size - 32 * 1024 * 1024, True)    # backfill only the recent tail
        return 0

    def begin(self, key, path):
        ent = self.files.get(key)
        return {"cwd": ent["cwd"] if ent else None, "model": None, "events": [], "ctx": None,
                "max_ctx": 0, "last_ts": None, "kind": None, "kind_ts": None, "turn_ts": None}

    def feed(self, d, o, key, path):
        if d["cwd"] is None and isinstance(o.get("cwd"), str):
            d["cwd"] = o["cwd"]
        ts = parse_iso(o.get("timestamp"))
        if ts is None:
            return
        k = _kind(o)
        if k:
            d["kind"], d["kind_ts"] = k, ts
            if k == "prompt":
                d["turn_ts"] = ts
        if o.get("type") in ("user", "assistant") or isinstance(o.get("message"), dict):
            d["last_ts"] = ts if d["last_ts"] is None else max(d["last_ts"], ts)
        msg = o.get("message")
        if isinstance(msg, dict) and msg.get("model") and not str(msg["model"]).startswith("<"):
            d["model"] = msg["model"]
        u = usage_of(o)
        if not u:
            return
        inp = int(u.get("input_tokens", 0) or 0)
        out = int(u.get("output_tokens", 0) or 0)
        cc = int(u.get("cache_creation_input_tokens", 0) or 0)
        cr = int(u.get("cache_read_input_tokens", 0) or 0)
        tok = inp + out + cc                      # 5 h burn excludes cheap cache reads
        if tok > 0:
            d["events"].append((ts, tok))
        ctx = inp + cc + cr                       # context fill is the whole prompt
        if ctx > 0 and not o.get("isSidechain"):
            d["ctx"] = (ctx, ts)
            d["max_ctx"] = max(d["max_ctx"], ctx)

    def commit(self, key, d, offset):
        with self.lock:
            ent = self.files.get(key) or {"offset": 0, "sid": Path(key).stem, "dir": Path(key).parent.name,
                                          "cwd": None, "model": None, "events": [], "ctx": 0, "ctx_ts": 0,
                                          "max_ctx": 0, "last_ts": 0, "kind": None, "kind_ts": 0, "turn_ts": 0}
            ent["offset"] = offset
            ent["cwd"] = d["cwd"] or ent["cwd"]
            ent["model"] = d["model"] or ent["model"]
            ent["events"].extend(d["events"])
            if d["ctx"]:
                ent["ctx"], ent["ctx_ts"] = d["ctx"]
            ent["max_ctx"] = max(ent["max_ctx"], d["max_ctx"])
            if d["last_ts"]:
                ent["last_ts"] = max(ent["last_ts"], d["last_ts"])
            if d["kind"]:
                ent["kind"], ent["kind_ts"] = d["kind"], d["kind_ts"]
            if d["turn_ts"]:
                ent["turn_ts"] = d["turn_ts"]
            self.files[key] = ent

    def forget(self, live):
        with self.lock:
            for k in [k for k in self.files if k not in live]:
                del self.files[k]

    # ---- hooks ----
    def on_hook(self, p: dict, now: float) -> bool:
        """Apply a hook event. True if a session's state changed."""
        sid = p.get("session_id")
        if not isinstance(sid, str) or not sid:
            return False
        ev = p.get("hook_event_name") or p.get("event")
        with self.lock:
            prev = self.hooks.get(sid)
            if ev == "Stop":
                if p.get("stop_hook_active"):
                    return False                    # a stop-hook continuation, not a real stop
                self.hooks[sid] = {"state": "waiting", "at": now, "cwd": p.get("cwd")}
            elif ev == "Notification" and p.get("notification_type") == "permission_prompt":
                self.hooks[sid] = {"state": "needs_approval", "at": now, "cwd": p.get("cwd")}
            elif ev in ("UserPromptSubmit", "SessionEnd"):
                self.hooks.pop(sid, None)
            else:
                return False
            return self.hooks.get(sid) != prev

    # ---- view ----
    def build(self, now: float, statusline_sessions: dict | None = None, top_n: int = 12) -> list:
        """v2 `sessions[]`: waiting / needs-approval first, then working, then idle."""
        sl = statusline_sessions or {}
        cutoff = now - self.window_s
        rows = []
        with self.lock:
            hooks = dict(self.hooks)
            for key, ent in self.files.items():
                ent["events"] = [(t, k) for (t, k) in ent["events"] if t >= cutoff]
                hook = hooks.pop(ent["sid"], None)
                rows.append(self._row(ent, hook, sl.get(ent["sid"]), now))
            for sid, hook in hooks.items():          # hook-only sessions (log not read yet)
                if hook["at"] >= cutoff:
                    rows.append(self._row({"sid": sid, "dir": sid, "cwd": hook.get("cwd"), "events": [],
                                           "ctx": 0, "max_ctx": 0, "last_ts": 0, "kind": None,
                                           "kind_ts": 0, "turn_ts": 0, "model": None}, hook, sl.get(sid), now))
        rows = [r for r in rows if r.pop("_last") >= cutoff]
        rank = {"needs_approval": 0, "waiting": 1, "working": 2, "idle": 3}
        rows.sort(key=lambda r: (rank[r["state"]], -r["since"]))
        return rows[:top_n]

    @staticmethod
    def _row(ent, hook, sl, now):
        last = max(ent["last_ts"] or 0, hook["at"] if hook else 0)
        kind, kind_ts = ent["kind"], ent["kind_ts"] or 0
        state, since = None, last
        fresh = hook and now - hook["at"] < WAITING_TTL
        if fresh and hook["state"] == "needs_approval" and not (kind in ("tool", "prompt", "end") and kind_ts > hook["at"] + 1):
            state, since = "needs_approval", hook["at"]
        elif fresh and hook["state"] == "waiting" and not (kind in ("prompt", "tool") and kind_ts > hook["at"] + 1):
            state, since = "waiting", hook["at"]
        elif kind in ("end", "interrupt") and now - kind_ts < WAITING_TTL:
            state, since = "waiting", kind_ts
        elif now - last <= ACTIVE_S:
            state, since = "working", (ent["turn_ts"] or last)
        else:
            state = "idle"
        model = ent["model"] or (sl or {}).get("model")
        window = (sl or {}).get("window")
        if not isinstance(window, (int, float)) or window <= 0:
            window = 1_000_000 if ent["max_ctx"] > 200_000 else 200_000   # ever past 200k -> 1M window
        tokens = ent["ctx"] or 0
        return {
            "id": ent["sid"],
            "project": project_name(ent["cwd"] or (sl or {}).get("cwd"), ent["dir"]),
            "cwd": ent["cwd"] or (sl or {}).get("cwd"),
            "state": state,
            "since": int(since or now),
            "active": (now - last) <= ACTIVE_S,
            "model": model,
            "context": {"pct": round(min(100.0, tokens / window * 100.0), 1) if tokens else 0.0,
                        "tokens": int(tokens), "window": int(window)},
            "tokens_5h": int(sum(k for (_, k) in ent["events"])),
            "_last": last,
        }


def v1_rows(sessions: list, now: float, top_n: int = 8) -> list:
    """The <= 0.3 per-project rows the phone parses: {name, tokens, share, active, idle_s,
    context_pct, context_tokens} (the newest session drives a project's context fill)."""
    agg: dict = {}
    for s in sessions:
        a = agg.setdefault(s["project"], {"name": s["project"], "tokens": 0, "active": False,
                                          "idle_s": None, "context_pct": 0.0, "context_tokens": 0, "_t": -1})
        a["tokens"] += s["tokens_5h"]
        a["active"] = a["active"] or s["active"]
        idle = 0 if s["active"] else max(0, int(now - s["since"]))
        a["idle_s"] = idle if a["idle_s"] is None else min(a["idle_s"], idle)
        if s["since"] >= a["_t"] and s["context"]["tokens"]:
            a["_t"] = s["since"]
            a["context_pct"], a["context_tokens"] = s["context"]["pct"], s["context"]["tokens"]
    rows = [a for a in agg.values() if a["tokens"] > 0 or a["context_tokens"]]
    total = sum(r["tokens"] for r in rows) or 1
    for r in rows:
        r.pop("_t")
        r["share"] = round(r["tokens"] / total * 100, 1)
    rows.sort(key=lambda r: r["tokens"], reverse=True)
    return rows[:top_n]
