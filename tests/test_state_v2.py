"""State v2 (V2-SPEC §5): the engine's state matches docs/design/state-v2.example.json key for key
and type for type; rev bumps only on change."""
import json
import time
from pathlib import Path

from conftest import feed

from ctu import paths

EXAMPLE = json.loads((Path(__file__).resolve().parent.parent / "docs" / "design" / "state-v2.example.json")
                     .read_text(encoding="utf-8"))
# Fields the engine emits beyond the example (documented in the hand-off report).
EXTRAS = {"app.accent", "app.theme", "app.statusline", "app.hook"}


def _kind(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    return type(v).__name__


def kinds(v, path="", acc=None):
    """{path: {kinds}} over every node; list items share one `[]` path (all items merged)."""
    acc = {} if acc is None else acc
    acc.setdefault(path, set()).add(_kind(v))
    if isinstance(v, dict):
        for k, x in v.items():
            kinds(x, f"{path}.{k}", acc)
    elif isinstance(v, list):
        for x in v:
            kinds(x, f"{path}[]", acc)
    return acc


def compare(ours, ex):
    """(problems, extra paths): every example path present with a kind the example allows there."""
    allowed, got = kinds(ex), kinds(ours)
    problems = [f"{p}: missing" for p in allowed if p not in got]
    problems += [f"{p}: {sorted(got[p])} not in {sorted(allowed[p])}" for p in allowed
                 if p in got and not got[p] <= allowed[p]]
    extras = {p.lstrip(".") for p in got if p not in allowed and p.rsplit(".", 1)[0] in allowed}
    return problems, extras


def build_example_like_state(make_engine):
    now = time.time()
    paths.CLAUDE_JSON.write_text(json.dumps({"oauthAccount": {
        "emailAddress": "paris@acme.dev", "displayName": "Paris", "organizationName": "Acme Labs"}}), encoding="utf-8")
    paths.CREDS_PATH.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "t", "expiresAt": (now + 3600) * 1000, "subscriptionType": "max"}}), encoding="utf-8")
    proj = paths.PROJECTS_DIR / "C--Dev-claude-usage-tracker"
    proj.mkdir(parents=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 40))
    (proj / "s1.jsonl").write_text(json.dumps({
        "type": "assistant", "timestamp": ts, "cwd": "C:\\Dev\\claude-usage-tracker", "sessionId": "s1",
        "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "tool_use", "name": "Read"}],
                    "usage": {"input_tokens": 6000, "output_tokens": 500, "cache_creation_input_tokens": 10000,
                              "cache_read_input_tokens": 130000}}}) + "\n", encoding="utf-8")
    e = make_engine(remote_enabled=True)
    e.sl_sessions = {"s1": {"ts": int(now), "model": "claude-opus-5-5", "window": 200000}}
    e.tailer.poll(now)
    e.extra = {"enabled": True, "used": 12.4, "limit": 50.0, "currency": "EUR", "pct": 24.8}
    e.status_raw = {"indicator": "none", "description": "All Systems Operational", "components": [],
                    "url": "https://status.anthropic.com"}
    e.remote.last_sync = int(now) - 8
    e.team_info = {"signed_in": True, "email": "paris@acme.dev", "role": "admin", "team": "acme.dev"}
    e.team_sync.device_ok = True
    e.team_merged = {"accounts": [
        {"acct": "ops@acme.dev", "name": "Ops", "month_spend": 0.0, "last_used": {"device": "OPS-LAPTOP"},
         "account": {"fh_pct": 18.0, "sd_pct": 33.0, "fh_resets_at": "2099-01-01T00:00:00+00:00",
                     "extra": {"currency": "EUR"}, "ts": int(now) - 60}},
        {"acct": "paris@acme.dev", "name": "Paris", "month_spend": 12.4, "last_used": {"device": "PARIS-DESK"},
         "account": {"fh_pct": 64.0, "sd_pct": 41.0, "fh_resets_at": "2099-01-01T00:00:00+00:00",
                     "extra": {"currency": "EUR"}, "ts": int(now) - 6}},
    ]}
    resets = int(now + 6720)
    week = int(now + 151920)
    feed(e, "seven_day", [(now - 6 * 3600 + i * 600, 39.5 + i * 0.04) for i in range(37)], week)
    for lid, used in (("seven_day_opus", 58.0), ("seven_day_sonnet", 23.0)):
        feed(e, lid, [(now - 7200 + i * 600, used) for i in range(13)], week, src="api")
    feed(e, "five_hour", [(now - 1800 + i * 60, 53.0 + 22.0 * i / 60) for i in range(31)], resets)
    return e


def test_state_matches_the_example(make_engine):
    e = build_example_like_state(make_engine)
    st = e.snapshot()
    problems, extras = compare(st, EXAMPLE)
    assert not problems, "\n".join(problems)
    assert extras == EXTRAS, extras
    assert st["decision"]["level"] == "will_hit" and st["decision"]["binding"] == "five_hour"
    assert st["decision"]["suggestion"]["email"] == "ops@acme.dev"
    assert [l["id"] for l in st["limits"]] == ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"]
    assert st["limits"][0]["binding"] and not any(l["binding"] for l in st["limits"][1:])
    assert st["alerts"][0]["kind"] == "will_hit" and st["alerts"][0]["id"].startswith("will_hit:five_hour:")
    assert st["sessions"][0]["context"]["window"] == 200000
    assert st["app"]["theme"] in ("light", "dark") and set(st["app"]["accent"]) == {"light", "dark"}
    json.dumps(st)                                           # serialisable


def test_rev_bumps_only_on_change(make_engine):
    e = make_engine()
    r0 = e.rev
    with e.lock:
        e.recompute()
    assert e.rev == r0                                       # nothing changed
    feed(e, "five_hour", [(time.time() - 60, 10.0)], int(time.time() + 3600))
    assert e.rev == r0 + 1
    st = e.snapshot()
    assert st["rev"] == e.rev and st["source"]["stale"] is False and st["source"]["kind"] == "statusline"


def test_staleness_and_auth_without_data(make_engine):
    st = make_engine().snapshot()
    assert st["source"]["stale"] is True and st["source"]["updated_at"] is None
    assert st["limits"] == [] and st["decision"]["binding"] is None
    assert st["source"]["auth"] == "missing" and st["decision"]["title"] == "Sign in to Claude Code"


def test_stale_readings_are_never_shown_as_live(make_engine):
    e = make_engine()
    old = time.time() - 3600
    feed(e, "five_hour", [(old, 40.0)], int(time.time() + 3600))
    st = e.snapshot()
    assert st["source"]["stale"] is True
    assert st["decision"]["detail"].startswith("As of ")


def test_expired_window_reads_as_refilled(make_engine):
    e = make_engine()
    now = time.time()
    feed(e, "five_hour", [(now - 600 + i * 60, 100.0) for i in range(10)], int(now + 2))
    assert e.state["limits"][0]["level"] == "limited" and e.state["decision"]["resume_at"] == int(now + 2)
    time.sleep(2.1)
    with e.lock:
        e.recompute()
    lim = e.state["limits"][0]
    assert lim["used_pct"] == 0.0 and lim["resets_at"] is None and lim["level"] == "ok"
    assert any(a["kind"] == "refilled" for a in e.state["alerts"])


def test_compare_catches_drift():
    import copy
    bad = copy.deepcopy(EXAMPLE)
    del bad["decision"]["resume_at"]
    bad["limits"][1]["used_pct"] = "41"
    bad["app"]["surprise"] = 1
    problems, extras = compare(bad, EXAMPLE)
    assert ".decision.resume_at: missing" in problems
    assert any(p.startswith(".limits[].used_pct") for p in problems)
    assert extras == {"app.surprise"}
