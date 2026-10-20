"""Update check (GitHub releases for the installer, PyPI for pip/pipx) and self-update."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from ctu import __version__, io, paths

RELEASES_URL = "https://github.com/paris-paraskevas/claude-usage-tracker/releases/latest"
GITHUB_API = "https://api.github.com/repos/paris-paraskevas/claude-usage-tracker/releases/latest"
PYPI_URL = "https://pypi.org/pypi/claude-usage-tracker/json"


def vtuple(v):
    try:
        return tuple(int(x) for x in str(v).split(".")[:3])
    except Exception:
        return (0,)


def is_installed_pkg() -> bool:
    """Running from site-packages (pip/pipx), not a source checkout or the frozen build."""
    if getattr(sys, "frozen", False):
        return False
    return "/site-packages/" in str(Path(__file__).resolve()).replace("\\", "/").lower()


def check_github_latest():
    """(latest_version, installer_url) from the latest GitHub release, or (None, None)."""
    try:
        req = urllib.request.Request(GITHUB_API, headers={"Accept": "application/vnd.github+json",
                                                          "User-Agent": "claude-usage-tracker"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.load(resp)
        ver = (d.get("tag_name") or "").lstrip("v")
        url = next((a.get("browser_download_url") for a in d.get("assets", [])
                    if str(a.get("name", "")).lower().endswith(".exe")), None)
        return (ver or None), url
    except Exception:
        return None, None


def check_pypi_latest():
    try:
        req = urllib.request.Request(PYPI_URL, headers={"Accept": "application/json", "User-Agent": "claude-usage-tracker"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return (json.load(resp).get("info") or {}).get("version") or None
    except Exception:
        return None


def check_latest():
    """(version, installer_url) from the source this copy updates from (PyPI can lag GitHub)."""
    if is_installed_pkg():
        return check_pypi_latest(), None
    return check_github_latest()


def check() -> dict:
    latest, url = check_latest()
    newer = bool(latest and vtuple(latest) > vtuple(__version__))
    return {"current": __version__, "latest": latest, "url": url or RELEASES_URL, "update": newer}


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def self_update(version: str | None, installer_url: str | None, quit_app) -> dict:
    """Frozen: download and run the installer silently (it closes, upgrades and relaunches us).
    pip/pipx: upgrade in place with our own interpreter, then relaunch. Checkout: point to releases."""
    if getattr(sys, "frozen", False) and installer_url:
        try:
            dst = os.path.join(os.environ.get("TEMP", str(paths.DATA_DIR)), "ClaudeUsageTracker-Setup.exe")
            req = urllib.request.Request(installer_url, headers={"User-Agent": "claude-usage-tracker"})
            with urllib.request.urlopen(req, timeout=180) as r, open(dst, "wb") as f:
                f.write(r.read())
            subprocess.Popen([dst, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], close_fds=True)
            quit_app()
            return {"ok": True, "message": f"Installing v{version}…"}
        except Exception as exc:
            io.log(f"self-update failed: {exc}")
            return {"ok": False, "message": "The download failed. Opening the releases page.", "url": RELEASES_URL}
    if not is_installed_pkg():
        return {"ok": False, "message": "This copy runs from a source checkout: update it with git.", "url": RELEASES_URL}
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "claude-usage-tracker"],
                           capture_output=True, text=True, timeout=300, creationflags=_no_window())
        if r.returncode != 0:
            io.log("self-update failed:\n" + (r.stdout or "") + (r.stderr or ""))
            return {"ok": False, "message": "pip couldn't upgrade it. Try `pipx upgrade claude-usage-tracker`.",
                    "url": RELEASES_URL}
        relaunch_after_exit()
        quit_app()
        return {"ok": True, "message": f"Updated to v{version}. Restarting…"}
    except Exception as exc:
        io.log(f"self-update error: {exc}")
        return {"ok": False, "message": "The upgrade failed.", "url": RELEASES_URL}


def relaunch_after_exit() -> None:
    """Start a fresh instance that waits for this one to release the single-instance lock."""
    try:
        import shutil
        gui = shutil.which("claude-usage-tracker-gui")
        cmd = [gui] if gui else [sys.executable, "-m", "ctu"]
        subprocess.Popen(cmd + ["--wait-for-lock"], close_fds=True, creationflags=_no_window())
    except Exception as exc:
        io.log(f"relaunch failed: {exc}")
