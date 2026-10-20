"""Usage samples per limit: [(t, used_pct)], capped by time.

On disk (history.jsonl) only value changes are appended — never rewritten per tick. In memory
an unchanged reading is also kept once per step (for a day), so the pace fit can tell
"observed flat" from "not observed"."""
from __future__ import annotations

import bisect
import json
import threading

from ctu import io, paths

KEEP_S = 32 * 86400                 # covers the 30-day History range
HEARTBEAT_KEEP_S = 86400
STEP_S = {"five_hour": 60}          # weekly scopes: 600
MAX_POINTS = 800


def _step(lid):
    return STEP_S.get(lid, 600)


class History:
    def __init__(self):
        self.series: dict = {}       # limit id -> [(t, pct, heartbeat)]
        self.lock = threading.Lock()
        self._appends = 0

    def load(self, now: float) -> None:
        p = paths.HISTORY_PATH
        rows, dropped = [], 0
        try:
            with open(p, "r", encoding="utf-8") as fh:
                for ln in fh:
                    try:
                        t, lid, v = json.loads(ln)
                    except Exception:
                        dropped += 1
                        continue
                    if now - t > KEEP_S:
                        dropped += 1
                        continue
                    rows.append((float(t), str(lid), float(v)))
        except FileNotFoundError:
            rows = self._legacy(now)
            if rows:
                self._rewrite(rows)
        except Exception as exc:
            io.log(f"history load failed: {exc}")
        with self.lock:
            for t, lid, v in sorted(rows):
                self.series.setdefault(lid, []).append((t, v, False))
        if dropped > 200:
            self._rewrite(rows)      # compaction: at most once per start

    @staticmethod
    def _legacy(now: float) -> list:
        """<= 0.3 history.json ({t:[], five_hour:[], seven_day:[]}) -> change points."""
        h = io.load_json(paths.LEGACY_HISTORY_PATH, None)
        if not isinstance(h, dict):
            return []
        rows = []
        for lid in ("five_hour", "seven_day"):
            last = None
            for t, v in zip(h.get("t") or [], h.get(lid) or []):
                if isinstance(t, (int, float)) and isinstance(v, (int, float)) and now - t <= KEEP_S and v != last:
                    rows.append((float(t), lid, float(v)))
                    last = v
        return rows

    @staticmethod
    def _rewrite(rows) -> None:
        p = paths.HISTORY_PATH
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(json.dumps([int(t), lid, v]) + "\n" for t, lid, v in sorted(rows)), encoding="utf-8")
            tmp.replace(p)
        except Exception as exc:
            io.log(f"history compaction failed: {exc}")

    def observe(self, lid: str, t: float, pct: float) -> bool:
        """Record a reading. True when the value changed (and was appended to disk)."""
        pct = round(float(pct), 2)
        with self.lock:
            s = self.series.setdefault(lid, [])
            if s and t <= s[-1][0]:
                return False
            changed = not s or s[-1][1] != pct
            if changed or t - s[-1][0] >= _step(lid):
                s.append((t, pct, not changed))
                self._appends += 1
                if self._appends % 500 == 0:
                    self._prune(t)
        if changed:
            io.append_line(paths.HISTORY_PATH, json.dumps([int(t), lid, pct]))
        return changed

    def _prune(self, now: float) -> None:
        for lid, s in self.series.items():
            self.series[lid] = [x for x in s if now - x[0] <= KEEP_S and not (x[2] and now - x[0] > HEARTBEAT_KEEP_S)]

    def samples(self, lid: str) -> list:
        with self.lock:
            return [(t, v) for t, v, _ in self.series.get(lid, ())]

    def since(self, lid: str, start: float) -> list:
        with self.lock:
            s = self.series.get(lid, [])
            i = bisect.bisect_left(s, (start,))
            return [[int(t), v] for t, v, _ in s[i:]]

    def usage(self, start: float, now: float) -> dict:
        """{t, five_hour, seven_day}: both limits' step values on one time axis since `start`,
        at most MAX_POINTS (None before a limit's first sample)."""
        with self.lock:
            fh = [(t, v) for t, v, _ in self.series.get("five_hour", ()) if t <= now]
            sd = [(t, v) for t, v, _ in self.series.get("seven_day", ()) if t <= now]
        axis = sorted({t for t, _ in fh + sd if t >= start})
        if len(axis) > MAX_POINTS:
            stride = -(-len(axis) // MAX_POINTS)
            axis = axis[::stride] + ([axis[-1]] if (len(axis) - 1) % stride else [])

        def at(series, ts, t):
            i = bisect.bisect_right(ts, t) - 1
            return series[i][1] if i >= 0 else None
        out = {"t": [], "five_hour": [], "seven_day": []}
        fts, sts = [x[0] for x in fh], [x[0] for x in sd]
        for t in axis:
            out["t"].append(int(t))
            out["five_hour"].append(at(fh, fts, t))
            out["seven_day"].append(at(sd, sts, t))
        return out

    def v1(self, n: int = 240) -> dict:
        """{t, five_hour, seven_day}: the 5-hour sample times with the weekly value at each."""
        with self.lock:
            fh = [(t, v) for t, v, _ in self.series.get("five_hour", ())][-n:]
            sd = [(t, v) for t, v, _ in self.series.get("seven_day", ())]
        if not fh:
            return {}
        ts = [t for t, _ in sd]
        out = {"t": [], "five_hour": [], "seven_day": []}
        for t, v in fh:
            i = bisect.bisect_right(ts, t) - 1
            out["t"].append(int(t))
            out["five_hour"].append(v)
            out["seven_day"].append(sd[i][1] if i >= 0 else 0.0)
        return out
