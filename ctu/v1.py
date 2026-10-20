"""The <= 0.3 snapshot (GET /api/usage, the relay payload), built from v2 state.

Consumers: mcp/server.mjs (usage, verdict, context, extra) and the Android app (Snapshot.kt).
Added for v2-aware readers: top-level `decision` and `source`, and per window `pace`,
`elapsed_pct` and `level` (now the v2 level). `resets_at` stays in epoch ms; `pace.eta_100` and
`decision.resume_at` are epoch seconds, as in v2. `updated_at` is the build time (liveness)."""
from __future__ import annotations

USAGE_BANDS = [(100, "#c8372d", "max"), (90, "#dd5f42", "crit"), (80, "#e2893f", "high"),
               (60, "#cda24e", "warn"), (0, "#5e9e72", "ok")]
VERDICT = {"ok": ("ok", "#5e9e72"), "watch": ("caution", "#cda24e"),
           "will_hit": ("stop", "#dd5f42"), "limited": ("over", "#c8372d")}


def usage_style(pct: float) -> tuple[str, str]:
    """(hex colour, band) for a 0-100 utilisation — the phone's gauge colours."""
    for threshold, hexv, band in USAGE_BANDS:
        if pct >= threshold:
            return hexv, band
    return "#5e9e72", "ok"


def windows(state: dict, now: float) -> list:
    out = []
    for lim in state.get("limits") or []:
        p = lim["pace"]
        eta = p.get("eta_100")
        out.append({
            "key": lim["id"], "label": "5h" if lim["id"] == "five_hour" else lim["label"],
            "pct": round(lim["used_pct"], 1),
            "resets_at": int(lim["resets_at"] * 1000) if lim["resets_at"] else None,
            "color": usage_style(lim["used_pct"])[0], "level": lim["level"],
            "rate_per_hour": p.get("rate_pct_h"),
            "eta_seconds": int(eta - now) if eta else None,
            "elapsed_pct": lim.get("elapsed_pct"), "pace": dict(p),
        })
    return out


def verdict(decision: dict) -> dict:
    level, color = VERDICT.get(decision.get("level"), VERDICT["ok"])
    return {"level": level, "color": color, "text": decision.get("title") or ""}


def context(sessions: list):
    """The newest session's context, in the statusline's field names."""
    live = [s for s in sessions if s["context"]["tokens"]]
    if not live:
        return None, None
    s = max(live, key=lambda s: (s["active"], s["since"]))
    c = s["context"]
    return {"used_percentage": c["pct"], "total_input_tokens": c["tokens"], "context_window_size": c["window"]}, s["cwd"]


def snapshot(state: dict, *, now: float, history: dict, sessions_v1: list, status_raw, alltime_compact,
             team_v1, team_overview, cfg: dict, remote_info: dict) -> dict:
    src = state["source"]
    ctx, cwd = context(state.get("sessions") or [])
    upd = (state.get("app") or {}).get("update")
    acct = state.get("account") or {}
    return {
        "ok": bool(state.get("limits")) and not src["stale"],
        "error": src.get("error"),
        "token_state": src.get("auth"),
        "updated_at": int(now),               # when this live desktop built the snapshot (phone: offline after 120 s)
        "subscription": acct.get("plan") or "",
        "account": {k: acct.get(k) for k in ("email", "name", "org")},
        "windows": windows(state, now),
        "extra": state.get("overage"),
        "history": history,
        "verdict": verdict(state["decision"]),
        "decision": state["decision"],
        "source": {k: src.get(k) for k in ("kind", "updated_at", "age_s", "stale", "auth", "error")},
        "sessions": sessions_v1,
        "context": ctx,
        "cwd": cwd,
        "alltime": alltime_compact,
        "update": {"available": upd.get("version") if upd else None, "url": upd.get("url") if upd else None},
        "status": status_raw,
        "remote": remote_info,
        "team": team_v1,
        "team_overview": team_overview,
        # Read by the phone (status chip filter, mirror toggle); not UI layout.
        "ui": {"status_components": list(cfg.get("status_components") or []),
               "remote_transcript": bool(cfg.get("remote_transcript"))},
    }
