"""Supabase-hybrid account pool — stdlib client (no supabase-py).

Talks to GoTrue (`/auth/v1/*`) and PostgREST (`/rest/v1/*`) with urllib only, so the process
that holds the Claude OAuth token pulls in NO extra dependency tree (closes supply-chain
risk #8). The session (access + refresh) is persisted in the OS keychain via `keyring`; the
access token is refreshed ahead of expiry with ROTATING refresh tokens — an out-of-window
reuse revokes the family, so we always store and use the latest one, and treat an
unexpected refresh failure as a forced re-login.

Design: docs/SUPABASE-MIGRATION.md  ·  Schema/RLS: supabase/migrations/0001_pool.sql
Boundary verified live 2026-07-08 against project sxciunvkygtehhztfjjo: signup stamps
app_metadata.team/role into the JWT; authenticated reads are RLS-scoped; the connector RPCs
are service_role-only (403 to authenticated).

Deps: `keyring` (session storage). Publishable key ships in the binary — safe by design
(RLS is the guard; `anon` is granted nothing).
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

# Public config — real values baked in (publishable key is safe to ship); env overrides for dev.
SUPABASE_URL = os.environ.get(
    "SUPABASE_URL", "https://sxciunvkygtehhztfjjo.supabase.co").rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.environ.get(
    "SUPABASE_PUBLISHABLE_KEY", "sb_publishable_IRxV6mJEorXS3UzPWC21yA_4cMoe2IJ")

_KEYRING_SERVICE = "ClaudeUsageTracker.supabase"
_SESSION_KEY = "session"
_DEVICE_PATH = Path(os.path.expanduser("~")) / ".claude-usage-tracker" / "device_id"
_TIMEOUT = 10
_REFRESH_SKEW = 120  # refresh this many seconds before the access token expires


def configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY)


# --- keyring-backed session store (lazy import so the app runs even if keyring is missing) ---

def _keyring():
    import keyring  # lazy
    return keyring


def _load_session() -> dict | None:
    try:
        raw = _keyring().get_password(_KEYRING_SERVICE, _SESSION_KEY)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _save_session(sess: dict) -> None:
    _keyring().set_password(_KEYRING_SERVICE, _SESSION_KEY, json.dumps(sess))


def _clear_session() -> None:
    try:
        _keyring().delete_password(_KEYRING_SERVICE, _SESSION_KEY)
    except Exception:
        pass


# --- HTTP ---------------------------------------------------------------------------------

def _http(method: str, path: str, *, body=None, token: str | None = None, prefer: str | None = None):
    """Returns (status_or_None, parsed_json_or_text). token=None uses the publishable key
    (auth endpoints); pass a user access token for PostgREST/logout."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(SUPABASE_URL + path, data=data, method=method)
    req.add_header("apikey", SUPABASE_PUBLISHABLE_KEY)
    req.add_header("Authorization", "Bearer " + (token or SUPABASE_PUBLISHABLE_KEY))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if prefer:
        req.add_header("Prefer", prefer)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            txt = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(txt) if txt.strip() else None)
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, txt
    except Exception as exc:
        return None, str(exc)


def _decode_jwt(tok: str) -> dict:
    p = tok.split(".")[1]
    p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p))


# --- auth (email OTP → session; team/role stamped server-side at signup) -------------------

def sign_in_start(email: str, captcha_token: str | None = None):
    """Send the sign-in email (6-digit code once custom SMTP + {{ .Token }} template are set;
    a magic link on the default template). Returns (ok, resp)."""
    body: dict = {"email": email, "create_user": True}
    if captcha_token:
        body["gotrue_meta_security"] = {"captcha_token": captcha_token}
    st, resp = _http("POST", "/auth/v1/otp", body=body)
    return st == 200, resp


def _store(resp) -> dict | None:
    if not isinstance(resp, dict) or not resp.get("access_token"):
        return None
    sess = {
        "access_token": resp["access_token"],
        "refresh_token": resp.get("refresh_token"),
        "expires_at": resp.get("expires_at") or int(time.time()) + int(resp.get("expires_in", 3600)),
    }
    _save_session(sess)
    return sess


def sign_in_verify_code(email: str, code: str):
    """Verify the 6-digit code → persisted session. Returns (ok, claims_or_error)."""
    st, resp = _http("POST", "/auth/v1/verify", body={"type": "email", "email": email, "token": code})
    if st == 200 and _store(resp):
        return True, claims()
    return False, resp


def sign_in_verify_url(link: str):
    """Complete a magic-link login from a pasted URL — used before custom SMTP delivers codes."""
    q = parse_qs(urlparse(link).query)
    body = {"type": (q.get("type") or ["magiclink"])[0]}
    if q.get("token_hash"):
        body["token_hash"] = q["token_hash"][0]
    elif q.get("token"):
        body["token"] = q["token"][0]
    else:
        return False, "no token in url"
    st, resp = _http("POST", "/auth/v1/verify", body=body)
    if st == 200 and _store(resp):
        return True, claims()
    return False, resp


def sign_out() -> None:
    sess = _load_session()
    if sess and sess.get("access_token"):
        _http("POST", "/auth/v1/logout", token=sess["access_token"])
    _clear_session()


def _refresh(sess: dict) -> dict | None:
    rt = sess.get("refresh_token")
    if not rt:
        return None
    st, resp = _http("POST", "/auth/v1/token?grant_type=refresh_token", body={"refresh_token": rt})
    if st == 200:
        return _store(resp)
    if st in (400, 401):
        _clear_session()  # family revoked / token invalid → force re-login
    return None


def access_token() -> str | None:
    """A valid access token, refreshed ahead of expiry. None → signed out / re-login needed."""
    sess = _load_session()
    if not sess:
        return None
    if (sess.get("expires_at") or 0) <= time.time() + _REFRESH_SKEW:
        sess = _refresh(sess)
        if not sess:
            return None
    return sess.get("access_token")


def signed_in() -> bool:
    return access_token() is not None


def has_session() -> bool:
    """Local-only: a stored session exists (may be expired). Cheap, no network -- for the
    frequent 'team enabled?' poll check. sync() then calls access_token(), which refreshes/clears."""
    return _load_session() is not None


def claims() -> dict | None:
    tok = access_token()
    return _decode_jwt(tok) if tok else None


def _claim(path: tuple):
    c = claims() or {}
    for k in path:
        c = (c or {}).get(k) if isinstance(c, dict) else None
    return c


def uid() -> str | None:
    return _claim(("sub",))


def email() -> str | None:
    return _claim(("email",))


def team() -> str | None:
    return _claim(("app_metadata", "team"))


def is_admin() -> bool:
    return _claim(("app_metadata", "role")) == "admin"


# --- device id (soft device-lock + `device` PK component) ----------------------------------

def device_id() -> str:
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


def set_username(username: str) -> bool:
    """Upsert the profile (user_id/email/team/is_admin are trigger-forced; only username +
    device are client-supplied). Call once after first sign-in."""
    tok = access_token()
    if not tok:
        return False
    body = {"username": username, "active_device_id": device_id(),
            "device_claimed_at": datetime.now(timezone.utc).isoformat()}
    st, _ = _http("POST", "/rest/v1/profiles?on_conflict=user_id", body=body, token=tok,
                  prefer="resolution=merge-duplicates,return=minimal")
    return st in (200, 201, 204)


def device_matches() -> bool:
    """Soft device-lock self-check (call each poll tick). NOT the isolation boundary — RLS is.
    True when no profile yet or the claimed device is this one."""
    tok, u = access_token(), uid()
    if not tok or not u:
        return True
    st, resp = _http("GET", f"/rest/v1/profiles?select=active_device_id&user_id=eq.{u}", token=tok)
    if st != 200 or not isinstance(resp, list) or not resp:
        return True
    return resp[0].get("active_device_id") in (None, device_id())


# --- push (~10s upsert) --------------------------------------------------------------------

def push(row: dict, dev: dict, account: dict, local_date: str) -> bool:
    """Upsert the pooled account + today's usage row as the logged-in user (RLS-scoped).
    `row` is build_team_report(snap, dev, account). team/writer_uid/by_name are NOT sent —
    the usage_force trigger stamps them. Returns True on success."""
    tok = access_token()
    if not tok:
        return False
    acct = account["acct"]
    e = row.get("extra") or {}
    st1, _ = _http("POST", "/rest/v1/accounts?on_conflict=team,acct", token=tok,
                   prefer="resolution=merge-duplicates,return=minimal",
                   body={"acct": acct, "display_name": row.get("name"), "org": account.get("org")})
    st2, _ = _http("POST", "/rest/v1/usage?on_conflict=team,acct,date", token=tok,
                   prefer="resolution=merge-duplicates,return=minimal",
                   body={
                       "acct": acct, "device": dev.get("did"), "date": local_date,
                       "display_name": row.get("name"),
                       "fh_pct": row.get("fh_pct"), "sd_pct": row.get("sd_pct"),
                       "fh_resets_at": row.get("fh_resets_at"), "sd_resets_at": row.get("sd_resets_at"),
                       "extra_enabled": e.get("enabled") if row.get("extra") else None,
                       "extra_used": e.get("used"), "extra_limit": e.get("limit"),
                       "extra_currency": e.get("currency"), "extra_pct": e.get("pct"),
                       "tok_month": dev.get("tok_month"), "src": "push", "ts": row.get("ts"),
                   })
    return st1 in (200, 201, 204) and st2 in (200, 201, 204)


# --- read (reshape Supabase rows into the shapes the existing merge fns expect) ------------

def _get(path: str):
    tok = access_token()
    if not tok:
        return None
    st, resp = _http("GET", path, token=tok)
    return resp if st == 200 and isinstance(resp, list) else None


def _row_from_db(r: dict) -> dict:
    """usage/finals row → the client row shape team_overview_merge/team_ledger_computed parse."""
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


def read_overview(tz: str, today: str, yesterday: str) -> dict:
    """Same shape the relay /overview did — feed straight into team_overview_merge. RLS scopes
    rows to the caller's team, so no team filter is sent."""
    accts = {a["acct"]: a for a in (_get("/rest/v1/accounts?select=acct,display_name,org") or [])}
    rows = _get(f"/rest/v1/usage?select=*&date=in.({today},{yesterday})") or []
    by_date: dict = {today: {}, yesterday: {}}
    for r in rows:
        by_date.setdefault(r["date"], {}).setdefault(r["acct"], {})[r["device"]] = _row_from_db(r)

    def newest(devmap):
        best = None
        for r in (devmap or {}).values():
            if best is None or (r.get("ts") or 0) > (best.get("ts") or 0):
                best = r
        return best

    accounts = []
    for acct, a in accts.items():
        tdev, ydev = by_date[today].get(acct, {}), by_date[yesterday].get(acct, {})
        top = newest(tdev)
        last = newest(tdev) or newest(ydev)
        accounts.append({
            "acct": acct, "name": a.get("display_name") or acct,
            "account": top or newest(ydev),
            "account_is_today": bool(top),
            "last_used": {"ts": last["ts"], "device": last["device"], "by": last.get("by_name")} if last else None,
            "devices": [{"did": d, **row} for d, row in tdev.items()],
            "escrow": {"present": False},  # escrow dropped in the Supabase model
        })
    return {"tz": tz, "today": today, "accounts": accounts}


def read_ledger(month: str) -> dict:
    """Relay /ledger shape — feed into team_ledger_computed / member_month_tokens."""
    names = {a["acct"]: (a.get("display_name") or a["acct"])
             for a in (_get("/rest/v1/accounts?select=acct,display_name") or [])}
    rows = _get(f"/rest/v1/usage?select=*&date=like.{month}-*") or []
    finals_rows = _get(f"/rest/v1/finals?select=*&month=eq.{month}") or []
    days: dict = {}
    for r in rows:
        days.setdefault(r["date"], {}).setdefault(r["acct"], {})[r["device"]] = _row_from_db(r)
    finals = {f["acct"]: _row_from_db(f) for f in finals_rows}
    return {"month": month, "accounts": names, "days": days, "finals": finals}


# --- connector token (admin mints; claude.ai connector consent pastes it) ------------------

def mint_connector_token(days: int = 30):
    """Admin-only: mint a random connector token, store its sha256 → team (server-forced),
    return the plaintext ONCE for the claude.ai connector. Returns (token, error)."""
    import hashlib
    import secrets
    tok = access_token()
    if not tok:
        return None, "not signed in"
    if not is_admin():
        return None, "admin only"
    plain = secrets.token_urlsafe(32)
    expires = datetime.fromtimestamp(time.time() + days * 86400, tz=timezone.utc).isoformat()
    st, resp = _http("POST", "/rest/v1/connector_tokens", token=tok,
                     prefer="return=minimal",
                     body={"token_hash": hashlib.sha256(plain.encode()).hexdigest(),
                           "expires_at": expires})
    if st in (200, 201, 204):
        return plain, None
    return None, f"mint failed (HTTP {st}): {resp}"
