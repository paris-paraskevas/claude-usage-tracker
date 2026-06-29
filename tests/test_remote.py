"""Remote-sync (optional, E2EE) unit tests. Skipped when the `remote` extra
(PyNaCl) isn't installed, so the core test job stays dependency-light."""
import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_usage_tracker as m  # noqa: E402

pytestmark = pytest.mark.skipif(not m.remote_available(),
                                reason="PyNaCl (the 'remote' extra) not installed")


@pytest.fixture
def tmp_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "REMOTE_PATH", tmp_path / "remote.json")
    monkeypatch.setattr(m, "_remote_id_cache", None)
    yield


def test_identity_generated_and_stable(tmp_identity):
    a = m.load_remote_identity(create=True)
    assert a and a["account_id"] and a["read_token"] and a["e2ee_key"]
    assert m.load_remote_identity(create=True) == a        # cached + persisted, stable


def test_no_identity_without_create(tmp_identity):
    assert m.load_remote_identity(create=False) is None


def test_encrypt_roundtrip(tmp_identity):
    from nacl.secret import SecretBox
    ident = m.load_remote_identity(create=True)
    obj = {"ok": True, "windows": [{"key": "five_hour", "pct": 64}], "blob": "y" * 200}
    blob = m.remote_encrypt(obj)
    assert blob["v"] == 1 and blob["nonce"] and blob["ct"]
    box = SecretBox(base64.b64decode(ident["e2ee_key"]))           # the phone's side
    pt = box.decrypt(base64.b64decode(blob["ct"]), base64.b64decode(blob["nonce"]))
    assert json.loads(pt) == obj


def test_two_blobs_use_distinct_nonces(tmp_identity):
    b1 = m.remote_encrypt({"a": 1})
    b2 = m.remote_encrypt({"a": 1})
    assert b1["nonce"] != b2["nonce"] and b1["ct"] != b2["ct"]    # random nonce per message


def test_remote_command_decrypt_roundtrip(tmp_identity):
    # The phone enqueues an E2EE command; the desktop decrypts it with the shared key.
    m.load_remote_identity(create=True)
    blob = m.remote_encrypt({"type": "prompt", "text": "hello claude"})
    assert m.remote_decrypt(blob) == {"type": "prompt", "text": "hello claude"}
    assert m.remote_decrypt({"nonce": "AA", "ct": "AA"}) is None  # garbage -> None
    assert m.remote_decrypt({}) is None


def test_pair_uri(tmp_identity):
    uri = m.remote_pair_uri({"remote_relay_url": "https://w.example.dev/"})
    assert uri.startswith("cutpair1:")
    raw = uri.split(":", 1)[1]
    raw += "=" * (-len(raw) % 4)
    payload = json.loads(base64.urlsafe_b64decode(raw))
    ident = m.load_remote_identity(create=True)
    assert payload["u"] == "https://w.example.dev"               # trailing slash trimmed
    assert payload["a"] == ident["account_id"]
    assert payload["t"] == ident["read_token"]
    assert payload["k"] == ident["e2ee_key"]


def test_pair_uri_requires_url(tmp_identity):
    assert m.remote_pair_uri({"remote_relay_url": ""}) is None


def test_rotate_changes_secrets(tmp_identity):
    a = m.load_remote_identity(create=True)
    b = m.rotate_remote_identity()
    assert b["e2ee_key"] != a["e2ee_key"] and b["read_token"] != a["read_token"]


def test_unpair_forgets_identity(tmp_identity):
    m.load_remote_identity(create=True)
    m.unpair_remote()
    assert m.load_remote_identity(create=False) is None
