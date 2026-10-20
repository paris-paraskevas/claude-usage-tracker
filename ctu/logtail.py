"""One incremental reader for ~/.claude/projects/*/*.jsonl feeding several consumers
(sessions, all-time). Each consumer keeps its own byte offset per file; the tailer reads from
the lowest offset once and hands each record only to consumers that haven't seen it.

A consumer's delta is committed only after its whole chunk was read and folded, so a partial
line or an error mid-file never double counts (the offset stays put and the bytes are re-read)."""
from __future__ import annotations

import json
import traceback
from datetime import datetime
from pathlib import Path

from ctu import io, paths

BACKFILL_MAX = 32 * 1024 * 1024     # a consumer's first read of a big file starts this far from the end


def parse_iso(s):
    try:
        s = str(s)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def project_name(cwd: str | None, fallback: str) -> str:
    """Basename of a session's cwd (Windows '\\' or POSIX '/'), else `fallback`."""
    if cwd:
        base = cwd.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
        if base:
            return base
    return fallback


def usage_of(o: dict) -> dict | None:
    """The token-usage dict on a record (`message.usage` or top-level `usage`), or None."""
    msg = o.get("message")
    u = msg.get("usage") if isinstance(msg, dict) else o.get("usage")
    return u if isinstance(u, dict) else None


def iter_records(path: Path, offset: int, end: int | None = None):
    """Yield (start, next_offset, record) for the COMPLETE lines of `path` in [offset, end).

    `end` is the size the caller stat'd, so bytes appended later wait for the next poll. A last
    line without a newline is a write in progress: left for later unless it already parses as a
    whole JSON object. Blank / unparseable lines are skipped (but consumed)."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        cur = offset
        while end is None or cur < end:
            raw = fh.readline() if end is None else fh.readline(end - cur)
            if not raw:
                break
            if not raw.endswith(b"\n"):
                try:
                    rec = json.loads(raw.decode("utf-8", errors="replace"))
                except Exception:
                    break
                if isinstance(rec, dict):
                    yield cur, cur + len(raw), rec
                break
            start, cur = cur, cur + len(raw)
            line = raw.strip()
            if not line:
                yield start, cur, None
                continue
            try:
                rec = json.loads(line.decode("utf-8", errors="replace"))
            except Exception:
                rec = None
            yield start, cur, rec if isinstance(rec, dict) else None


def read_appended(path: Path, offset: int, end: int | None = None, pos: list | None = None):
    """Records from `iter_records`; pos[0] tracks the offset just past the last consumed line."""
    if pos is None:
        pos = [offset]
    pos[0] = offset
    for _start, nxt, rec in iter_records(path, offset, end):
        pos[0] = nxt
        if rec is not None:
            yield rec


def _next_line(path: Path, offset: int) -> int:
    """First line boundary at or after `offset`."""
    if offset <= 0:
        return 0
    with open(path, "rb") as fh:
        fh.seek(offset - 1)
        if fh.read(1) == b"\n":
            return offset
        fh.readline()
        return fh.tell()


class Consumer:
    """Interface. `want` returns the offset to read from (None = nothing to read), or
    (offset, True) for a backfill hint that must be aligned to a line boundary."""
    name = "consumer"

    def want(self, key: str, st, now: float):
        raise NotImplementedError

    def begin(self, key: str, path: Path):
        raise NotImplementedError

    def feed(self, delta, rec: dict, key: str, path: Path) -> None:
        raise NotImplementedError

    def commit(self, key: str, delta, offset: int) -> None:
        raise NotImplementedError

    def forget(self, live: set) -> None:
        pass


class LogTailer:
    def __init__(self, consumers: list):
        self.consumers = consumers

    def files(self):
        try:
            return list(paths.PROJECTS_DIR.glob("*/*.jsonl"))
        except Exception:
            return []

    def poll(self, now: float) -> bool:
        """Fold newly appended bytes into every consumer. True if anything was read."""
        files = self.files()
        changed = False
        for f in files:
            try:
                st = f.stat()
            except Exception:
                continue
            key = str(f)
            offs = {}
            for c in self.consumers:
                w = c.want(key, st, now)
                if isinstance(w, tuple):
                    try:
                        w = _next_line(f, w[0])
                    except Exception:
                        w = None
                if w is not None and w < st.st_size:
                    offs[c] = w
            if not offs:
                continue
            changed |= self._read(f, key, st.st_size, offs)
        live = {str(f) for f in files}
        for c in self.consumers:
            try:
                c.forget(live)
            except Exception:
                pass
        return changed

    def _read(self, f: Path, key: str, size: int, offs: dict) -> bool:
        deltas, failed = {}, set()
        for c in offs:
            try:
                deltas[c] = c.begin(key, f)
            except Exception:
                failed.add(c)
        start = min(offs.values())
        end_pos = start
        try:
            for rec_start, nxt, rec in iter_records(f, start, size):
                end_pos = nxt
                if rec is None:
                    continue
                for c, off in offs.items():
                    if rec_start < off or c in failed:
                        continue
                    try:
                        c.feed(deltas[c], rec, key, f)
                    except Exception:
                        failed.add(c)
                        io.log(f"{c.name}: fold error in {f.name}:\n" + traceback.format_exc())
        except Exception:
            io.log(f"log tail read error on {f.name}:\n" + traceback.format_exc())
            return False
        for c, off in offs.items():
            if c not in failed and end_pos > off:
                c.commit(key, deltas[c], end_pos)
        return end_pos > start
