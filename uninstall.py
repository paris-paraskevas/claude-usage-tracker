"""Remove Claude Usage Tracker shortcuts (Desktop, Start Menu, Startup) and its
optional Claude Code "session waiting" Stop hook from ~/.claude/settings.json.

    python uninstall.py

Leaves the code and your data (config/history in %LOCALAPPDATA% or the project
folder) intact. Delete the project folder to remove everything.
"""
import subprocess
import sys
from pathlib import Path

PS = (
    "foreach($d in @([Environment]::GetFolderPath('Desktop'),"
    "[Environment]::GetFolderPath('Programs'),"
    "[Environment]::GetFolderPath('Startup'))){"
    "$p=Join-Path $d 'Claude Usage Tracker.lnk';"
    "if(Test-Path $p){Remove-Item $p -Force; \"removed $p\"}}"
)


def remove_hook() -> bool:
    """Strip our --session-hook entry from ~/.claude/settings.json (other hooks are kept;
    an unparseable file is left untouched). Uses the app's own logic from this checkout."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import claude_usage_tracker as cut
    except Exception as exc:
        print(f"Could not load the app to remove its Claude Code hook: {exc}")
        return False
    return cut.remove_session_hook()


if __name__ == "__main__":
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS])
    print("Shortcuts removed.")
    if remove_hook():
        print("Session-waiting hook removed from ~/.claude/settings.json (if it was installed).")
    else:
        print("Couldn't update ~/.claude/settings.json — remove the '--session-hook' Stop hook by hand.")
    print("Stop a running instance from its tray icon (Quit).")
