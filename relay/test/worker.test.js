// End-to-end tests of the Worker's fetch/scheduled handlers against fake bindings:
// DCR → consent → code → token → MCP → refresh (rotation) → connector-token revocation,
// plus phone-sync route behaviour and the daily prune.
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import worker from "../src/index.js";
import { fakeD1, fakeKV, fakeSupabase } from "./fakes.js";
import { prevMonth } from "../src/lib.js";

const ORIGIN = "https://relay.test";
const REDIRECT = "https://claude.ai/api/mcp/auth_callback";
const sha = (s) => createHash("sha256").update(s).digest("hex");

let env, sb, realFetch, realNow, clock;

beforeEach(() => {
  sb = { connectors: new Map([[sha("ct-good"), "acme.com"]]), tz: { "acme.com": "America/New_York" } };
  realFetch = globalThis.fetch;
  globalThis.fetch = fakeSupabase(sb);
  env = { DB: fakeD1(), KV: fakeKV(), SUPABASE_URL: "https://sb.test", SUPABASE_SECRET: "sb_secret_x" };
  // A controllable clock so the 60 s connector-token cache can be stepped past. Each test
  // starts a day further on so per-isolate caches from earlier tests have expired.
  realNow = Date.now;
  clock = (clock || realNow()) + 86400_000;
  Date.now = () => clock;
});
afterEach(() => {
  globalThis.fetch = realFetch;
  Date.now = realNow;
});

function call(path, { method = "GET", headers = {}, body, form, ip } = {}) {
  const h = new Headers(headers);
  if (ip) h.set("cf-connecting-ip", ip);
  let payload = body;
  if (form) {
    payload = new URLSearchParams(form).toString();
    h.set("content-type", "application/x-www-form-urlencoded");
  } else if (body !== undefined && typeof body !== "string") {
    payload = JSON.stringify(body);
    h.set("content-type", "application/json");
  }
  return worker.fetch(new Request(ORIGIN + path, { method, headers: h, body: payload, redirect: "manual" }), env, {});
}

async function register(extra = {}, ip = "198.51.100.7") {
  const r = await call("/oauth/register", {
    method: "POST", ip, body: { client_name: "Claude", redirect_uris: [REDIRECT], ...extra },
  });
  return { status: r.status, body: await r.json() };
}

function pkce() {
  const verifier = randomBytes(32).toString("base64url");
  return { verifier, challenge: createHash("sha256").update(verifier).digest("base64url") };
}

function authParams(clientId, challenge, extra = {}) {
  return {
    client_id: clientId, redirect_uri: REDIRECT, response_type: "code",
    code_challenge: challenge, code_challenge_method: "S256", state: "st-123", ...extra,
  };
}

async function authorize(clientId, challenge, connectorToken = "ct-good") {
  const r = await call("/oauth/authorize", {
    method: "POST", form: { ...authParams(clientId, challenge), connector_token: connectorToken, action: "allow" },
  });
  assert.equal(r.status, 302);
  const loc = new URL(r.headers.get("location"));
  assert.equal(loc.searchParams.get("state"), "st-123");
  return loc.searchParams.get("code");
}

async function exchange(clientId, code, verifier) {
  const r = await call("/oauth/token", {
    method: "POST",
    form: { grant_type: "authorization_code", code, code_verifier: verifier, redirect_uri: REDIRECT, client_id: clientId },
  });
  return { status: r.status, body: await r.json(), headers: r.headers };
}

async function refresh(clientId, refreshToken) {
  const r = await call("/oauth/token", {
    method: "POST", form: { grant_type: "refresh_token", refresh_token: refreshToken, client_id: clientId },
  });
  return { status: r.status, body: await r.json() };
}

function mcp(token, msg) {
  return call("/mcp", { method: "POST", headers: token ? { authorization: `Bearer ${token}` } : {}, body: msg });
}

async function connect() {
  const { body: client } = await register();
  const { verifier, challenge } = pkce();
  const code = await authorize(client.client_id, challenge);
  const tok = await exchange(client.client_id, code, verifier);
  assert.equal(tok.status, 200);
  return { clientId: client.client_id, ...tok.body };
}

test("metadata advertises refresh_token", async () => {
  const as = await (await call("/.well-known/oauth-authorization-server")).json();
  assert.deepEqual(as.grant_types_supported, ["authorization_code", "refresh_token"]);
  assert.deepEqual(as.code_challenge_methods_supported, ["S256"]);
  const pr = await (await call("/.well-known/oauth-protected-resource")).json();
  assert.equal(pr.resource, ORIGIN + "/mcp");
});

test("DCR validates redirect_uris and client_name, and is rate-limited per IP", async () => {
  const ok = await register({ client_name: "  A".padEnd(150, "b") });
  assert.equal(ok.status, 201);
  assert.ok(ok.body.grant_types.includes("refresh_token"));
  assert.equal(ok.body.client_name.length, 64);
  assert.equal((await register({ redirect_uris: ["http://evil.example/cb"] })).status, 400);
  assert.equal((await register({ redirect_uris: Array(6).fill(REDIRECT).map((u, i) => u + i) })).status, 400);
  assert.equal((await register({ redirect_uris: [REDIRECT + "x".repeat(600)] })).status, 400);
  assert.equal((await register({ client_name: "x".repeat(201) })).status, 400);
  assert.equal((await register({ redirect_uris: ["http://localhost:3000/cb"] })).status, 201);
  // Failed validations don't count; 2 successes so far from this IP → 8 more allowed.
  for (let i = 0; i < 8; i++) assert.equal((await register()).status, 201);
  const limited = await register();
  assert.equal(limited.status, 429);
  assert.equal((await register({}, "203.0.113.9")).status, 201, "other IPs are unaffected");
});

test("consent page: client name, redirect host, headers, validation, deny", async () => {
  const { body: client } = await register({ client_name: "<b>Evil</b> Claude" });
  const { challenge } = pkce();
  const qs = new URLSearchParams(authParams(client.client_id, challenge)).toString();
  const page = await call(`/oauth/authorize?${qs}`);
  assert.equal(page.status, 200);
  const htmlText = await page.text();
  assert.match(htmlText, /^<!doctype html><html lang="en"><head>/);
  assert.match(htmlText, /name="viewport"/);
  assert.ok(htmlText.includes("&lt;b&gt;Evil&lt;/b&gt; Claude"), "client name is escaped");
  assert.ok(!htmlText.includes("<b>Evil</b>"));
  assert.ok(htmlText.includes(">claude.ai</b>"), "redirect host shown");
  assert.match(htmlText, /<label for="ct">Connector token<\/label>/);
  assert.match(htmlText, /Mint connector token/);
  assert.match(htmlText, /prefers-color-scheme:dark/);
  assert.equal(page.headers.get("x-frame-options"), "DENY");
  assert.match(page.headers.get("content-security-policy"), /frame-ancestors 'none'/);

  // Unknown client / mismatched redirect → styled error page (no redirect), same headers.
  const bad = await call(`/oauth/authorize?${new URLSearchParams(authParams(client.client_id, challenge, { redirect_uri: "https://evil.example/cb" }))}`);
  assert.equal(bad.status, 400);
  assert.equal(bad.headers.get("x-frame-options"), "DENY");
  assert.ok(!(await bad.text()).includes("javascript:"));

  // Wrong response_type / missing S256 → redirected back with an OAuth error.
  const rt = await call(`/oauth/authorize?${new URLSearchParams(authParams(client.client_id, challenge, { response_type: "token" }))}`);
  assert.equal(rt.status, 302);
  assert.equal(new URL(rt.headers.get("location")).searchParams.get("error"), "unsupported_response_type");
  const plain = await call(`/oauth/authorize?${new URLSearchParams(authParams(client.client_id, challenge, { code_challenge_method: "plain" }))}`);
  assert.equal(new URL(plain.headers.get("location")).searchParams.get("error"), "invalid_request");

  // Deny → access_denied with state.
  const deny = await call("/oauth/authorize", { method: "POST", form: { ...authParams(client.client_id, challenge), action: "deny" } });
  assert.equal(deny.status, 302);
  const dl = new URL(deny.headers.get("location"));
  assert.equal(dl.origin + dl.pathname, REDIRECT);
  assert.equal(dl.searchParams.get("error"), "access_denied");
  assert.equal(dl.searchParams.get("state"), "st-123");

  // Wrong connector token → the consent form is re-rendered with an inline error.
  const wrong = await call("/oauth/authorize", { method: "POST", form: { ...authParams(client.client_id, challenge), connector_token: "nope" } });
  assert.equal(wrong.status, 403);
  const wt = await wrong.text();
  assert.match(wt, /role="alert"/);
  assert.match(wt, /name="connector_token"/);

  // Supabase down → inline "try again", not a 500.
  sb.down = true;
  const down = await call("/oauth/authorize", { method: "POST", form: { ...authParams(client.client_id, challenge), connector_token: "ct-good" } });
  assert.equal(down.status, 503);
  assert.match(await down.text(), /try again/);
});

test("full flow: code → token → MCP → refresh rotation → revocation", async () => {
  // Step to 02:30 UTC on the 1st of next month: already the new month in UTC, still the last
  // day of the old month in New York.
  const d = new Date(clock);
  clock = Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1, 2, 30);
  const nyToday = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)).toISOString().slice(0, 10);
  const nyMonth = nyToday.slice(0, 7);
  assert.notEqual(new Date(clock).toISOString().slice(0, 7), nyMonth);
  const { body: client } = await register();
  const { verifier, challenge } = pkce();

  // A wrong verifier burns the code (single-use) and fails.
  const code0 = await authorize(client.client_id, challenge);
  assert.equal((await exchange(client.client_id, code0, pkce().verifier)).body.error, "invalid_grant");
  assert.equal((await exchange(client.client_id, code0, verifier)).body.error, "invalid_grant", "code is single-use");

  const code = await authorize(client.client_id, challenge);
  const tok = await exchange(client.client_id, code, verifier);
  assert.equal(tok.status, 200);
  assert.equal(tok.headers.get("cache-control"), "no-store");
  assert.ok(tok.body.access_token && tok.body.refresh_token);
  assert.equal(tok.body.token_type, "Bearer");
  const row = env.DB.sqlite.prepare("SELECT tid, ct_hash FROM oauth_tokens").get();
  assert.equal(row.tid, "acme.com");
  assert.equal(row.ct_hash, sha("ct-good"));

  // MCP works and ledger defaults to the current month in the TEAM tz (from public.teams).
  const init = await (await mcp(tok.body.access_token, { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18" } })).json();
  assert.equal(init.result.protocolVersion, "2025-06-18");
  const led = await (await mcp(tok.body.access_token, { jsonrpc: "2.0", id: 2, method: "tools/call", params: { name: "get_team_ledger", arguments: {} } })).json();
  assert.equal(JSON.parse(led.result.content[0].text).month, nyMonth);
  assert.deepEqual(sb.months.slice(-2), [nyMonth, prevMonth(nyMonth)]);

  // Overview carries structuredContent + ui link.
  const ov = await (await mcp(tok.body.access_token, { jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "get_team_overview" } })).json();
  assert.equal(ov.result.structuredContent.tz, "America/New_York");
  assert.equal(ov.result.structuredContent.today, nyToday);

  // Refresh: rotates; the old refresh token is dead afterwards.
  const r1 = await refresh(client.client_id, tok.body.refresh_token);
  assert.equal(r1.status, 200);
  assert.notEqual(r1.body.refresh_token, tok.body.refresh_token);
  assert.equal((await refresh(client.client_id, tok.body.refresh_token)).body.error, "invalid_grant");
  assert.equal((await refresh("someone-else", r1.body.refresh_token)).body.error, "invalid_grant");
  const ping = await mcp(r1.body.access_token, { jsonrpc: "2.0", id: 4, method: "ping" });
  assert.deepEqual((await ping.json()).result, {});

  // Revoke the connector token in Supabase: within the 60 s cache the token still works...
  sb.connectors.clear();
  assert.equal((await mcp(r1.body.access_token, { jsonrpc: "2.0", id: 5, method: "ping" })).status, 200);
  // ...after it, 401 + WWW-Authenticate (invalid_token) so claude.ai re-authorises.
  clock += 61_000;
  const dead = await mcp(r1.body.access_token, { jsonrpc: "2.0", id: 6, method: "ping" });
  assert.equal(dead.status, 401);
  assert.match(dead.headers.get("www-authenticate"), /error="invalid_token"/);
  assert.match(dead.headers.get("www-authenticate"), /resource_metadata="https:\/\/relay\.test\/\.well-known\/oauth-protected-resource"/);
  // Every token minted from that connector token is gone, so refresh fails too.
  assert.equal(env.DB.sqlite.prepare("SELECT count(*) AS n FROM oauth_tokens").get().n, 0);
  assert.equal((await refresh(client.client_id, r1.body.refresh_token)).body.error, "invalid_grant");
});

test("refresh is refused once the connector token is revoked (checked live, not cached)", async () => {
  const c = await connect();
  sb.connectors.clear();
  const r = await refresh(c.clientId, c.refresh_token);
  assert.equal(r.status, 400);
  assert.equal(r.body.error, "invalid_grant");
});

test("MCP: auth errors, notifications, Supabase outage", async () => {
  const none = await mcp(null, { jsonrpc: "2.0", id: 1, method: "ping" });
  assert.equal(none.status, 401);
  assert.match(none.headers.get("www-authenticate"), /^Bearer resource_metadata=/);
  assert.equal((await mcp("garbage", { jsonrpc: "2.0", id: 1, method: "ping" })).status, 401);
  assert.equal((await call("/mcp")).status, 405);

  // A pre-migration token (no ct_hash) is treated as expired.
  env.DB.sqlite.prepare("INSERT INTO oauth_tokens(token_hash,tid,exp) VALUES(?,?,?)").run(sha("legacy"), "acme.com", Date.now() + 1e9);
  assert.equal((await mcp("legacy", { jsonrpc: "2.0", id: 1, method: "ping" })).status, 401);

  const c = await connect();
  const note = await mcp(c.access_token, { jsonrpc: "2.0", method: "notifications/initialized" });
  assert.equal(note.status, 202);
  assert.equal(await note.text(), "");
  const parse = await call("/mcp", { method: "POST", headers: { authorization: `Bearer ${c.access_token}` }, body: "{not json" });
  assert.equal(parse.status, 400);

  clock += 61_000;   // past the positive cache
  sb.down = true;
  const out = await mcp(c.access_token, { jsonrpc: "2.0", id: 2, method: "ping" });
  assert.equal(out.status, 503, "an outage is not a revocation");
  sb.down = false;
  assert.equal((await mcp(c.access_token, { jsonrpc: "2.0", id: 3, method: "ping" })).status, 200);
});

test("daily prune removes expired OAuth state, unused clients and snapshots", async () => {
  const now = Date.now();
  const db = env.DB.sqlite;
  db.prepare("INSERT INTO oauth_clients(client_id,redirect_uris,name,created,used) VALUES(?,?,?,?,?)").run("old-unused", "[]", "x", now - 8 * 86400e3, null);
  db.prepare("INSERT INTO oauth_clients(client_id,redirect_uris,name,created,used) VALUES(?,?,?,?,?)").run("new-unused", "[]", "x", now - 86400e3, null);
  db.prepare("INSERT INTO oauth_clients(client_id,redirect_uris,name,created,used) VALUES(?,?,?,?,?)").run("active", "[]", "x", now - 200 * 86400e3, now - 86400e3);
  db.prepare("INSERT INTO oauth_clients(client_id,redirect_uris,name,created,used) VALUES(?,?,?,?,?)").run("idle", "[]", "x", now - 200 * 86400e3, now - 100 * 86400e3);
  db.prepare("INSERT INTO oauth_codes(code_hash,client_id,redirect_uri,code_challenge,tid,exp,ct_hash) VALUES(?,?,?,?,?,?,?)").run("c", "a", "r", "x", "t", now - 1, "h");
  db.prepare("INSERT INTO oauth_tokens(token_hash,tid,exp,ct_hash) VALUES(?,?,?,?)").run("t1", "t", now - 1, "h");
  db.prepare("INSERT INTO oauth_tokens(token_hash,tid,exp,ct_hash) VALUES(?,?,?,?)").run("t2", "t", now + 1000, "h");
  db.prepare("INSERT INTO oauth_refresh(token_hash,client_id,tid,ct_hash,exp) VALUES(?,?,?,?,?)").run("r1", "a", "t", "h", now - 1);
  db.prepare("INSERT INTO oauth_reg_rl(ip,win,n) VALUES(?,?,?)").run("ip", Math.floor(now / 3600e3) - 5, 3);
  db.prepare("INSERT INTO snapshots(account_id,exp) VALUES(?,?)").run("gone", now - 1);
  db.prepare("INSERT INTO snapshots(account_id,exp) VALUES(?,?)").run("kept", now + 1000);
  let p;
  await worker.scheduled({}, env, { waitUntil: (x) => { p = x; } });
  await p;
  const ids = (t, c) => db.prepare(`SELECT ${c} AS k FROM ${t} ORDER BY 1`).all().map((r) => r.k);
  assert.deepEqual(ids("oauth_clients", "client_id"), ["active", "new-unused"]);
  assert.deepEqual(ids("oauth_codes", "code_hash"), []);
  assert.deepEqual(ids("oauth_tokens", "token_hash"), ["t2"]);
  assert.deepEqual(ids("oauth_refresh", "token_hash"), []);
  assert.deepEqual(ids("oauth_reg_rl", "ip"), []);
  assert.deepEqual(ids("snapshots", "account_id"), ["kept"]);
});

test("phone sync: command channel and team routes are gone; push-token PUT/DELETE work", async () => {
  const acct = "acct_12345678";
  const auth = { authorization: "Bearer read-token" };
  assert.equal((await call(`/v1/acct/${acct}/snapshot`, { method: "PUT", headers: auth, body: { nonce: "n", ct: "c" } })).status, 204);
  assert.equal((await call(`/v1/acct/${acct}/command`, { headers: auth })).status, 404);
  assert.equal((await call(`/v1/acct/${acct}/command`, { method: "PUT", headers: auth, body: { nonce: "n", ct: "c" } })).status, 404);
  assert.equal((await call("/v1/team/abcdefgh12/overview", { headers: auth })).status, 404);

  assert.equal((await call(`/v1/acct/${acct}/push-token`, { method: "PUT", headers: auth, body: { token: "fcm-1" } })).status, 204);
  assert.equal((await call(`/v1/acct/${acct}/push-token`, { method: "PUT", headers: auth, body: { token: "fcm-2" } })).status, 204);
  assert.deepEqual(JSON.parse(await env.KV.get(`tokens:${acct}`)), ["fcm-1", "fcm-2"]);
  assert.equal((await call(`/v1/acct/${acct}/push-token`, { method: "DELETE", headers: auth, body: { token: "fcm-1" } })).status, 204);
  assert.deepEqual(JSON.parse(await env.KV.get(`tokens:${acct}`)), ["fcm-2"]);
  assert.equal((await call(`/v1/acct/${acct}/push-token`, { method: "POST", headers: auth, body: { token: "x" } })).status, 405);
  assert.equal((await call(`/v1/acct/${acct}/snapshot`, { headers: { authorization: "Bearer wrong" } })).status, 403);
});

test("push: refreshes the push-token set TTL at most once a day", async () => {
  const acct = "acct_87654321";
  const auth = { authorization: "Bearer read-token" };
  env.FCM_PROJECT_ID = "p";
  await env.KV.put("gtoken", JSON.stringify({ token: "g", exp: Math.floor(Date.now() / 1000) + 3600 }));
  await call(`/v1/acct/${acct}/snapshot`, { method: "PUT", headers: auth, body: { nonce: "n", ct: "c" } });
  await call(`/v1/acct/${acct}/push-token`, { method: "PUT", headers: auth, body: { token: "fcm-1" } });
  const rt0 = env.KV.map.get(`tokens:${acct}`).metadata.rt;

  const push = async () => (await call(`/v1/acct/${acct}/push`, { method: "POST", headers: auth, body: { nonce: "n", ct: "c" } })).json();
  assert.deepEqual(await push(), { sent: 1 });
  assert.equal(env.KV.map.get(`tokens:${acct}`).metadata.rt, rt0, "no rewrite within a day");
  clock += 86400_000 + 1;
  // Keep the cached Google token valid at the new clock.
  await env.KV.put("gtoken", JSON.stringify({ token: "g", exp: Math.floor(Date.now() / 1000) + 3600 }));
  const writes = env.KV.writes;
  assert.deepEqual(await push(), { sent: 1 });
  assert.equal(env.KV.writes, writes + 1, "token set re-put exactly once");
  assert.equal(env.KV.map.get(`tokens:${acct}`).metadata.rt, Date.now());
  assert.equal(env.KV.map.get(`tokens:${acct}`).ttl, 7 * 86400);
});

test("unexpected errors return a generic 500 without details", async () => {
  env.DB = { prepare() { throw new Error("SQLITE_ERROR: secret detail"); } };
  const origError = console.error;
  console.error = () => {};
  try {
    const r = await call("/v1/acct/acct_12345678/snapshot", { method: "PUT", headers: { authorization: "Bearer t" }, body: { nonce: "n", ct: "c" } });
    assert.equal(r.status, 500);
    assert.deepEqual(await r.json(), { error: "internal" });
  } finally {
    console.error = origError;
  }
});
