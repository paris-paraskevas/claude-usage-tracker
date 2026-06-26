"""Install Claude Usage Tracker as a desktop app.

Creates/updates the local virtualenv, generates the app icon, and adds
Desktop + Start Menu + Startup shortcuts (so it auto-starts on login).

    python install.py

Re-running is safe — it just refreshes everything. Remove with uninstall.py.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PY = VENV / "Scripts" / "python.exe"
PYW = VENV / "Scripts" / "pythonw.exe"
ICO = ROOT / "app.ico"
SCRIPT = ROOT / "claude_usage_tracker.py"


def ensure_venv():
    if not PY.exists():
        print("Creating virtualenv (.venv)…")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    print("Installing dependencies…")
    subprocess.run([str(PY), "-m", "pip", "install", "--disable-pip-version-check",
                    "-q", "-r", str(ROOT / "requirements.txt")], check=True)


def make_icons():
    sys.path.insert(0, str(ROOT))
    import claude_usage_tracker as m
    from PIL import Image
    base = m.make_icon_image({"five_hour": {"pct": 66}, "seven_day": {"pct": 40}})
    base.save(ROOT / "app_icon.png")
    base.resize((256, 256), Image.NEAREST).save(
        ICO, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("Generated app icon.")


def _q(p):
    return str(p).replace("'", "''")   # escape single quotes for PowerShell literals


def make_shortcuts():
    ps = (
        "$ws=New-Object -ComObject WScript.Shell;"
        "$dirs=@([Environment]::GetFolderPath('Desktop'),"
        "[Environment]::GetFolderPath('Programs'),"
        "[Environment]::GetFolderPath('Startup'));"
        f"$t='{_q(PYW)}';$a='\"{_q(SCRIPT)}\"';$wd='{_q(ROOT)}';$ic='{_q(ICO)}';"
        "foreach($d in $dirs){$p=Join-Path $d 'Claude Usage Tracker.lnk';"
        "$s=$ws.CreateShortcut($p);$s.TargetPath=$t;$s.Arguments=$a;"
        "$s.WorkingDirectory=$wd;$s.IconLocation=$ic;"
        "$s.Description='Claude Usage Tracker';$s.Save()};"
        "'Created shortcuts: Desktop, Start Menu, Startup'"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], check=True)


if __name__ == "__main__":
    ensure_venv()
    make_icons()
    make_shortcuts()
    print("\nInstalled. Find 'Claude Usage Tracker' in the Start Menu / Desktop,")
    print("or it will start automatically at your next login.")
    print(f"\nStart it now:\n  \"{PYW}\" \"{SCRIPT}\"")
