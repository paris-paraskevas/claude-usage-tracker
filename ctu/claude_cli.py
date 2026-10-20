"""Claude Code CLI: locate it and start its own sign-in (`claude auth login`). We never write
the token: Claude Code owns ~/.claude/.credentials.json and the next poll picks the login up."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from ctu import io

# Address-safe characters only: the visible path shells out via `cmd /k`, so anything else
# (quotes, spaces, & | < > ^ …) is rejected to keep a localhost caller from injecting commands.
_EMAIL_SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@._%+-")


def claude_cli():
    p = shutil.which("claude")
    if p:
        return p
    for cand in (Path.home() / ".local" / "bin" / "claude.exe", Path.home() / ".local" / "bin" / "claude"):
        if cand.exists():
            return str(cand)
    return None


def safe_login_email(email):
    if email and not (set(email) - _EMAIL_SAFE_CHARS) and "@" in email:
        return email
    return None


def launch_login(email=None, visible: bool = False) -> dict:
    """Start `claude auth login` (browser sign-in). Returns {ok, message} for the UI to show."""
    exe = claude_cli()
    if not exe:
        return {"ok": False, "message": "Claude Code wasn't found. Install it, or run `claude auth login` in a terminal."}
    email = safe_login_email(email)
    args = [exe, "auth", "login"] + (["--email", email] if email else [])
    try:
        if os.name == "nt" and visible:
            cmd = "cmd /k " + " ".join(f'"{a}"' for a in args)
            subprocess.Popen(cmd, creationflags=0x00000010, close_fds=True)       # CREATE_NEW_CONSOLE
            return {"ok": True, "message": "Complete the sign-in in the terminal window."}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
        subprocess.Popen(args, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "message": "Opening Claude sign-in in your browser…"}
    except Exception as exc:
        io.log(f"login launch failed: {exc}")
        return {"ok": False, "message": "Couldn't open sign-in. Run `claude auth login` in a terminal."}
