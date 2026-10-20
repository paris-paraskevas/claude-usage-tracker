"""Remove Claude Usage Tracker shortcuts (Desktop, Start Menu, Startup) and its Claude Code
integration (the session hooks and the statusLine command) from ~/.claude/settings.json.

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
    """Strip our --session-hook / --statusline entries from ~/.claude/settings.json (everything
    else is kept; an unparseable file is left untouched). Uses the app's own logic."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from ctu.install import remove_claude_integration
    except Exception as exc:
        print(f"Could not load the app to remove its Claude Code hooks: {exc}")
        return False
    return remove_claude_integration()


if __name__ == "__main__":
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS])
    print("Shortcuts removed.")
    if remove_hook():
        print("Hooks and status line removed from ~/.claude/settings.json (if they were installed).")
    else:
        print("Couldn't update ~/.claude/settings.json — remove the '--session-hook' / '--statusline' entries by hand.")
    print("Stop a running instance from its tray icon (Quit).")
