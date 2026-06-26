"""Set up Claude Usage Tracker from a source checkout.

Creates a local virtualenv, installs dependencies, then runs the app's
interactive setup (Desktop / Start Menu / Startup shortcuts).

    python install.py

If you'd rather install it like a normal tool (and update with `pipx upgrade`):

    pipx install git+https://github.com/paris-paraskevas/claude-usage-tracker.git
    claude-usage-tracker --install
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PY = VENV / "Scripts" / "python.exe"
SCRIPT = ROOT / "claude_usage_tracker.py"

if __name__ == "__main__":
    if not PY.exists():
        print("Creating virtualenv (.venv)…")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    print("Installing dependencies…")
    subprocess.run([str(PY), "-m", "pip", "install", "--disable-pip-version-check",
                    "-q", "-r", str(ROOT / "requirements.txt")], check=True)
    # Generate the icon and run the interactive shortcut/autostart setup.
    subprocess.run([str(PY), str(SCRIPT), "--install"])
