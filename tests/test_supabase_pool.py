"""supabase_pool: serialised token refresh, in-memory session cache, error propagation."""
import json
import threading
import time

import pytest

import ctu.team as team
from ctu import usage
from ctu.team import pool as sp


class FakeKeyring:
    def __init__(self, sess=None):
        self.store = {} if sess is None else {(sp._KEYRING_SERVICE, sp._SESSION_KEY): json.dumps(sess)}
        self.gets = 0

    def get_password(self, svc, key):
        self.gets += 1
        return self.store.get((svc, key))

    def set_password(self, svc, key, val):
        self.store[(svc, key)] = val

    def delete_password(self, svc, key):
        self.store.pop((svc, key), None)


@pytest.fixture
def pool(monkeypatch):
    def install(sess):
        kr = FakeKeyring(sess)
        monkeypatch.setattr(sp, "_keyring", lambda: kr)
        monkeypatch.setattr(sp, "_session_cache", {"loaded": False, "sess": None})
        return kr
    return install


def test_session_is_cached_in_memory(pool):
    kr = pool({"access_token": "a1", "refresh_token": "r1", "expires_at": time.time() + 3600})
    for _ in range(10):
        assert sp.access_token() == "a1"
    assert kr.gets == 1                                     # one keychain read, not one per call


def test_concurrent_refresh_uses_rotating_token_once(pool, monkeypatch):
    kr = pool({"access_token": "old", "refresh_token": "r1", "expires_at": 0})
    calls = []

    def fake_http(method, path, *, body=None, token=None, prefer=None):
        assert "grant_type=refresh_token" in path
        calls.append(body["refresh_token"])
        time.sleep(0.05)                                    # widen the race window
        if body["refresh_token"] != "r1":
            return 400, {"error": "invalid_grant"}
        return 200, {"access_token": "new", "refresh_token": "r2", "expires_in": 3600}
    monkeypatch.setattr(sp, "_http", fake_http)

    out = []
    ts = [threading.Thread(target=lambda: out.append(sp.access_token())) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert out == ["new"] * 6
    assert calls == ["r1"]                                  # exactly one refresh, never a reuse
    assert json.loads(kr.store[(sp._KEYRING_SERVICE, sp._SESSION_KEY)])["refresh_token"] == "r2"


def test_get_raises_instead_of_empty(pool, monkeypatch):
    pool({"access_token": "a1", "refresh_token": "r1", "expires_at": time.time() + 3600})
    monkeypatch.setattr(sp, "_http", lambda *a, **k: (None, "timed out"))
    with pytest.raises(sp.PoolError):
        sp.read_overview("Europe/Athens", "2026-07-06", "2026-07-05")
    with pytest.raises(sp.PoolError):
        sp.read_ledger("2026-07")
    monkeypatch.setattr(sp, "_http", lambda *a, **k: (200, []))
    ov = sp.read_overview("Europe/Athens", "2026-07-06", "2026-07-05")
    assert ov["accounts"] == []                             # a genuinely empty pool still works


def test_push_sends_hostname_as_device(pool, monkeypatch):
    pool({"access_token": "a1", "refresh_token": "r1", "expires_at": time.time() + 3600})
    sent = []
    monkeypatch.setattr(sp, "_http", lambda method, path, **k: (sent.append((path, k.get("body"))), (201, None))[1])
    row = {"name": "Paris", "fh_pct": 1.0, "ts": 1}
    assert sp.push(row, {"did": "abc123", "device": "DESKTOP-X", "tok_month": 5},
                   {"acct": "a@x.com", "org": None}, "2026-07-06")
    usage = next(b for p, b in sent if p.startswith("/rest/v1/usage"))
    assert usage["device"] == "DESKTOP-X"


def test_teamsync_pauses_when_device_claimed_elsewhere(monkeypatch):
    pushed = []
    monkeypatch.setattr(sp, "signed_in", lambda: True)
    monkeypatch.setattr(sp, "device_matches", lambda: False)
    monkeypatch.setattr(sp, "push", lambda *a, **k: pushed.append(a) or True)
    monkeypatch.setattr(sp, "device_id", lambda: "dev1")
    ts = team.TeamSync()
    ts.sync({"windows": []}, {})
    assert ts.device_ok is False and not pushed
    monkeypatch.setattr(sp, "device_matches", lambda: True)
    monkeypatch.setattr(usage, "read_account", lambda: {"email": "a@x.com", "name": "A"})
    monkeypatch.setattr(usage, "fetch_profile_org", lambda: None)
    ts.session_changed()                                    # e.g. "use it here" -> re-check now
    ts.sync({"windows": []}, {"team_tz": "Europe/Athens"})
    assert ts.device_ok is True and len(pushed) == 1
