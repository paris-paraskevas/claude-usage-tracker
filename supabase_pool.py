"""Supabase-hybrid account pool — SCAFFOLDING (dormant, placeholder-driven).

Design: docs/SUPABASE-MIGRATION.md  ·  Schema/RLS: supabase/migrations/0001_pool.sql

Status: NOT wired into the app yet and NOT verified against a live Supabase project.
This module is inert unless SUPABASE_URL + SUPABASE_PUBLISHABLE_KEY are configured, and
its dependencies (`supabase`, `keyring`) are imported LAZILY so importing this file never
breaks the running tracker even when they aren't installed. Activation (retire the D1 team
path, add the launch auth gate, swap TeamSync.sync to push()/read_overview()) happens after
the Supabase project exists and the grant/isolation ship-gate probe passes.

To activate later:  pip install "supabase>=2" keyring   (add to pyproject — authoritative)

SPIKE before shipping (see the doc): supabase-py API surface (verify_otp / ClientOptions /
upsert kwargs), the exact session-storage interface, and the reshape column mapping.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

# Placeholder config — real values come from the user's Supabase project (never the secret key).
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")                       # https://<ref>.supabase.co
SUPABASE_PUBLISHABLE_KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")  # sb_publishable_… (public; RLS is the guard)

_KEYRING_SERVICE = "ClaudeUsageTracker.supabase"
_DEVICE_PATH = Path(os.path.expanduser("~")) / ".claude-usage-tracker" / "device_id"


def configured() -> bool:
    """True only when a real Supabase target is set — the whole module no-ops otherwise."""
    return bool(SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY)


# --- lazy dependency shims (kept out of module import so the app runs without the deps) ----

class _KeyringStorage:
    """Persist the Supabase session in the OS keychain (never plaintext). Duck-typed to
    supabase-py's SyncSupportedStorage (get/set/remove_item). SPIKE: confirm the exact base
    class + method names against the installed supabase_auth version and subclass it."""
    def __init__(self):
        import keyring  # lazy
        self._kr = keyring

    def get_item(self, key: str):
        return self._kr.get_password(_KEYRING_SERVICE, key)

    def set_item(self, key: str, value: str) -> None:
        self._kr.set_password(_KEYRING_SERVICE, key, value)

    def remove_item(self, key: str) -> None:
        try:
            self._kr.delete_password(_KEYRING_SERVICE, key)
        except Exception:
            pass


def client():
    """A keyring-backed Supabase client (auto-persists + refreshes the session). Raises if the
    deps aren't installed or config is missing — call only on the configured/activated path."""
    if not configured():
        raise RuntimeError("Supabase not configured (SUPABASE_URL / SUPABASE_PUBLISHABLE_KEY)")
    from supabase import create_client  # lazy
    from supabase.lib.client_options import ClientOptions  # SPIKE: verify import path per version
    return create_client(
        SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY,
        options=ClientOptions(storage=_KeyringStorage(), persist_session=True),
    )


def device_id() -> str:
    """Stable per-install device id (soft device-lock + the `device` PK component). Mirrors the
    old ensure_team_device() did minting; persisted locally."""
    try:
        _DEVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _DEVICE_PATH.exists():
            v = _DEVICE_PATH.read_text(encoding="utf-8").strip()
            if v:
                return v
        v = uuid.uuid4().hex[:16]
        _DEVICE_PATH.write_text(v, encoding="utf-8")
        return v
    except Exception:
        return uuid.uuid4().hex[:16]


# --- auth (email OTP → username → device claim) --------------------------------------------

def sign_in_start(sb, email: str, captcha_token: str | None = None) -> None:
    """Send the 6-digit OTP. team/role are stamped server-side at signup (app_metadata)."""
    opts = {"should_create_user": True}
    if captcha_token:
        opts["captcha_token"] = captcha_token
    sb.auth.sign_in_with_otp({"email": email, "options": opts})


def sign_in_verify(sb, email: str, code: str):
    """Verify the code → returns a session (access + refresh, persisted via keyring)."""
    return sb.auth.verify_otp({"email": email, "token": code, "type": "email"})


def set_username(sb, uid: str, username: str) -> None:
    """Upsert the profile. user_id/email/team/is_admin are all server-forced by a trigger;
    only username + device are client-supplied."""
    sb.table("profiles").upsert(
        {"username": username, "active_device_id": device_id(), "device_claimed_at": "now()"},
        on_conflict="user_id",
    ).execute()


def device_matches(sb, uid: str) -> bool:
    """Soft device-lock self-check (call each poll tick). NOT a security boundary — RLS is."""
    r = sb.table("profiles").select("active_device_id").eq("user_id", uid).single().execute()
    return not r.data or r.data.get("active_device_id") == device_id()


# --- push (~10s upsert) --------------------------------------------------------------------

def push(sb, row: dict, dev: dict, account: dict, local_date: str) -> None:
    """Upsert the pooled account + today's usage row as the logged-in user (RLS-scoped).
    `row` is build_team_report(snap, dev, account) — team/writer_uid/by_name are dropped from
    the payload (server-forced by the usage_force trigger). returning='minimal' cuts egress."""
    acct = account["acct"]
    e = row.get("extra") or {}
    sb.table("accounts").upsert(
        {"acct": acct, "display_name": row.get("name"), "org": account.get("org")},
        on_conflict="team,acct", returning="minimal",
    ).execute()
    sb.table("usage").upsert({
        "acct": acct, "device": dev.get("did"), "date": local_date, "display_name": row.get("name"),
        "fh_pct": row.get("fh_pct"), "sd_pct": row.get("sd_pct"),
        "fh_resets_at": row.get("fh_resets_at"), "sd_resets_at": row.get("sd_resets_at"),
        "extra_enabled": e.get("enabled") if row.get("extra") else None,
        "extra_used": e.get("used"), "extra_limit": e.get("limit"),
        "extra_currency": e.get("currency"), "extra_pct": e.get("pct"),
        "tok_month": dev.get("tok_month"), "src": "push", "ts": row.get("ts"),
    }, on_conflict="team,acct,device,date", returning="minimal").execute()


# --- read (reshape Supabase rows into the shapes the existing merge fns expect) -------------

def _row_from_db(r: dict) -> dict:
    """usage/finals row → the client row shape team_overview_merge/team_ledger_computed parse
    (mirrors relay rowFromDb; `device` fills both did+device)."""
    ee = r.get("extra_enabled")
    return {
        "name": r.get("display_name"),
        "fh_pct": r.get("fh_pct"), "sd_pct": r.get("sd_pct"),
        "fh_resets_at": r.get("fh_resets_at"), "sd_resets_at": r.get("sd_resets_at"),
        "extra": None if ee is None else {
            "enabled": bool(ee), "used": r.get("extra_used"), "limit": r.get("extra_limit"),
            "currency": r.get("extra_currency"), "pct": r.get("extra_pct"),
        },
        "did": r.get("device"), "device": r.get("device"), "by_name": r.get("by_name"),
        "tok_month": r.get("tok_month"), "ts": r.get("ts"), "src": r.get("src"),
    }


def read_overview(sb, tz: str, today: str, yesterday: str) -> dict:
    """Returns the same shape the relay /overview did — feed straight into team_overview_merge.
    RLS scopes rows to the caller's team, so no team filter is sent."""
    accts = {a["acct"]: a for a in
             (sb.table("accounts").select("acct,display_name,org").execute().data or [])}
    rows = (sb.table("usage").select("*").in_("date", [today, yesterday]).execute().data or [])
    by_date: dict = {today: {}, yesterday: {}}
    for r in rows:
        by_date.setdefault(r["date"], {}).setdefault(r["acct"], {})[r["device"]] = _row_from_db(r)

    def pick(devmap):  # cron 'account' row if present, else newest push (mirrors pickAccountRow)
        if not devmap:
            return None
        best = None
        for r in devmap.values():
            if best is None or (r.get("ts") or 0) > (best.get("ts") or 0):
                best = r
        return best

    def last_used(devmap):  # newest human push → {ts, device, by}
        best = None
        for did, r in (devmap or {}).items():
            if best is None or (r.get("ts") or 0) > (best.get("ts") or 0):
                best = r
        return {"ts": best["ts"], "device": best["device"], "by": best.get("by_name")} if best else None

    accounts = []
    for acct, a in accts.items():
        tdev, ydev = by_date[today].get(acct, {}), by_date[yesterday].get(acct, {})
        accounts.append({
            "acct": acct, "name": a.get("display_name") or acct,
            "account": pick(tdev) or pick(ydev),
            "account_is_today": bool(pick(tdev)),
            "last_used": last_used(tdev) or last_used(ydev),
            "devices": [{"did": d, **row} for d, row in tdev.items()],
            "escrow": {"present": False},  # escrow dropped in the Supabase model
        })
    return {"tz": tz, "today": today, "accounts": accounts}


def read_ledger(sb, month: str) -> dict:
    """Returns the relay /ledger shape — feed into team_ledger_computed / member_month_tokens."""
    names = {a["acct"]: (a.get("display_name") or a["acct"]) for a in
             (sb.table("accounts").select("acct,display_name").execute().data or [])}
    rows = (sb.table("usage").select("*").like("date", f"{month}-%").execute().data or [])
    finals_rows = (sb.table("finals").select("*").eq("month", month).execute().data or [])
    days: dict = {}
    for r in rows:
        days.setdefault(r["date"], {}).setdefault(r["acct"], {})[r["device"]] = _row_from_db(r)
    finals = {f["acct"]: _row_from_db(f) for f in finals_rows}
    return {"month": month, "accounts": names, "days": days, "finals": finals}
