"""Logging and JSON file helpers (atomic writes, parent dirs created on demand)."""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

from ctu import paths

_LOG_LOCK = threading.Lock()


def now_local() -> datetime:
    return datetime.now().astimezone()


def log(msg: str) -> None:
    """Append one line to the app log (rotated to its last 300 lines past 1 MB)."""
    line = f"{now_local():%Y-%m-%d %H:%M:%S}  {msg}"
    with _LOG_LOCK:
        try:
            p = paths.LOG_PATH
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size > 1_000_000:
                tail = p.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
                p.write_text("\n".join(tail) + "\n", encoding="utf-8")
            with p.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass
    try:
        if sys.stdout and sys.stdout.isatty():
            print(line)
    except Exception:
        pass


def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"read {path.name} failed: {exc}")
    return default


def dumps(data, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(data, indent=2)
    return json.dumps(data, separators=(",", ":"))


def save_json(path: Path, data, pretty: bool = False) -> bool:
    """Atomic write (tmp + replace). Compact unless `pretty` (files people edit)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(dumps(data, pretty), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as exc:
        log(f"write {path.name} failed: {exc}")
        return False


def append_line(path: Path, line: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:
        log(f"append {path.name} failed: {exc}")


def file_sig(path: Path):
    """(mtime_ns, size) or None — cheap change detection without reading the file."""
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except Exception:
        return None
