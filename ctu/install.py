"""Desktop / Start Menu / Startup shortcuts (PowerShell WScript.Shell) and uninstall cleanup."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from ctu import APP_NAME, hooks, paths


def pythonw() -> str:
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.exists() else exe)


def launch_target():
    """(target, args): the frozen exe, the pip/pipx gui launcher, or pythonw + the entry script."""
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    gui = shutil.which("claude-usage-tracker-gui")
    if gui and gui.lower().endswith(".exe"):
        return gui, ""
    script = Path(sys.modules["ctu"].__file__).resolve().parent.parent / "claude_usage_tracker.py"
    return pythonw(), (f'"{script}"' if script.exists() else "-m ctu")


def _ps_q(s) -> str:
    return str(s).replace("'", "''")


def make_shortcuts(target, args, locations) -> None:
    folders = "@(" + ",".join(f"[Environment]::GetFolderPath('{loc}')" for loc in locations) + ")"
    icon = str(paths.ICO_PATH) if paths.ICO_PATH.exists() else target
    ps = ("$ws=New-Object -ComObject WScript.Shell;"
          f"foreach($d in {folders}){{"
          f"$p=Join-Path $d '{_ps_q(APP_NAME)}.lnk';$s=$ws.CreateShortcut($p);"
          f"$s.TargetPath='{_ps_q(target)}';$s.Arguments='{_ps_q(args)}';"
          f"$s.WorkingDirectory='{_ps_q(Path(target).parent)}';$s.IconLocation='{_ps_q(icon)}';"
          f"$s.Description='{_ps_q(APP_NAME)}';$s.Save()}}")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], check=True)


def remove_shortcuts(locations=("Desktop", "Programs", "Startup")) -> None:
    folders = "@(" + ",".join(f"[Environment]::GetFolderPath('{loc}')" for loc in locations) + ")"
    ps = (f"foreach($d in {folders}){{"
          f"$p=Join-Path $d '{_ps_q(APP_NAME)}.lnk'; if(Test-Path $p){{Remove-Item $p -Force; \"removed $p\"}}}}")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])


def _ask(question, default=True) -> bool:
    try:
        a = input(f"  {question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    except EOFError:
        return default
    return default if not a else a.startswith("y")


def do_install() -> None:
    from ctu.tray import ensure_app_icon
    ensure_app_icon()
    target, args = launch_target()
    print(f"\n{APP_NAME} setup\n  launcher: {target}\n")
    locs = [loc for q, loc in (("Add a Desktop shortcut?", "Desktop"), ("Add to the Start Menu?", "Programs"),
                               ("Start automatically when you log in?", "Startup")) if _ask(q)]
    if locs:
        make_shortcuts(target, args, locs)
        print(f"\n  shortcuts created: {', '.join(locs)}")
    else:
        print("\n  no shortcuts created.")
    print("\n  Windows doesn't let an app pin itself: launch it, then right-click its taskbar icon -> Pin.")
    if _ask("\n  Launch it now?"):
        try:
            subprocess.Popen([target] + ([args.strip('"')] if args.strip() else []), close_fds=True)
            print("  launched — look for the tray icon by the clock.")
        except Exception as exc:
            print(f"  launch failed: {exc}")
    print()


def remove_claude_integration() -> bool:
    """Our hooks and our statusLine out of ~/.claude/settings.json (others untouched)."""
    a = hooks.remove_session_hook()
    b = hooks.remove_statusline()
    return a and b


def do_uninstall() -> None:
    remove_shortcuts()
    print("Removed Desktop / Start Menu / Startup shortcuts.")
    if remove_claude_integration():
        print("Removed our hooks and status line from ~/.claude/settings.json (if they were there).")
    else:
        print("Couldn't update ~/.claude/settings.json — remove the '--session-hook' / '--statusline' entries by hand.")


def install_autostart() -> None:
    from ctu.tray import ensure_app_icon
    ensure_app_icon()
    target, args = launch_target()
    make_shortcuts(target, args, ["Startup"])
    print("Autostart installed (Startup shortcut).")


def uninstall_autostart() -> None:
    remove_shortcuts(["Startup"])
    print("Autostart removed (Startup shortcut).")
