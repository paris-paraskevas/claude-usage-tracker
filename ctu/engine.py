"""The engine: one State (schema 2) with a `rev` counter.

Collectors (statusline watcher, usage API, log tailer, status, update, team, remote) run on a
scheduler with at most one job in flight per kind. Each applies its slice under the lock and
calls recompute(), which derives limits + pace, the decision and alerts, and bumps `rev` (waking
SSE clients and the tray) only when the content changed. Disk writes happen on change only."""
from __future__ import annotations

import copy
import hashlib
import heapq
import json
import queue
import threading
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor

from ctu import (__version__, alerts, alltime, config, forecast, history, hooks, io, paths, remote,
                 sessions, status, update, usage, v1, winplat)
from ctu.logtail import LogTailer
from ctu.team import pool
import ctu.team as team

PRIMARY = ("five_hour", "seven_day")
PRELOAD_EVERY_S = 300
ALLTIME_SAVE_EVERY_S = 600
TEAM_POOL_EVERY_S = 60
REFILLED_SHOW_S = 900


class Scheduler:
    """A heap of (due, name) on one thread plus a small worker pool; a job never overlaps itself."""

    def __init__(self, workers: int = 4):
        self.jobs: dict = {}
        self.heap: list = []
        self.cond = threading.Condition()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ctu-job")
        self.stopped = False
        self.thread = None

    def add(self, name: str, fn, interval: float, delay: float = 0.0) -> None:
        with self.cond:
            self.jobs[name] = {"fn": fn, "interval": interval, "running": False, "due": time.time() + delay}
            heapq.heappush(self.heap, (self.jobs[name]["due"], name))
            self.cond.notify()

    def kick(self, name: str) -> None:
        """Run a job now (or right after its current run)."""
        with self.cond:
            j = self.jobs.get(name)
            if not j:
                return
            if j["running"]:
                j["kicked"] = True
                return
            j["due"] = time.time()
            heapq.heappush(self.heap, (j["due"], name))
            self.cond.notify()

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="ctu-scheduler", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        with self.cond:
            self.stopped = True
            self.cond.notify()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        while True:
            with self.cond:
                if self.stopped:
                    return
                now = time.time()
                while self.heap and self.heap[0][0] <= now:
                    due, name = heapq.heappop(self.heap)
                    j = self.jobs.get(name)
                    if not j or j["running"] or due < j["due"] - 0.001:
                        continue                    # superseded entry, or already in flight
                    j["running"] = True
                    try:
                        self.pool.submit(self._run, name)
                    except RuntimeError:
                        return                      # shutting down
                timeout = (self.heap[0][0] - now) if self.heap else 1.0
                self.cond.wait(max(0.05, min(timeout, 1.0)))

    def _run(self, name: str) -> None:
        j = self.jobs[name]
        try:
            j["fn"]()
        except Exception:
            io.log(f"job {name} failed:\n" + traceback.format_exc())
        finally:
            with self.cond:
                j["running"] = False
                j["due"] = time.time() + (0.0 if j.pop("kicked", False) else j["interval"])
                heapq.heappush(self.heap, (j["due"], name))
                self.cond.notify()


def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class Engine:
    def __init__(self, cfg: dict | None = None, *, network: bool = True):
        self.cfg = cfg if cfg is not None else config.load()
        self.network = network
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.stopping = False
        persisted = io.load_json(paths.STATE_PATH, {})
        persisted = persisted if isinstance(persisted, dict) and persisted.get("v") == 2 else {}
        self.book = alerts.AlertBook(persisted.get("alerts"))
        self.levels: dict = dict(persisted.get("levels") or {})
        self.update_checked_at = persisted.get("update_checked_at") or 0
        self.history = history.History()
        self.sessions = sessions.SessionTracker()
        self.alltime = alltime.AllTime(io.load_json(paths.ALLTIME_PATH, None))
        self._alltime_saved_rev = self.alltime.rev
        self._alltime_saved_at = time.time()
        self.tailer = LogTailer([self.sessions, self.alltime])
        self.readings: dict = {}           # limit id -> {"pct", "resets_at", "at", "src"}
        self.extra = None
        self.sl_sig = None
        self.sl_ts = None
        self.sl_sessions: dict = {}
        self.api_last = 0.0
        self.api_last_ok = None
        self.api_error = None
        self.api_auth = None
        self.api_force = False
        self.status_raw = None
        self.update_info = None
        self.team_sync = team.TeamSync()
        self.team_info = None              # {"signed_in", "email", "role", "team"}
        self.team_merged = None
        self.team_pool_at = 0.0
        self._team_pool_err = None
        self.remote = remote.RemoteSync()
        self.refills: dict = {}            # limit id -> {"at", "resets_at"}
        self.platform = {"accent": dict(winplat.DEFAULT_ACCENT), "theme": "system", "clock": "system"}
        self.claude_integration = {"statusline": {"installed": False, "existing": None}, "hook": {"installed": False}}
        self.state: dict = {}
        self.rev = 0
        self._hash = None
        self._dirty = False
        self._preload_at = time.time()
        self._alltime_compact = (None, None)
        self.port = None
        self.host = None                   # the desktop shell (windows/tray), when running
        self.listeners: list = []          # called with the new state after each rev bump
        self._deliveries: queue.Queue = queue.Queue()
        self._notify_q: queue.Queue = queue.Queue()
        self.scheduler = Scheduler()
        self.history.load(time.time())
        self._preload()
        self._refresh_integration()          # recomputes

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._upgrade_claude_commands()
        t = float(self.cfg.get("tick_seconds", 10))
        s = self.scheduler
        s.add("statusline", self.job_statusline, 2)
        s.add("tail", self.job_tail, t)
        s.add("tick", self.job_tick, t)
        s.add("platform", self.job_platform, 60)
        if self.network:
            s.add("usage", self.job_usage, t)
            s.add("status", self.job_status, float(self.cfg.get("status_interval_seconds", 300)), delay=2)
            s.add("update", self.job_update, 3600, delay=30)
            s.add("team", self.job_team, 15, delay=1)
            s.add("remote", self.job_remote, max(5.0, float(self.cfg.get("remote_sync_seconds", 10))), delay=3)
        threading.Thread(target=self._deliver_loop, name="ctu-alerts", daemon=True).start()
        threading.Thread(target=self._listen_loop, name="ctu-listeners", daemon=True).start()
        s.start()

    def _upgrade_claude_commands(self) -> None:
        """0.3 registered only the Stop hook, and the exe may have moved: refresh what's ours."""
        try:
            if self.cfg.get("session_hook") and not hooks.hook_installed():
                hooks.install_session_hook()
            if hooks.statusline_status().get("installed"):
                hooks.install_statusline()             # rewrites our command if the path changed
            self._refresh_integration()
        except Exception:
            io.log("claude settings refresh failed:\n" + traceback.format_exc())

    def stop(self) -> None:
        with self.lock:
            self.stopping = True
            self.changed.notify_all()
        self.scheduler.stop()
        self._deliveries.put(None)
        self._notify_q.put(None)
        self.persist(force=True)
        self.write_preload()
        if self.alltime.rev != self._alltime_saved_rev:
            io.save_json(paths.ALLTIME_PATH, self.alltime.cache)

    def _preload(self) -> None:
        """Last-known readings from the previous run (they keep their real age, so staleness is honest)."""
        p = io.load_json(paths.SNAPSHOT_PATH, None)
        if not isinstance(p, dict) or p.get("v") != 2:
            return
        for lid, r in (p.get("readings") or {}).items():
            if lid in forecast.WINDOW_S and isinstance(r, dict) and isinstance(r.get("pct"), (int, float)):
                self.readings[lid] = {"pct": float(r["pct"]), "resets_at": r.get("resets_at"),
                                      "at": float(r.get("at") or 0), "src": r.get("src") or "api"}
        self.extra = p.get("extra")
        self.status_raw = p.get("status_raw")

    def write_preload(self) -> None:
        with self.lock:
            data = {"v": 2, "readings": self.readings, "extra": self.extra, "status_raw": self.status_raw}
        io.save_json(paths.SNAPSHOT_PATH, data)

    def persist(self, force: bool = False) -> None:
        with self.lock:
            if not (self._dirty or force):
                return
            data = {"v": 2, "levels": self.levels, "alerts": self.book.to_json(),
                    "update_checked_at": self.update_checked_at}
            self._dirty = False
        io.save_json(paths.STATE_PATH, data)

    # ------------------------------------------------------------------ collectors
    def _reading(self, lid: str, pct: float, resets_at, at: float, src: str) -> None:
        prev = self.readings.get(lid)
        if prev and prev["at"] > at:
            return
        self.readings[lid] = {"pct": float(pct), "resets_at": resets_at, "at": float(at), "src": src}
        self.history.observe(lid, at, pct)

    def apply_statusline(self, sl: dict | None) -> None:
        if not sl:
            return
        with self.lock:
            if sl.get("ts"):
                for lid, w in sl["windows"].items():
                    self._reading(lid, w["pct"], w["resets_at"], sl["ts"], "statusline")
                self.sl_ts = max(self.sl_ts or 0, sl["ts"])
            self.sl_sessions = sl.get("sessions") or {}
            self.recompute()

    def job_statusline(self) -> None:
        sig = io.file_sig(paths.STATUSLINE_PATH)
        if sig is None or sig == self.sl_sig:
            return
        self.sl_sig = sig
        self.apply_statusline(usage.read_statusline())

    def apply_fetch(self, r: usage.FetchResult, now: float) -> None:
        with self.lock:
            self.api_last, self.api_force = now, False
            prev_ok = self.api_last_ok
            self.api_last_ok = r.ok
            if r.ok:
                for lid, w in r.windows.items():
                    self._reading(lid, w["pct"], w["resets_at"], now, "api")
                for lid in ("seven_day_opus", "seven_day_sonnet"):
                    if lid not in r.windows:
                        self.readings.pop(lid, None)     # scope no longer reported
                self.extra = r.extra
                self.api_error, self.api_auth = None, None
            else:
                self.api_error = r.error
                self.api_auth = r.token_state if r.token_state != usage.TokenState.OK else None
            if prev_ok != r.ok:
                io.log(f"usage API: {'ok' if r.ok else r.error}")
            self.recompute()

    def job_usage(self) -> None:
        now = time.time()
        with self.lock:
            fresh = bool(self.sl_ts and now - self.sl_ts <= self.cfg["statusline_stale_seconds"])
            iv = self.cfg["api_extras_interval_seconds"] if fresh else self.cfg["api_fallback_interval_seconds"]
            due = now - self.api_last >= iv or (self.api_force and now - self.api_last >= 30)
        if due:
            self.apply_fetch(usage.fetch_usage(int(self.cfg.get("request_timeout_seconds", 20))), now)

    def job_tail(self) -> None:
        if self.tailer.poll(time.time()):
            with self.lock:
                self.recompute()

    def job_status(self) -> None:
        s = status.fetch_status()
        if s:
            with self.lock:
                self.status_raw = s
                self.recompute()

    def job_update(self) -> None:
        now = time.time()
        if not self.cfg.get("update_check", True) or now - self.update_checked_at < 86400:
            return
        res = update.check()
        with self.lock:
            self.update_checked_at = now
            self._dirty = True
            if res["latest"]:
                self.update_info = res
            self.recompute()

    def job_platform(self) -> None:
        """Accent / theme and our ~/.claude/settings.json integration, once a minute."""
        plat = {"accent": winplat.accent(), "theme": winplat.apps_theme(), "clock": winplat.clock_pref()}
        forecast.CLOCK_12H = plat["clock"] == "12h"
        with self.lock:
            if plat != self.platform:
                self.platform = plat
                self.recompute()
        self._refresh_integration()

    def job_team(self) -> None:
        now = time.time()
        if not pool.configured() or not pool.has_session():
            info, merged = ({"signed_in": False, "email": None, "role": None, "team": None}, None)
        else:
            signed = pool.signed_in()
            info = {"signed_in": signed, "email": pool.email() if signed else None,
                    "role": ("admin" if pool.is_admin() else "member") if signed else None,
                    "team": pool.team() if signed else None}
            merged = self.team_merged if signed else None
            if signed:
                with self.lock:
                    snap = {"windows": v1.windows(self.state, now), "extra": self.state.get("overage")}
                self.team_sync.sync(snap, self.cfg, self.alltime.cache, now)
                if now - self.team_pool_at >= TEAM_POOL_EVERY_S:
                    self.team_pool_at = now
                    try:
                        merged = team.supabase_team_overview(self.cfg.get("team_tz"))
                        if self._team_pool_err:
                            io.log("team overview: ok")
                        self._team_pool_err = None
                    except Exception as exc:
                        if str(exc) != self._team_pool_err:
                            io.log(f"team overview: {exc}")
                        self._team_pool_err = str(exc)
        with self.lock:
            self.team_info, self.team_merged = info, merged
            self.recompute()

    def job_remote(self) -> None:
        if not remote.RemoteSync.enabled(self.cfg) or not remote.load_remote_identity(create=False):
            return
        now = time.time()
        snap = self.v1_snapshot(now)
        if self.remote.due(snap, now, float(self.cfg.get("remote_sync_seconds", 10))):
            before = self.remote.last_sync
            self.remote.sync(snap, self.cfg, now)
            if self.remote.last_sync != before:
                with self.lock:
                    self.recompute()

    def job_tick(self) -> None:
        now = time.time()
        with self.lock:
            self.recompute()
        self.persist()
        if now - self._preload_at >= PRELOAD_EVERY_S:
            self._preload_at = now
            self.write_preload()
        if self.alltime.rev != self._alltime_saved_rev and now - self._alltime_saved_at >= ALLTIME_SAVE_EVERY_S:
            self._alltime_saved_rev, self._alltime_saved_at = self.alltime.rev, now
            io.save_json(paths.ALLTIME_PATH, self.alltime.cache)

    # ------------------------------------------------------------------ derivation
    def _limits(self, now: float, sess: list) -> tuple[list, str | None]:
        active = [s for s in sess if s["active"] and s["model"]]
        scope = forecast.scope_of_model(max(active, key=lambda s: s["since"])["model"]) if active else None
        if scope is None:
            sl = [v for v in self.sl_sessions.values() if isinstance(v, dict) and now - (v.get("ts") or 0) < 600]
            if sl:
                scope = forecast.scope_of_model(max(sl, key=lambda v: v.get("ts") or 0).get("model"))
        out = []
        for lid in forecast.ORDER:
            r = self.readings.get(lid)
            if not r:
                continue
            used, resets = r["pct"], r["resets_at"]
            if resets and resets <= now:
                used, resets = 0.0, None                 # the window is over: it has refilled
            pc = forecast.pace(lid, used, resets, self.history.samples(lid), now)
            raw = forecast.raw_level(used, pc["pace"])
            prev = self.levels.get(lid) or {}
            reset = bool(prev) and not (prev.get("resets_at") == resets or forecast.same_window(prev.get("resets_at"), resets))
            lvl = forecast.apply_hysteresis(prev.get("level"), raw, pc["pace"]["projected_at_reset"], used, reset)
            if reset and prev.get("level") == "limited":
                self.refills[lid] = {"at": now, "resets_at": prev.get("resets_at")}
            if prev.get("level") != lvl or prev.get("resets_at") != resets:
                self.levels[lid] = {"level": lvl, "resets_at": resets}
                self._dirty = True
            out.append({"id": lid, "label": forecast.LABELS[lid], "scope": forecast.SCOPES[lid],
                        "used_pct": round(used, 1), "resets_at": int(resets) if resets else None,
                        "window_s": forecast.WINDOW_S[lid], "elapsed_pct": pc["elapsed_pct"],
                        "level": lvl, "binding": False, "pace": pc["pace"]})
        return out, scope

    def _source(self, now: float) -> dict:
        prim = [self.readings[k] for k in PRIMARY if k in self.readings]
        newest = max(prim, key=lambda r: r["at"]) if prim else None
        updated = int(newest["at"]) if newest else None
        failed = bool(self.api_last_ok is False and self.api_last > (newest["at"] if newest else 0))
        stale = forecast.is_stale(updated, now, float(self.cfg["api_fallback_interval_seconds"]), failed)
        tok_state = usage.read_token()[1]
        auth = self.api_auth or tok_state
        if newest and newest["src"] == "statusline" and not stale:
            auth = usage.TokenState.OK if tok_state != usage.TokenState.MISSING else auth
        return {"kind": newest["src"] if newest else "api", "updated_at": updated,
                "age_s": int(now - updated) if updated else None, "stale": stale, "auth": auth,
                "error": self.api_error if failed else None}

    def _decision(self, limits, scope, source, now, pool_rows) -> dict:
        bid = forecast.pick_binding(limits, scope)
        b = next((l for l in limits if l["id"] == bid), None)
        for l in limits:
            l["binding"] = l["id"] == bid
        title, detail = forecast.texts(b, now, self.extra)
        if b is None and source["auth"] != usage.TokenState.OK:
            title = "Sign in to Claude Code"
            detail = ("Your Claude login has expired. Run any Claude Code command, or sign in."
                      if source["auth"] == usage.TokenState.EXPIRED else "No Claude Code login was found on this computer.")
        elif b is not None and source["stale"]:
            detail = f"As of {forecast.fmt_clock(source['updated_at'], now)} · {detail}"
        level = b["level"] if b else "ok"
        fh = next((l for l in limits if l["id"] == "five_hour"), None)
        return {"level": level, "binding": bid, "title": title, "detail": detail,
                "resume_at": forecast.resume_at(limits, scope),
                "suggestion": forecast.suggestion(pool_rows, fh["used_pct"] if fh else None, level, now)}

    def _conditions(self, limits, scope, sess, source, now) -> list:
        c = alerts.cond
        out = []
        live_keys = {a["key"]: a for a in self.book.live.values()}
        if source["stale"]:          # no new limit episodes from stale data; keep the live ones
            out += [c(a["kind"], a["key"], None, a["title"], a["body"]) | {"id": a["base_id"]}
                    for a in live_keys.values() if a["kind"] in ("will_hit", "limited", "refilled")]
        else:
            for l in limits:
                if l["scope"] not in ("all", scope):
                    continue
                t, d = forecast.texts(l, now, self.extra)
                if l["level"] == "will_hit":
                    out.append(c("will_hit", f"will_hit:{l['id']}", l["resets_at"], t, d.split(" Slow to")[0]))
                elif l["level"] == "limited":
                    out.append(c("limited", f"limited:{l['id']}", l["resets_at"], t, d))
            for lid, rf in list(self.refills.items()):
                if now - rf["at"] > REFILLED_SHOW_S:
                    self.refills.pop(lid, None)
                    continue
                name = forecast.LABELS[lid].replace(" · ", " ")
                out.append(c("refilled", f"refilled:{lid}", rf["resets_at"], f"{name} limit refilled",
                             "The window has reset. You can continue."))
        for s in sess:
            pct = s["context"]["pct"]
            key = f"context_high:{s['id']}"
            if s["active"] and (pct >= 90 or (key in live_keys and pct >= 85)):
                out.append(c("context_high", key, None, "Context almost full",
                             f"{s['project']} at {pct:.0f}% — consider /compact."))
            if s["state"] == "needs_approval":
                out.append(c("needs_approval", f"needs_approval:{s['id']}", s["since"],
                             f"{s['project']} needs your approval", "Claude Code is waiting on a permission prompt."))
            elif s["state"] == "waiting" and now - s["since"] >= 60 and (
                    f"session_waiting:{s['id']}" in live_keys or now - s["since"] <= 600):
                # News only when fresh: a turn that ended long before the tracker started isn't toasted.
                out.append(c("session_waiting", f"session_waiting:{s['id']}", s["since"],
                             f"{s['project']} is waiting for you", "Claude finished its turn."))
        ex = self.extra
        if isinstance(ex, dict) and ex.get("enabled") and isinstance(ex.get("pct"), (int, float)):
            if ex["pct"] >= 80 or ("overage_high" in live_keys and ex["pct"] >= 75):
                body = (f"{forecast.fmt_money(ex.get('used'), ex.get('currency'))} of "
                        f"{forecast.fmt_money(ex.get('limit'), ex.get('currency'))} used."
                        if ex.get("limit") else "Close to your extra usage cap.")
                out.append(c("overage_high", "overage_high", None, f"Extra usage at {ex['pct']:.0f}%", body))
        svc = status.service_view(self.status_raw, self.cfg.get("status_components"))
        if svc["indicator"] in ("minor", "major", "critical"):
            body = ("Affected: " + ", ".join(svc["affected"][:4]) + ".") if svc["affected"] else svc["url"]
            out.append(c("incident", "incident", svc["indicator"], f"Claude status: {svc['description'] or svc['indicator']}", body))
        return out

    def recompute(self) -> None:
        """Rebuild state from the inputs; bump rev and notify only if the content changed.
        Call with the lock held (or from a collector's locked section)."""
        with self.lock:
            now = time.time()
            sess = self.sessions.build(now, self.sl_sessions, int(self.cfg.get("sessions_top_n", 12)))
            limits, scope = self._limits(now, sess)
            source = self._source(now)
            acct = usage.read_account() or {}
            team_view = self._team_view(acct, limits, now)
            decision = self._decision(limits, scope, source, now, (team_view or {}).get("pool"))
            if team_view and team_view["pool"]:
                for p in team_view["pool"]:
                    if p["is_me"]:
                        p["level"] = decision["level"]
            mute = self.book.mute_state(now, self.cfg.get("quiet_hours"))
            new = self.book.evaluate(self._conditions(limits, scope, sess, source, now), now,
                                     bool(mute["until"]), self.cfg.get("alerts") or {})
            if new:
                self._dirty = True
            ov = self.cfg.get("overlay") or {}
            upd = self.update_info
            st = {
                "schema": 2, "rev": self.rev, "generated_at": int(now),
                "source": source,
                "account": {"email": acct.get("email"), "name": acct.get("name"), "org": acct.get("org"),
                            "plan": usage.read_plan() or None},
                "decision": decision,
                "limits": limits,
                "overage": copy.deepcopy(self.extra) if isinstance(self.extra, dict) else None,
                "sessions": sess,
                "service": status.service_view(self.status_raw, self.cfg.get("status_components")),
                "team": team_view,
                "alerts": self.book.view(),
                "mute": mute,
                "app": {
                    "version": __version__,
                    "update": ({"available": True, "version": upd["latest"], "url": upd["url"]}
                               if upd and upd.get("update") else None),
                    "remote": {"enabled": bool(self.cfg.get("remote_enabled")), "paired": remote.load_remote_identity() is not None,
                               "relay_url": self.cfg.get("remote_relay_url") or None, "last_sync": self.remote.last_sync},
                    "overlay": {"shown": bool(ov.get("shown")), "pinned": bool(ov.get("pinned"))},
                    "accent": dict(self.platform["accent"]),
                    "theme": self.platform["theme"],
                    "clock": self.platform.get("clock", "system"),
                    "statusline": dict(self.claude_integration["statusline"]),
                    "hook": dict(self.claude_integration["hook"]),
                },
            }
            h = _h({**st, "rev": 0, "generated_at": 0, "source": {**source, "age_s": 0}})
            if h == self._hash:
                return
            self._hash = h
            self.rev += 1
            st["rev"] = self.rev
            self.state = st
            for a in new:
                self._deliveries.put(a)
            self.changed.notify_all()
            if self.listeners:
                self._notify_q.put(st)       # listeners (the tray) run on their own thread, never under the lock

    def _listen_loop(self) -> None:
        while True:
            st = self._notify_q.get()
            if st is None:
                return
            while not self._notify_q.empty():  # only the newest state matters
                nxt = self._notify_q.get_nowait()
                if nxt is None:
                    return
                st = nxt
            for fn in list(self.listeners):
                try:
                    fn(st)
                except Exception:
                    io.log("state listener failed:\n" + traceback.format_exc())

    def _team_view(self, acct, limits, now):
        if not pool.configured():
            return None
        info = self.team_info or {"signed_in": False, "email": None, "role": None, "team": None}
        rows = team.pool_view(self.team_merged, acct.get("email"), None, now) if info["signed_in"] else []
        return {**info, "device_ok": self.team_sync.device_ok if info["signed_in"] else None, "pool": rows}

    # ------------------------------------------------------------------ delivery
    def _deliver_loop(self) -> None:
        from ctu import notify
        while True:
            a = self._deliveries.get()
            if a is None:
                return
            try:
                io.log(f"alert {a['kind']}: {a['title']} — {a['body']}")
                notify.toast(a["title"], a["body"], tag=a["kind"])
                if (a["kind"] in alerts.PHONE_PUSH and self.cfg.get("phone_push", True)
                        and remote.RemoteSync.enabled(self.cfg)):
                    self.remote.push(self.cfg, a["title"], a["body"], a["kind"])
            except Exception:
                io.log("alert delivery failed:\n" + traceback.format_exc())

    # ------------------------------------------------------------------ reads
    def snapshot(self) -> dict:
        """The current state with generated_at / age_s refreshed (a shallow copy)."""
        with self.lock:
            st = dict(self.state)
        now = time.time()
        st["generated_at"] = int(now)
        src = dict(st.get("source") or {})
        if src.get("updated_at"):
            src["age_s"] = int(now - src["updated_at"])
        st["source"] = src
        return st

    def wait_change(self, last_rev: int, timeout: float):
        """Block until rev != last_rev (-> new rev) or timeout / stop (-> None)."""
        with self.changed:
            self.changed.wait_for(lambda: self.rev != last_rev or self.stopping, timeout)
            if self.stopping or self.rev == last_rev:
                return None
            return self.rev

    def history_view(self, window: str) -> list:
        """GET /api/v2/history: [[t, used_pct], ...] since the window's last reset."""
        lid = window if window in forecast.WINDOW_S else "five_hour"
        with self.lock:
            r = self.readings.get(lid)
        resets = r["resets_at"] if r else None
        if not resets or resets <= time.time():
            return []
        return self.history.since(lid, resets - forecast.WINDOW_S[lid] - forecast.SAME_WINDOW_S)

    def alltime_view(self, rng: str) -> dict:
        rng = rng if rng in alltime.RANGES else "7d"
        now = time.time()
        days = alltime.RANGES[rng]
        start = now - days * 86400 if days else 0.0
        return alltime.view(self.alltime.cache, now, rng, int(self.cfg.get("alltime_top_n", 8)),
                            usage=self.history.usage(start, now))

    def ledger_view(self, month: str) -> dict:
        """GET /api/v2/team/ledger: UI-ready rows and per-currency totals."""
        info = self.team_info or {}
        if not info.get("signed_in"):
            return {"ok": False, "error": "signed_out"}
        return team.ledger_view(month, self.cfg.get("team_tz"), (usage.read_account() or {}).get("email"))

    def _alltime_v1(self, now: float):
        rev, data = self._alltime_compact
        if rev != self.alltime.rev or data is None:
            data = alltime.compact(self.alltime.cache, now)
            self._alltime_compact = (self.alltime.rev, data)
        return data

    def team_v1(self) -> dict:
        info = self.team_info or {"signed_in": False, "email": None, "role": None, "team": None}
        signed = info["signed_in"]
        return {"in_team": signed, "role": info["role"], "email": info["email"], "team": info["team"],
                "tz": self.cfg.get("team_tz"), "last_ok": self.team_sync.last_ok,
                "device_ok": self.team_sync.device_ok if signed else None}

    def v1_snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        st = self.snapshot()
        with self.lock:
            info = self.team_info or {}
            team_ov = team.team_overview_compact(self.team_merged, info.get("role")) if info.get("signed_in") else None
            remote_info = {"enabled": bool(self.cfg.get("remote_enabled")), "relay_url": self.cfg.get("remote_relay_url", ""),
                           "available": remote.remote_available(), "paired": remote.load_remote_identity() is not None,
                           "last_sync_ok": self.remote.last_ok}
            return v1.snapshot(st, now=now, history=self.history.v1(),
                               sessions_v1=sessions.v1_rows(st.get("sessions") or [], now),
                               status_raw=self.status_raw, alltime_compact=self._alltime_v1(now),
                               team_v1=self.team_v1(), team_overview=team_ov, cfg=self.cfg, remote_info=remote_info)

    # ------------------------------------------------------------------ hooks
    def on_hook(self, payload: dict) -> bool:
        if not isinstance(payload, dict):
            return False
        with self.lock:
            changed = self.sessions.on_hook(payload, time.time())
            if changed:
                self.recompute()
        if payload.get("hook_event_name") in ("Stop", "UserPromptSubmit"):
            self.scheduler.kick("tail")
        return changed

    # ------------------------------------------------------------------ settings
    def settings(self) -> dict:
        """GET /api/v2/settings: the grouped settings object."""
        with self.lock:
            comps = [c.get("name") for c in (self.status_raw or {}).get("components") or [] if c.get("name")]
            return config.settings_view(self.cfg, version=__version__, components=comps,
                                        statusline_installed=self.claude_integration["statusline"]["installed"])

    def patch_settings(self, patch) -> tuple[bool, dict]:
        """PATCH /api/v2/settings: all-or-nothing. -> (ok, the full settings object | errors)."""
        clean, acts, errors = config.api_patch(patch)

        def fail(errs):
            path, msg = next(iter(errs.items()))
            return False, {"ok": False, "error": f"{path}: {msg}" if path else msg, "errors": errs}
        if errors:
            return fail(errors)
        if "session_hook" in clean and clean["session_hook"] != self.cfg.get("session_hook"):
            ok = hooks.install_session_hook() if clean["session_hook"] else hooks.remove_session_hook()
            if not ok:
                return fail({"data.session_hook": "~/.claude/settings.json couldn't be updated (is it valid JSON?)"})
            with self.lock:
                self.claude_integration["hook"] = {"installed": bool(clean["session_hook"])}
        if acts.get("statusline_installed") is True:
            res = self._install_integration()
            if not res["ok"]:
                return fail({"data.statusline_installed": res["message"]})
        elif acts.get("statusline_installed") is False:
            if not hooks.remove_statusline():
                return fail({"data.statusline_installed": "~/.claude/settings.json couldn't be updated"})
            self._refresh_integration()
        with self.lock:
            self.cfg = config.merge(self.cfg, clean)
            config.save(self.cfg)
            if clean.get("remote_enabled") is True and remote.remote_available():
                remote.load_remote_identity(create=True)
            self.recompute()
        if "overlay" in clean and self.host:
            self.host.apply_overlay(self.cfg.get("overlay") or {})
        if "remote_enabled" in clean or "remote_relay_url" in clean:
            self.remote.force()
            self.scheduler.kick("remote")
        return True, self.settings()

    def _refresh_integration(self) -> None:
        integ = {"statusline": {k: v for k, v in hooks.statusline_status().items() if k != "readable"},
                 "hook": {"installed": hooks.hook_installed()}}
        with self.lock:
            self.claude_integration = integ
            self.recompute()

    def _install_integration(self) -> dict:
        """The statusline (live limits) plus the session hooks (waiting / needs approval). An existing
        statusLine that isn't ours is reported and left alone; the hooks are installed regardless."""
        res = hooks.install_statusline()
        if res.get("error") != "unreadable" and hooks.install_session_hook():
            with self.lock:
                if not self.cfg.get("session_hook"):
                    self.cfg["session_hook"] = True
                    config.save(self.cfg)
            res["hook"] = True
        self._refresh_integration()
        return res

    def save_internal(self, **kw) -> None:
        """Persist internal keys (overlay position / size)."""
        with self.lock:
            self.cfg.update(kw)
            config.save(self.cfg)

    # ------------------------------------------------------------------ actions
    ACTIONS = ("refresh", "login", "update", "check_update", "mute", "overlay", "overlay_resize", "flyout_resize", "flyout_close",
               "open_dashboard", "remote_pair", "remote_rotate", "remote_unpair", "remote_sync", "team_login_start",
               "team_login_verify", "team_logout", "team_claim_device", "team_connector_token",
               "install_statusline", "remove_statusline", "open_logs")

    def action(self, name: str, body: dict | None) -> tuple[int, dict]:
        """POST /api/v2/actions/<name> -> (http status, json)."""
        body = body if isinstance(body, dict) else {}
        fn = getattr(self, f"_a_{name}", None) if name in self.ACTIONS else None
        if fn is None:
            return 404, {"ok": False, "error": "unknown action"}
        try:
            return 200, fn(body)
        except Exception as exc:
            io.log(f"action {name} failed:\n" + traceback.format_exc())
            return 500, {"ok": False, "error": str(exc)}

    def refresh(self) -> None:
        with self.lock:
            self.api_force = True
            self.sl_sig = None
        for j in ("statusline", "usage", "tail", "status", "team"):
            self.scheduler.kick(j)
        self.remote.force()
        self.scheduler.kick("remote")

    def _a_refresh(self, b):
        self.refresh()
        return {"ok": True}

    def _a_login(self, b):
        from ctu.claude_cli import launch_login
        return launch_login(b.get("email") or None, bool(b.get("terminal")))

    def _a_check_update(self, b):
        res = update.check()
        with self.lock:
            self.update_checked_at = time.time()
            if res["latest"]:
                self.update_info = res
            self._dirty = True
            self.recompute()
        return {"ok": True, **res, "message": (f"Version {res['latest']} is available." if res["update"] else
                                               f"You're on the latest version ({__version__})." if res["latest"]
                                               else "Couldn't check for updates (offline?).")}

    def _a_update(self, b):
        info = self.update_info or update.check()
        if not info.get("update"):
            return {"ok": False, "message": f"You're on the latest version ({__version__})."}
        return update.self_update(info.get("latest"), info.get("url") if str(info.get("url", "")).endswith(".exe") else None,
                                  self.quit)

    def _a_mute(self, b):
        how = b.get("for")
        if how not in ("refill", "hour", "off"):
            return {"ok": False, "error": "for must be refill, hour or off"}
        with self.lock:
            d = self.state.get("decision") or {}
            refill = d.get("resume_at")
            if not refill and d.get("binding"):
                refill = next((l["resets_at"] for l in self.state.get("limits", []) if l["id"] == d["binding"]), None)
            self.book.set_mute(how, time.time(), refill)
            self._dirty = True
            self.recompute()
            return {"ok": True, "mute": self.state["mute"]}

    def _a_overlay(self, b):
        patch = {k: bool(b[src]) for src, k in (("show", "shown"), ("pin", "pinned"), ("click_through", "click_through"))
                 if src in b}
        ok, res = self.patch_settings({"overlay": patch})
        return {"ok": True, "overlay": res["overlay"]} if ok else res

    def _a_overlay_resize(self, b):
        try:
            w, h = int(b.get("w")), int(b.get("h"))
        except Exception:
            return {"ok": False, "error": "w and h must be integers"}
        w, h = max(120, min(800, w)), max(24, min(720, h))
        self.save_internal(overlay_size=[w, h])
        if self.host:
            self.host.resize_overlay(w, h)
        return {"ok": True, "w": w, "h": h}

    def _a_flyout_resize(self, b):
        try:
            w, h = int(b.get("w")), int(b.get("h"))
        except Exception:
            return {"ok": False, "error": "w and h must be integers"}
        w, h = max(200, min(480, w)), max(120, min(600, h))
        if self.host:
            self.host.resize_flyout(w, h)
        return {"ok": True, "w": w, "h": h}

    def _a_flyout_close(self, b):
        if self.host:
            self.host.close_flyout()
        return {"ok": True}

    def _a_open_dashboard(self, b):
        view = "".join(c for c in str(b.get("view") or "now") if c.isalnum() or c in "-_")[:32] or "now"
        self.open_dashboard(view)
        return {"ok": True}

    def open_dashboard(self, view: str = "now") -> None:
        if self.host:
            self.host.open_dashboard(view)
        elif self.port:
            webbrowser.open(f"http://127.0.0.1:{self.port}/#/{view}")

    def _a_remote_pair(self, b):
        if not remote.remote_available():
            return {"ok": False, "error": "PyNaCl isn't installed, so phone sync is unavailable."}
        ident = remote.load_remote_identity(create=True)
        if not self.cfg.get("remote_enabled"):
            self.patch_settings({"remote": {"enabled": True}})
        else:
            with self.lock:
                self.recompute()
        return {"ok": bool(ident), "qr_svg": remote.remote_pair_qr_svg(self.cfg), "qr": "/api/v2/remote/qr",
                "url": self.cfg.get("remote_relay_url")}

    def _a_remote_rotate(self, b):
        remote.rotate_remote_identity()
        self.remote.force()
        with self.lock:
            self.recompute()
        return {"ok": True, "qr_svg": remote.remote_pair_qr_svg(self.cfg), "qr": "/api/v2/remote/qr"}

    def _a_remote_unpair(self, b):
        remote.unpair_remote()
        self.patch_settings({"remote": {"enabled": False}})
        return {"ok": True}

    def _a_remote_sync(self, b):
        self.remote.force()
        self.scheduler.kick("remote")
        return {"ok": True}

    def _team_changed(self) -> None:
        self.team_sync.session_changed()
        self.team_pool_at = 0.0
        self.scheduler.kick("team")

    def _a_team_login_start(self, b):
        email = str(b.get("email") or "").strip().lower()
        if not email or "@" not in email:
            return {"ok": False, "error": "email required"}
        ok, resp = pool.sign_in_start(email)
        return {"ok": True} if ok else {"ok": False, "error": f"could not send the code: {resp}"}

    def _a_team_login_verify(self, b):
        email = str(b.get("email") or "").strip().lower()
        code = str(b.get("code") or "").strip()
        username = str(b.get("username") or "").strip()
        if not (email and code):
            return {"ok": False, "error": "email and code required"}
        ok, _res = pool.sign_in_verify_code(email, code)
        if not ok:
            return {"ok": False, "error": "invalid or expired code"}
        if not (username and pool.set_username(username)):
            pool.claim_device()               # signing in here = using the pool login here
        self._team_changed()
        return {"ok": True, "team": pool.team(), "role": "admin" if pool.is_admin() else "member"}

    def _a_team_logout(self, b):
        pool.sign_out()
        self._team_changed()
        return {"ok": True}

    def _a_team_claim_device(self, b):
        ok = pool.claim_device()
        self._team_changed()
        return {"ok": True} if ok else {"ok": False, "error": "could not claim this device"}

    def _a_team_connector_token(self, b):
        if not pool.is_admin():
            return {"ok": False, "error": "admin only"}
        try:
            days = max(1, min(365, int(b.get("days") or 30)))
        except Exception:
            days = 30
        tok, err = pool.mint_connector_token(days)
        return {"ok": True, "token": tok} if tok else {"ok": False, "error": err or "mint failed"}

    def _a_install_statusline(self, b):
        return self._install_integration()

    def _a_remove_statusline(self, b):
        ok = hooks.remove_statusline()
        self._refresh_integration()
        return {"ok": ok}

    def _a_open_logs(self, b):
        winplat.open_path(paths.LOG_PATH)
        return {"ok": True, "path": str(paths.LOG_PATH)}

    def quit(self) -> None:
        if self.host:
            self.host.quit()
