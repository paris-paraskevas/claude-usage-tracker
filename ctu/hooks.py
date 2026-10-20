"""Edits to ~/.claude/settings.json: our session hooks and our statusLine command.

Safety rules: never rewrite a file that doesn't parse as a JSON object, back the original up
once (settings.json.cut-bak), never replace a statusLine that isn't ours, and only touch
entries whose command carries our flag."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ctu import io, paths

HOOK_FLAG = "--session-hook"
STATUSLINE_FLAG = "--statusline"
# event -> matcher (None = the event has no matcher)
HOOK_EVENTS = {"Stop": None, "Notification": "permission_prompt", "UserPromptSubmit": None, "SessionEnd": None}


def _pythonw() -> str:
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.exists() else exe)


def _short_path(p: str) -> str:
    """Windows 8.3 path (no spaces) when available, so the command parses in Git Bash,
    PowerShell and cmd alike; else the path unchanged."""
    if os.name != "nt" or " " not in p:
        return p
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(p, buf, 1024)
        if 0 < n < 1024 and " " not in buf.value:
            return buf.value
    except Exception:
        pass
    return p


def _arg(p: str) -> str:
    p = _short_path(str(p)).replace("\\", "/")        # Git Bash eats unquoted backslashes
    return f'"{p}"' if " " in p else p


def _command(flag: str) -> str:
    if getattr(sys, "frozen", False):
        return f"{_arg(sys.executable)} {flag}"
    script = Path(sys.modules["ctu"].__file__).resolve().parent.parent / "claude_usage_tracker.py"
    if not script.exists():                                       # wheel install: run the package
        return f"{_arg(_pythonw())} -m ctu {flag}"
    return f"{_arg(_pythonw())} {_arg(script)} {flag}"


def hook_command() -> str:
    return _command(HOOK_FLAG)


def statusline_command() -> str:
    return _command(STATUSLINE_FLAG)


def load_settings():
    """(dict, ok). Missing file -> ({}, True); unparseable or not an object -> (None, False)."""
    p = paths.CLAUDE_SETTINGS
    if not p.exists():
        return {}, True
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        io.log(f"~/.claude/settings.json is not valid JSON ({exc}); leaving it untouched")
        return None, False
    if not isinstance(data, dict):
        io.log("~/.claude/settings.json is not a JSON object; leaving it untouched")
        return None, False
    return data, True


def save_settings(data: dict) -> bool:
    p = paths.CLAUDE_SETTINGS
    try:
        bak = p.with_name(p.name + ".cut-bak")
        if p.exists() and not bak.exists():     # one backup of the ORIGINAL, before our first edit
            bak.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        return io.save_json(p, data, pretty=True)
    except Exception as exc:
        io.log(f"write claude settings failed: {exc}")
        return False


def _is_ours(h, flag=HOOK_FLAG) -> bool:
    return isinstance(h, dict) and flag in str(h.get("command", ""))


def _strip(groups) -> list:
    """`groups` without our hook command (now-empty groups dropped)."""
    out = []
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            out.append(g)
            continue
        kept = [h for h in (g.get("hooks") or []) if not _is_ours(h)]
        if kept:
            g = dict(g)
            g["hooks"] = kept
            out.append(g)
    return out


def install_session_hook() -> bool:
    """Register `--session-hook` on Stop, Notification[permission_prompt], UserPromptSubmit and
    SessionEnd. Idempotent (refreshes the command path). False if settings.json can't be parsed."""
    try:
        data, ok = load_settings()
        if not ok:
            return False
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            io.log("~/.claude/settings.json has a non-object 'hooks'; leaving it untouched")
            return False
        before = json.dumps(data, sort_keys=True)
        cmd = hook_command()
        for event in list(hooks):                       # drop ours everywhere (incl. the legacy idle_prompt one)
            if isinstance(hooks.get(event), list):
                cleaned = _strip(hooks[event])
                if cleaned:
                    hooks[event] = cleaned
                else:
                    hooks.pop(event)
        for event, matcher in HOOK_EVENTS.items():
            groups = hooks.get(event)
            if not isinstance(groups, list):
                groups = hooks[event] = []
            g = {"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}
            if matcher:
                g = {"matcher": matcher, **g}
            groups.append(g)
        if json.dumps(data, sort_keys=True) == before:
            return True
        if not save_settings(data):
            return False
        io.log("installed the session hooks")
        return True
    except Exception as exc:
        io.log(f"install session hook failed: {exc}")
        return False


def remove_session_hook() -> bool:
    """Strip our hook from every event, keeping everything else. Writes only on change; never
    touches a file it can't parse. True when the hook is (now) absent."""
    try:
        data, ok = load_settings()
        if not ok:
            return False
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return True
        before = json.dumps(hooks, sort_keys=True)
        for event in list(hooks):
            if isinstance(hooks.get(event), list):
                cleaned = _strip(hooks[event])
                if cleaned:
                    hooks[event] = cleaned
                else:
                    hooks.pop(event)
        if json.dumps(hooks, sort_keys=True) == before:
            return True
        if not hooks:
            data.pop("hooks", None)
        if not save_settings(data):
            return False
        io.log("removed the session hooks")
        return True
    except Exception as exc:
        io.log(f"remove session hook failed: {exc}")
        return False


def hook_installed() -> bool:
    data, ok = load_settings()
    hooks = (data or {}).get("hooks") if ok else None
    if not isinstance(hooks, dict):
        return False
    return all(any(_is_ours(h) for g in (hooks.get(ev) or []) if isinstance(g, dict) for h in (g.get("hooks") or []))
               for ev in HOOK_EVENTS)


def statusline_status() -> dict:
    """{"installed": ours, "existing": someone else's command or None, "readable": bool}."""
    data, ok = load_settings()
    if not ok:
        return {"installed": False, "existing": None, "readable": False}
    sl = data.get("statusLine")
    if not sl:
        return {"installed": False, "existing": None, "readable": True}
    cmd = str(sl.get("command", "")) if isinstance(sl, dict) else str(sl)
    if STATUSLINE_FLAG in cmd and isinstance(sl, dict):
        return {"installed": True, "existing": None, "readable": True}
    return {"installed": False, "existing": cmd or json.dumps(sl), "readable": True}


def install_statusline() -> dict:
    """Add our statusLine command. An existing statusLine that isn't ours is reported, never replaced."""
    data, ok = load_settings()
    if not ok:
        return {"ok": False, "installed": False, "error": "unreadable",
                "message": "~/.claude/settings.json isn't valid JSON, so it was left untouched."}
    sl = data.get("statusLine")
    cmd = str(sl.get("command", "")) if isinstance(sl, dict) else ""
    if sl and STATUSLINE_FLAG not in cmd:
        return {"ok": False, "installed": False, "error": "existing", "existing": cmd or json.dumps(sl),
                "message": "Claude Code already has a status line. Remove it first, or keep using API polling."}
    new = {"type": "command", "command": statusline_command(), "padding": 0}
    if isinstance(sl, dict) and sl.get("command") == new["command"]:
        return {"ok": True, "installed": True, "message": "The status line is already installed."}
    data["statusLine"] = {**(sl if isinstance(sl, dict) else {}), **new}
    if not save_settings(data):
        return {"ok": False, "installed": False, "error": "write_failed", "message": "Couldn't write ~/.claude/settings.json."}
    io.log("installed the statusLine command")
    return {"ok": True, "installed": True, "message": "Status line installed. Usage updates live from the next Claude reply."}


def remove_statusline() -> bool:
    """Remove our statusLine (someone else's is left alone). True when ours is (now) absent."""
    data, ok = load_settings()
    if not ok:
        return False
    sl = data.get("statusLine")
    if not (isinstance(sl, dict) and STATUSLINE_FLAG in str(sl.get("command", ""))):
        return True
    data.pop("statusLine", None)
    if not save_settings(data):
        return False
    io.log("removed the statusLine command")
    return True
