"""Remove Claude Usage Tracker shortcuts (Desktop, Start Menu, Startup).

    python uninstall.py

Leaves the code and your data (config/history in %LOCALAPPDATA% or the project
folder) intact. Delete the project folder to remove everything.
"""
import subprocess

PS = (
    "foreach($d in @([Environment]::GetFolderPath('Desktop'),"
    "[Environment]::GetFolderPath('Programs'),"
    "[Environment]::GetFolderPath('Startup'))){"
    "$p=Join-Path $d 'Claude Usage Tracker.lnk';"
    "if(Test-Path $p){Remove-Item $p -Force; \"removed $p\"}}"
)

if __name__ == "__main__":
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS])
    print("Shortcuts removed. Stop a running instance from its tray icon (Quit).")
