"""Filesystem locations.

Other modules read these as `paths.X` at call time, so tests and --selftest can rebind them
(monkeypatch, or use_data_dir()). Importing this module touches nothing on disk."""
from __future__ import annotations

import os
import sys
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
CLAUDE_DIR = HOME / ".claude"
CREDS_PATH = CLAUDE_DIR / ".credentials.json"
CLAUDE_JSON = HOME / ".claude.json"                  # oauthAccount (email / name / org)
CLAUDE_SETTINGS = CLAUDE_DIR / "settings.json"       # where the hooks / statusLine install
PROJECTS_DIR = CLAUDE_DIR / "projects"               # session logs: */<session-id>.jsonl


def default_data_dir() -> Path:
    """%LOCALAPPDATA%\\ClaudeUsageTracker on every install type (source, pipx, frozen)."""
    return Path(os.environ.get("LOCALAPPDATA") or str(HOME / "AppData" / "Local")) / "ClaudeUsageTracker"


def use_data_dir(d) -> None:
    """(Re)bind every per-user data path under `d`."""
    global DATA_DIR, CONFIG_PATH, STATE_PATH, HISTORY_PATH, LEGACY_HISTORY_PATH, SNAPSHOT_PATH, ALLTIME_PATH
    global STATUSLINE_PATH, LOG_PATH, ICON_PATH, ICO_PATH, REMOTE_PATH, PORT_PATH, WEBVIEW_DIR
    DATA_DIR = d = Path(d)
    CONFIG_PATH = d / "config.json"
    STATE_PATH = d / "state.json"                  # alert episodes, levels, mute
    HISTORY_PATH = d / "history.jsonl"             # usage samples, appended on change
    LEGACY_HISTORY_PATH = d / "history.json"       # <= 0.3 format, migrated once
    SNAPSHOT_PATH = d / "last_snapshot.json"       # preload for a fast first paint
    ALLTIME_PATH = d / "alltime.json"
    STATUSLINE_PATH = d / "statusline.json"        # written by `--statusline`
    LOG_PATH = d / "claude_usage_tracker.log"
    ICON_PATH = d / "app_icon.png"
    ICO_PATH = d / "app.ico"
    REMOTE_PATH = d / "remote.json"                # phone pairing secrets (opt-in)
    PORT_PATH = d / "server_port"                  # live API port, for the hook client / MCP
    WEBVIEW_DIR = d / "webview"


use_data_dir(default_data_dir())


def ensure_data_dir() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def ui_dir() -> Path:
    """ctu/ui: next to this file in a checkout or wheel, under _MEIPASS when frozen."""
    base = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and base:
        return Path(base) / "ctu" / "ui"
    try:
        from importlib.resources import files
        return Path(str(files("ctu") / "ui"))
    except Exception:
        return Path(__file__).resolve().parent / "ui"
