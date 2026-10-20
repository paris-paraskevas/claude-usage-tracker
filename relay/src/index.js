/**
 * Claude Usage Tracker — remote relay (Cloudflare Worker).
 *
 * Two independent features share this Worker:
 *
 * 1. Phone sync (`/v1/acct/{id}/{snapshot|push-token|push}`) — a zero-knowledge dumb pipe
 *    between the desktop tracker (producer) and the Android app (consumer). Stores and
 *    forwards END-TO-END-ENCRYPTED blobs and never sees the e2eeKey or any plaintext.
 *    Snapshot ciphertext lives in D1 (`snapshots`); the per-account auth-token hash and the
 *    FCM push-token set live in KV. See docs/REMOTE.md for the contract.
 *
 * 2. claude.ai connector — an OAuth 2.1 provider (DCR + PKCE + refresh tokens) plus a remote
 *    MCP server at `/mcp` that serves the team account pool READ-ONLY from Supabase
 *    (service_role RPCs). Every grant is bound to a connector token a team admin mints in
 *    the desktop app; revoking or expiring that token in Supabase cuts the connector off
 *    within ~60 s. OAuth state (clients, codes, tokens) lives in D1 (`oauth_*`).
 *    See docs/MCP-REMOTE.md.
 *
 * Bindings: KV namespace `KV`, D1 database `DB`. Vars: SUPABASE_URL.
 * Secrets:  SUPABASE_SECRET (connector), FCM_PROJECT_ID, FCM_CLIENT_EMAIL, FCM_PRIVATE_KEY (push).
 * Cron:     one daily run that prunes expired snapshots and OAuth state.
 */

import {
  DEFAULT_TEAM_TZ, validTz, tzParts, monthIn, prevMonth,
  buildOverview, buildLedger, ledgerComputed, memberMonthTokens, overviewMerge,
  escapeHtml, cleanName, validateRedirectUris, isValidChallenge, verifyPkce,
  sha256hex, randB64url, b64urlBytes, timingSafeEqual, dispatchRpc,
} from "./lib.js";
import { POOL_UI_URI, POOL_WIDGET_HTML } from "./widget.js";

const WRITE_MIN_GAP_MS = 5000;             // per-account snapshot-write floor (~10s sync; D1-backed)
const SNAPSHOT_TTL_S = 7 * 86400;          // forget stale accounts that stop syncing
const AUTH_REFRESH_GAP_MS = 86400 * 1000;  // refresh a KV key's 7-day TTL at most once/day, not every push

export default {
  async fetch(request, env, ctx) {
    try {
      return await handle(request, env, ctx);
    } catch (err) {
      console.error("unhandled", err && (err.stack || err.message || err));
      return cors(json({ error: "internal" }, 500));
    }
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dailyPrune(env));
  },
};

async function handle(request, env, ctx) {
  const url = new URL(request.url);
  const { method } = request;

  if (method === "OPTIONS") return cors(new Response(null, { status: 204 }));
  if (url.pathname === "/" || url.pathname === "/health") {
    return cors(json({ ok: true, service: "claude-usage-relay", v: 1 }));
  }

  if (url.pathname.startsWith("/.well-known/oauth") || url.pathname.startsWith("/oauth/")) {
    return cors(await handleOAuth(request, env, url));
  }
  if (url.pathname === "/mcp") return cors(await handleMcp(request, env, url));

  // /v1/acct/{accountId}/{resource}
  const m = url.pathname.match(/^\/v1\/acct\/([A-Za-z0-9_-]{8,64})\/(snapshot|push-token|push)$/);
  if (!m) return cors(json({ error: "not_found" }, 404));
  const accountId = m[1];
  const resource = m[2];

  const bearer = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "").trim();
  if (!bearer) return cors(json({ error: "unauthorized" }, 401));
  const bearerHash = await sha256hex(bearer);

  const authKey = `auth:${accountId}`;
  // getWithMetadata (not get): lets the snapshot PUT below see when the auth TTL was last
  // refreshed — from this same read — so it only re-writes the key when a refresh is due.
  const authRec = await env.KV.getWithMetadata(authKey);
  const storedHash = authRec.value;
  const authRefreshedAt = (authRec.metadata && Number(authRec.metadata.rt)) || 0;

  // Trust-on-first-use: the very first snapshot PUT pins the readToken hash.
  const isFirstWrite = !storedHash && method === "PUT" && resource === "snapshot";
  if (!storedHash && !isFirstWrite) {
    // A consumer GET before the desktop's first push isn't an error — the account
    // simply has no snapshot yet. Answer 204 so the phone shows a "waiting to sync"
    // state instead of a scary 404. Other verbs on an unknown account stay 404.
    if (method === "GET" && resource === "snapshot") return cors(new Response(null, { status: 204 }));
    return cors(json({ error: "unknown_account" }, 404));
  }
  if (storedHash && !timingSafeEqual(storedHash, bearerHash)) {
    return cors(json({ error: "forbidden" }, 403));
  }

  if (resource === "snapshot") {
    if (method === "PUT") {
      // Snapshot blob lives in D1 (not KV) so a ~10s sync stays free — D1 allows 100k
      // writes/day vs KV's 1,000. Still E2EE: only the opaque ciphertext is stored.
      const prev = await env.DB.prepare("SELECT wts FROM snapshots WHERE account_id=?").bind(accountId).first();
      const lastWrite = (prev && prev.wts) || 0;
      if (lastWrite && Date.now() - lastWrite < WRITE_MIN_GAP_MS) {
        return cors(json({ error: "rate_limited" }, 429));
      }
      const blob = await readBlob(request);
      if (!blob) return cors(json({ error: "bad_blob" }, 400));
      const now = Date.now();
      await env.DB.prepare(
        "INSERT INTO snapshots(account_id,v,nonce,ct,ts,wts,exp) VALUES(?1,?2,?3,?4,?5,?6,?7) " +
        "ON CONFLICT(account_id) DO UPDATE SET v=?2,nonce=?3,ct=?4,ts=?5,wts=?6,exp=?7"
      ).bind(accountId, blob.v || 1, blob.nonce, blob.ct, blob.ts, now, now + SNAPSHOT_TTL_S * 1000).run();
      // Auth-token hash stays in KV: written once on first use, then its 7-day TTL is
      // refreshed at most once a day (not every push) — a handful of KV writes/day total.
      if (isFirstWrite || (storedHash && now - authRefreshedAt > AUTH_REFRESH_GAP_MS)) {
        await env.KV.put(authKey, bearerHash, {
          expirationTtl: SNAPSHOT_TTL_S,
          metadata: { rt: now },
        });
      }
      return cors(new Response(null, { status: 204 }));
    }
    if (method === "GET") {
      const r = await env.DB.prepare("SELECT v,nonce,ct,ts,exp FROM snapshots WHERE account_id=?")
        .bind(accountId).first();
      if (!r || (r.exp && r.exp < Date.now())) return cors(new Response(null, { status: 204 }));
      return cors(new Response(JSON.stringify({ v: r.v, nonce: r.nonce, ct: r.ct, ts: r.ts }),
        { status: 200, headers: { "content-type": "application/json" } }));
    }
  }

  if (resource === "push-token") {
    // PUT registers, DELETE unregisters; both take {"token": "<fcm token>"} in the body.
    if (method !== "PUT" && method !== "DELETE") return cors(json({ error: "method" }, 405));
    const body = await readJson(request);
    const token = body && typeof body.token === "string" && body.token.length <= 4096 ? body.token : null;
    if (!token) return cors(json({ error: "bad_token" }, 400));
    const setKey = `tokens:${accountId}`;
    const set = new Set(parseList(await env.KV.get(setKey)));
    if (method === "PUT") set.add(token);
    else set.delete(token);
    await putTokenSet(env, setKey, [...set].slice(-20));
    return cors(new Response(null, { status: 204 }));
  }

  if (resource === "push" && method === "POST") {
    const blob = await readBlob(request);
    if (!blob) return cors(json({ error: "bad_blob" }, 400));
    const setKey = `tokens:${accountId}`;
    const rec = await env.KV.getWithMetadata(setKey);
    const tokens = parseList(rec.value);
    const refreshedAt = (rec.metadata && Number(rec.metadata.rt)) || 0;
    if (!tokens.length) return cors(json({ sent: 0 }));
    let access;
    try {
      access = await googleAccessToken(env);
    } catch (e) {
      console.error("fcm token", e && (e.message || e));
      return cors(json({ error: "fcm_unconfigured" }, 503));
    }
    const dead = [];
    let sent = 0;
    for (const token of tokens) {
      const ok = await sendFcm(env, access, token, blob);
      if (ok === "gone") dead.push(token);
      else if (ok === true) sent++;
    }
    if (dead.length) {
      await putTokenSet(env, setKey, tokens.filter((t) => !dead.includes(t)));
    } else if (sent > 0 && Date.now() - refreshedAt > AUTH_REFRESH_GAP_MS) {
      // The token set's TTL is otherwise only refreshed when the phone re-registers; an
      // active desktop pushing alerts keeps it alive too (≤1 KV write per account per day).
      await putTokenSet(env, setKey, tokens);
    }
    return cors(json({ sent }));
  }

  return cors(json({ error: "method_not_allowed" }, 405));
}

function parseList(v) {
  try {
    const a = JSON.parse(v || "[]");
    return Array.isArray(a) ? a.filter((t) => typeof t === "string") : [];
  } catch {
    return [];
  }
}

async function putTokenSet(env, key, list) {
  await env.KV.put(key, JSON.stringify(list), {
    expirationTtl: SNAPSHOT_TTL_S,
    metadata: { rt: Date.now() },
  });
}

// ---- Daily housekeeping (wrangler.toml [triggers]) -------------------------
//
// D1 has no TTL, so this replaces KV's automatic expiry for snapshots and keeps the OAuth
// tables from growing without bound (DCR is open to the internet).

async function dailyPrune(env) {
  const now = Date.now();
  await env.DB.batch([
    env.DB.prepare("DELETE FROM snapshots WHERE exp < ?1").bind(now),
    env.DB.prepare("DELETE FROM oauth_codes WHERE exp < ?1").bind(now),
    env.DB.prepare("DELETE FROM oauth_tokens WHERE exp < ?1").bind(now),
    env.DB.prepare("DELETE FROM oauth_refresh WHERE exp < ?1").bind(now),
    // Registered but never completed a grant within 7 days, or idle past the refresh lifetime
    // (every refresh bumps `used`, so such a client can't hold a live token any more).
    env.DB.prepare("DELETE FROM oauth_clients WHERE (used IS NULL AND created < ?1) OR used < ?2")
      .bind(now - OAUTH_CLIENT_UNUSED_TTL_MS, now - OAUTH_REFRESH_TTL_MS - 86400_000),
    env.DB.prepare("DELETE FROM oauth_reg_rl WHERE win < ?1").bind(Math.floor(now / 3600_000) - 1),
  ]);
}

// ---- Supabase account-pool reads (service_role) ----------------------------
//
// The claude.ai connector reads the pool from Supabase. SUPABASE_SECRET is a wrangler secret
// (never in wrangler.toml/[vars]); SUPABASE_URL is a [vars] entry.

function sbHeaders(env) {
  return {
    apikey: env.SUPABASE_SECRET, authorization: `Bearer ${env.SUPABASE_SECRET}`,
    "content-type": "application/json",
  };
}

async function sbRpc(env, fn, body) {
  const res = await fetch(`${env.SUPABASE_URL}/rest/v1/rpc/${fn}`, {
    method: "POST", headers: sbHeaders(env), body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`supabase rpc ${fn} -> ${res.status}`);
  return res.json();
}

// Per-isolate caches. Isolates are short-lived and not shared, so these only shave repeat
// Supabase calls off bursts of MCP requests; correctness never depends on them.
const CT_CACHE_MS = 60 * 1000;
const TZ_CACHE_MS = 10 * 60 * 1000;
const ctCache = new Map();   // connector-token sha256 -> { team, until }
const tzCache = new Map();   // team -> { tz, until }

// The team a connector token (by sha256 hex) currently resolves to, or null if it's unknown,
// expired or revoked. Throws if Supabase is unreachable (callers decide how to fail).
async function connectorTeam(env, ctHash, useCache = true) {
  if (!ctHash) return null;
  const now = Date.now();
  if (useCache) {
    const c = ctCache.get(ctHash);
    if (c && c.until > now) return c.team;
  }
  const res = await sbRpc(env, "resolve_connector_token", { p_hash: ctHash });
  const team = typeof res === "string" && res ? res : null;
  if (team) {
    if (ctCache.size > 1000) ctCache.clear();
    ctCache.set(ctHash, { team, until: now + CT_CACHE_MS });
  } else {
    ctCache.delete(ctHash);
  }
  return team;
}

// The team's timezone from public.teams (an optional per-team override table), falling back
// to the default when the team has no row or Supabase can't be read.
async function teamTz(env, team) {
  const now = Date.now();
  const c = tzCache.get(team);
  if (c && c.until > now) return c.tz;
  let tz = DEFAULT_TEAM_TZ, ttl = TZ_CACHE_MS;
  try {
    const res = await fetch(
      `${env.SUPABASE_URL}/rest/v1/teams?select=tz&team=eq.${encodeURIComponent(team)}&limit=1`,
      { headers: sbHeaders(env) });
    if (res.ok) {
      const rows = await res.json();
      tz = validTz(Array.isArray(rows) && rows[0] && rows[0].tz) || DEFAULT_TEAM_TZ;
    } else {
      console.error("teams tz read", res.status);
      ttl = 60 * 1000;
    }
  } catch (e) {
    console.error("teams tz read", e && (e.message || e));
    ttl = 60 * 1000;
  }
  tzCache.set(team, { tz, until: now + ttl });
  return tz;
}

async function sbTeamOverview(env, team, tz) {
  const now = new Date(Date.now());
  const today = tzParts(tz, now).date;
  const yesterday = tzParts(tz, new Date(now.getTime() - 86400_000)).date;
  const rows = await sbRpc(env, "get_team_usage", { p_team: team, p_dates: [today, yesterday] });
  return buildOverview(team, tz, today, yesterday, rows);
}

async function sbTeamLedger(env, team, month) {
  return buildLedger(team, month, await sbRpc(env, "get_team_month", { p_team: team, p_month: month }));
}

// ---- Remote MCP (docs/MCP-REMOTE.md) — team pool tools over Streamable HTTP (stateless JSON)
//
// Serves the plaintext team pool (Supabase) to claude.ai / mobile. The personal usage snapshot
// is E2EE, so it stays on the local MCP server — never here. Auth: an OAuth access token from
// the provider below, re-validated against the connector token it was minted from.

const MCP_RESOURCES = [
  { uri: POOL_UI_URI, name: "Team account pool", description: "Interactive account-pool dashboard.", mimeType: "text/html;profile=mcp-app" },
];

const MCP_TOOLS = [
  {
    name: "get_team_overview",
    description: "Your team's live Claude account POOL — per-account 5-hour/weekly load, month-to-date extra-usage spend, and accounts near a limit. Renders as an interactive widget where the host supports MCP Apps.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true, openWorldHint: false },
    _meta: { "ui": { "resourceUri": POOL_UI_URI } },
  },
  {
    name: "get_team_ledger",
    description: "Your team's per-account calendar-month extra-usage spend and tokens for a month.",
    inputSchema: {
      type: "object",
      properties: { month: { type: "string", description: "Month as YYYY-MM (defaults to the current month in the team's timezone)." } },
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
];

// Resolve the caller's Bearer to a team. Returns {team, tz}, null (missing/expired/revoked —
// answer 401 so the client re-authorises) or "upstream" (Supabase unreachable — answer 503).
async function mcpResolveTeam(request, env) {
  const bearer = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "").trim();
  if (!bearer) return null;
  // oauth_tokens.tid holds the team's email DOMAIN (bound at consent from the connector token).
  const tok = await env.DB.prepare("SELECT tid, exp, ct_hash FROM oauth_tokens WHERE token_hash=?1")
    .bind(await sha256hex(bearer)).first();
  // Rows without ct_hash predate revocation support; treat them as expired (one re-auth).
  if (!tok || !(tok.exp > Date.now()) || !tok.ct_hash) return null;
  let team;
  try {
    team = await connectorTeam(env, tok.ct_hash);
  } catch (e) {
    console.error("resolve_connector_token", e && (e.message || e));
    return "upstream";
  }
  if (!team || team !== tok.tid) {
    await revokeGrant(env, tok.ct_hash);
    return null;
  }
  return { team, tz: await teamTz(env, team) };
}

// Drop every access/refresh token minted from a connector token that no longer resolves.
async function revokeGrant(env, ctHash) {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM oauth_tokens WHERE ct_hash=?1").bind(ctHash),
    env.DB.prepare("DELETE FROM oauth_refresh WHERE ct_hash=?1").bind(ctHash),
  ]);
}

async function mcpCallTool(env, team, name, args) {
  if (name === "get_team_overview") {
    // Enrich to match the desktop: fetch this + previous month's ledger and merge in
    // per-account month €spend / tokens + KPIs, instead of raw today/yesterday.
    const ov = await sbTeamOverview(env, team.team, team.tz);
    const month = (ov.today || "").slice(0, 7);
    const merged = month
      ? overviewMerge(ov, await sbTeamLedger(env, team.team, month), await sbTeamLedger(env, team.team, prevMonth(month)))
      : ov;
    // structuredContent + the ui:// link → MCP-Apps hosts render the pool widget; the
    // text block is the graceful fallback for hosts without UI support.
    return {
      content: [{ type: "text", text: JSON.stringify(merged, null, 2) }],
      structuredContent: merged,
      _meta: { "ui": { "resourceUri": POOL_UI_URI } },
    };
  }
  if (name === "get_team_ledger") {
    const month = typeof args.month === "string" && /^\d{4}-(0[1-9]|1[0-2])$/.test(args.month)
      ? args.month : monthIn(team.tz);
    const led = await sbTeamLedger(env, team.team, month);
    const prev = await sbTeamLedger(env, team.team, prevMonth(month));
    led.spend = ledgerComputed(led, prev);            // per-account calendar-month € spend
    led.tokens = {};                                  // per-account month tokens
    for (const a of Object.keys(led.accounts || {})) led.tokens[a] = memberMonthTokens(led, a);
    return { content: [{ type: "text", text: JSON.stringify(led, null, 2) }] };
  }
  throw new Error(`unknown tool ${name}`);
}

async function handleMcp(request, env, url) {
  if (request.method !== "POST") {
    return new Response(JSON.stringify({ error: "method_not_allowed" }),
      { status: 405, headers: { "content-type": "application/json", allow: "POST" } });
  }
  // Every /mcp call must present a valid bearer. Without one, 401 + WWW-Authenticate so the
  // client discovers the OAuth server (MCP authorization spec) and re-authorises.
  const hasBearer = /^Bearer\s+\S/i.test(request.headers.get("authorization") || "");
  const team = await mcpResolveTeam(request, env);
  if (team === "upstream") {
    return json({ jsonrpc: "2.0", id: null, error: { code: -32603, message: "team directory temporarily unavailable" } }, 503,
      { "retry-after": "30" });
  }
  if (!team) {
    const rm = `resource_metadata="${url.origin}/.well-known/oauth-protected-resource"`;
    return json({ jsonrpc: "2.0", id: null, error: { code: -32001, message: "unauthorized" } }, 401, {
      "www-authenticate": hasBearer
        ? `Bearer error="invalid_token", error_description="token expired or revoked", ${rm}`
        : `Bearer ${rm}`,
    });
  }
  const text = await request.text();
  let msg;
  try {
    msg = text && text.length <= 1_000_000 ? JSON.parse(text) : null;
  } catch {
    return json({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "parse error" } }, 400);
  }
  const { status, body } = await dispatchRpc(msg, {
    serverInfo: { name: "claude-usage-tracker-team", version: "0.2.0" },
    tools: MCP_TOOLS,
    resources: MCP_RESOURCES,
    readResource: async (uri) => uri === POOL_UI_URI
      ? [{ uri, mimeType: "text/html;profile=mcp-app", text: POOL_WIDGET_HTML }] : null,
    callTool: (name, args) => mcpCallTool(env, team, name, args),
  });
  if (body === null) return new Response(null, { status: 202 });
  return json(body, status);
}

// ---- OAuth 2.1 provider (docs/MCP-REMOTE.md) -------------------------------
//
// What a claude.ai custom connector needs: metadata + Dynamic Client Registration + an
// authorization-code flow with PKCE (S256) + rotating refresh tokens. Consent asks for a
// connector token (minted by a team admin in the desktop app); the grant is bound to the team
// it resolves to in Supabase AND to the token itself (ct_hash), so revoking the connector
// token there revokes every access/refresh token minted from it.

const OAUTH_ACCESS_TTL_MS = 24 * 3600 * 1000;
const OAUTH_REFRESH_TTL_MS = 90 * 86400 * 1000;
const OAUTH_CODE_TTL_MS = 5 * 60 * 1000;
const OAUTH_CLIENT_UNUSED_TTL_MS = 7 * 86400 * 1000;
const REG_LIMIT_PER_HOUR = 10;

async function handleOAuth(request, env, url) {
  const p = url.pathname;
  const origin = url.origin;

  if (p === "/.well-known/oauth-authorization-server" && request.method === "GET") {
    return json({
      issuer: origin,
      authorization_endpoint: `${origin}/oauth/authorize`,
      token_endpoint: `${origin}/oauth/token`,
      registration_endpoint: `${origin}/oauth/register`,
      response_types_supported: ["code"],
      grant_types_supported: ["authorization_code", "refresh_token"],
      code_challenge_methods_supported: ["S256"],
      token_endpoint_auth_methods_supported: ["none"],
    });
  }
  if (p === "/.well-known/oauth-protected-resource" && request.method === "GET") {
    return json({
      resource: `${origin}/mcp`, authorization_servers: [origin],
      bearer_methods_supported: ["header"], resource_name: "Claude Usage Tracker — team pool",
    });
  }

  // Dynamic Client Registration (RFC 7591). Open by design (claude.ai self-registers), so it's
  // validated, rate-limited per client IP and pruned by the daily cron.
  if (p === "/oauth/register" && request.method === "POST") {
    const body = await readJson(request);
    if (!body || typeof body !== "object" || Array.isArray(body)) {
      return json({ error: "invalid_client_metadata", error_description: "JSON body required" }, 400);
    }
    const v = validateRedirectUris(body.redirect_uris);
    if (v.error) return json({ error: "invalid_redirect_uri", error_description: v.error }, 400);
    if (body.client_name !== undefined && (typeof body.client_name !== "string" || body.client_name.length > 200)) {
      return json({ error: "invalid_client_metadata", error_description: "client_name must be a string of at most 200 characters" }, 400);
    }
    const name = cleanName(body.client_name, 64) || "MCP client";
    // ≤10 registrations per IP per clock hour. One D1 upsert per attempt; the IP is stored
    // only as a truncated hash.
    const ip = request.headers.get("cf-connecting-ip") || "unknown";
    const ipKey = (await sha256hex(`reg:${ip}`)).slice(0, 32);
    const win = Math.floor(Date.now() / 3600_000);
    const rl = await env.DB.prepare(
      "INSERT INTO oauth_reg_rl(ip,win,n) VALUES(?1,?2,1) ON CONFLICT(ip,win) DO UPDATE SET n=n+1 RETURNING n"
    ).bind(ipKey, win).first();
    if (rl && rl.n > REG_LIMIT_PER_HOUR) {
      return json({ error: "rate_limited", error_description: "too many client registrations from this address; try again later" },
        429, { "retry-after": String(3600 - Math.floor((Date.now() / 1000) % 3600)) });
    }
    const clientId = randB64url(16);
    const now = Date.now();
    await env.DB.prepare("INSERT INTO oauth_clients(client_id,redirect_uris,name,created) VALUES(?1,?2,?3,?4)")
      .bind(clientId, JSON.stringify(v.uris), name, now).run();
    return json({
      client_id: clientId, client_id_issued_at: Math.floor(now / 1000), client_name: name, redirect_uris: v.uris,
      token_endpoint_auth_method: "none", grant_types: ["authorization_code", "refresh_token"], response_types: ["code"],
    }, 201);
  }

  // Authorization endpoint — consent page (GET) + submission (POST).
  if (p === "/oauth/authorize") {
    if (request.method !== "GET" && request.method !== "POST") return json({ error: "method_not_allowed" }, 405);
    const q = request.method === "POST" ? new URLSearchParams(await request.text()) : url.searchParams;
    const req = {
      clientId: q.get("client_id") || "",
      redirectUri: q.get("redirect_uri") || "",
      responseType: q.get("response_type") || "",
      challenge: q.get("code_challenge") || "",
      method: q.get("code_challenge_method") || "",
      state: q.get("state") || "",
    };
    const client = req.clientId && req.clientId.length <= 64
      ? await env.DB.prepare("SELECT redirect_uris, name FROM oauth_clients WHERE client_id=?1").bind(req.clientId).first()
      : null;
    let registered = [];
    try { registered = client ? JSON.parse(client.redirect_uris) : []; } catch { registered = []; }
    if (!client || !Array.isArray(registered) || !registered.includes(req.redirectUri)) {
      // Can't trust redirect_uri, so the error has to be shown here rather than sent back.
      return oauthPage({
        title: "Connection request not valid",
        body: `<h1>This connection request isn't valid</h1>
<p>The app asking to connect isn't registered with this tracker, or the address it wants to return to doesn't match its registration.</p>
<p>Close this tab and start again from the connector settings in Claude.</p>`,
        status: 400,
      });
    }
    // From here redirect_uri is trusted, so protocol errors go back to the client (RFC 6749 §4.1.2.1).
    if (req.responseType !== "code") return redirectTo(req.redirectUri, { error: "unsupported_response_type", state: req.state });
    if (req.method !== "S256" || !isValidChallenge(req.challenge)) {
      return redirectTo(req.redirectUri, {
        error: "invalid_request", error_description: "PKCE with code_challenge_method=S256 is required", state: req.state,
      });
    }

    const clientName = client.name || "MCP client";
    if (request.method === "GET") return consentPage(req, clientName);

    if (q.get("action") === "deny") return redirectTo(req.redirectUri, { error: "access_denied", state: req.state });

    // POST — validate the connector token via Supabase -> team email domain, mint a single-use code.
    const connectorToken = (q.get("connector_token") || "").trim();
    if (!connectorToken || connectorToken.length > 512) {
      return consentPage(req, clientName, "Paste your connector token to continue.", 400);
    }
    const ctHash = await sha256hex(connectorToken);
    let team;
    try {
      team = await connectorTeam(env, ctHash, false);
    } catch (e) {
      console.error("resolve_connector_token", e && (e.message || e));
      return consentPage(req, clientName, "Couldn't reach the team directory just now. Please try again in a moment.", 503);
    }
    if (!team) {
      return consentPage(req, clientName,
        "That connector token didn't work — it may be mistyped, expired or revoked. Ask a team admin to mint a new one and try again.", 403);
    }
    const code = randB64url(32);
    const now = Date.now();
    await env.DB.batch([
      env.DB.prepare(
        "INSERT INTO oauth_codes(code_hash,client_id,redirect_uri,code_challenge,tid,ct_hash,exp) VALUES(?1,?2,?3,?4,?5,?6,?7)"
      ).bind(await sha256hex(code), req.clientId, req.redirectUri, req.challenge, team, ctHash, now + OAUTH_CODE_TTL_MS),
      env.DB.prepare("UPDATE oauth_clients SET used=?2 WHERE client_id=?1").bind(req.clientId, now),
    ]);
    return redirectTo(req.redirectUri, { code, state: req.state });
  }

  // Token endpoint — authorization_code (+PKCE) and refresh_token (rotated on every use).
  if (p === "/oauth/token" && request.method === "POST") {
    const ct = request.headers.get("content-type") || "";
    let q;
    if (ct.includes("application/json")) {
      const b = await readJson(request);
      q = new URLSearchParams(Object.entries(b && typeof b === "object" ? b : {}).map(([k, v]) => [k, String(v)]));
    } else {
      q = new URLSearchParams(await request.text());
    }
    const grant = q.get("grant_type");
    const clientId = q.get("client_id") || "";

    if (grant === "authorization_code") {
      const code = q.get("code") || "";
      if (!code) return tokenError("invalid_request", "code required");
      // Single-use: DELETE … RETURNING consumes the code atomically, so it can never be replayed
      // (even by two concurrent requests).
      const row = await env.DB.prepare(
        "DELETE FROM oauth_codes WHERE code_hash=?1 RETURNING client_id,redirect_uri,code_challenge,tid,ct_hash,exp"
      ).bind(await sha256hex(code)).first();
      if (!row || !(row.exp > Date.now()) || row.client_id !== clientId
          || row.redirect_uri !== (q.get("redirect_uri") || "") || !row.ct_hash) {
        return tokenError("invalid_grant");
      }
      if (!(await verifyPkce(q.get("code_verifier") || "", row.code_challenge))) {
        return tokenError("invalid_grant", "PKCE verification failed");
      }
      return issueTokens(env, row.client_id, row.tid, row.ct_hash);
    }

    if (grant === "refresh_token") {
      const rt = q.get("refresh_token") || "";
      if (!rt) return tokenError("invalid_request", "refresh_token required");
      const rtHash = await sha256hex(rt);
      const row = await env.DB.prepare("SELECT client_id,tid,ct_hash,exp FROM oauth_refresh WHERE token_hash=?1")
        .bind(rtHash).first();
      if (!row || !(row.exp > Date.now()) || (clientId && clientId !== row.client_id)) return tokenError("invalid_grant");
      let team;
      try {
        team = await connectorTeam(env, row.ct_hash, false);
      } catch (e) {
        console.error("resolve_connector_token", e && (e.message || e));
        return json({ error: "temporarily_unavailable" }, 503, { "retry-after": "30", "cache-control": "no-store" });
      }
      if (!team || team !== row.tid) {
        await revokeGrant(env, row.ct_hash);
        return tokenError("invalid_grant", "the connector token behind this grant was revoked or expired");
      }
      // Rotate: consume the presented refresh token atomically; a lost race (or replay) fails.
      const used = await env.DB.prepare("DELETE FROM oauth_refresh WHERE token_hash=?1 RETURNING token_hash")
        .bind(rtHash).first();
      if (!used) return tokenError("invalid_grant");
      return issueTokens(env, row.client_id, row.tid, row.ct_hash);
    }

    return tokenError("unsupported_grant_type");
  }

  return json({ error: "not_found" }, 404);
}

async function issueTokens(env, clientId, tid, ctHash) {
  const access = randB64url(32);
  const refresh = randB64url(32);
  const now = Date.now();
  await env.DB.batch([
    env.DB.prepare("INSERT INTO oauth_tokens(token_hash,tid,ct_hash,exp) VALUES(?1,?2,?3,?4)")
      .bind(await sha256hex(access), tid, ctHash, now + OAUTH_ACCESS_TTL_MS),
    env.DB.prepare("INSERT INTO oauth_refresh(token_hash,client_id,tid,ct_hash,exp) VALUES(?1,?2,?3,?4,?5)")
      .bind(await sha256hex(refresh), clientId, tid, ctHash, now + OAUTH_REFRESH_TTL_MS),
    env.DB.prepare("UPDATE oauth_clients SET used=?2 WHERE client_id=?1").bind(clientId, now),
  ]);
  return json({
    access_token: access, token_type: "Bearer", expires_in: Math.floor(OAUTH_ACCESS_TTL_MS / 1000),
    refresh_token: refresh,
  }, 200, { "cache-control": "no-store", pragma: "no-cache" });
}

function tokenError(error, description) {
  return json(description ? { error, error_description: description } : { error }, 400,
    { "cache-control": "no-store", pragma: "no-cache" });
}

function redirectTo(redirectUri, params) {
  const u = new URL(redirectUri);
  for (const [k, v] of Object.entries(params)) if (v) u.searchParams.set(k, v);
  return new Response(null, { status: 302, headers: { location: u.toString(), "cache-control": "no-store" } });
}

// ---- Consent + error pages --------------------------------------------------

const PAGE_CSS = `
:root{color-scheme:light dark;--bg:#faf7f2;--card:#fff;--fg:#221d18;--muted:#5f574d;--line:#ddd5c9;--accent:#b4532f;--accent-fg:#fff;--input:#fff;--err-bg:#fdecea;--err-fg:#8a2414}
@media (prefers-color-scheme:dark){:root{--bg:#100e0c;--card:#1a1612;--fg:#f2ede5;--muted:#b3a99c;--line:#3a332b;--accent:#e08a66;--accent-fg:#1c0f08;--input:#14110e;--err-bg:#3a1a12;--err-fg:#ffb4a2}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:480px;margin:6vh auto;padding:24px;background:var(--card);border:1px solid var(--line);border-radius:14px}
@media (max-width:520px){main{margin:0;border-radius:0;border-left:0;border-right:0;min-height:100vh}}
h1{font-size:20px;line-height:1.3;margin:0 0 12px}
p{margin:0 0 12px}
.muted{color:var(--muted);font-size:14px}
.err{background:var(--err-bg);color:var(--err-fg);padding:10px 12px;border-radius:8px;font-size:14px}
label{display:block;font-weight:600;margin:16px 0 6px}
input[type=password]{width:100%;padding:11px 12px;border-radius:8px;border:1px solid var(--line);background:var(--input);color:var(--fg);font:14px ui-monospace,SFMono-Regular,Menlo,monospace}
input:focus-visible,button:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
.help{color:var(--muted);font-size:13px;margin-top:8px}
.actions{display:flex;gap:10px;margin-top:18px;flex-wrap:wrap}
button{padding:11px 18px;border-radius:8px;font:600 15px system-ui,sans-serif;cursor:pointer;border:1px solid transparent}
button.primary{background:var(--accent);color:var(--accent-fg)}
button.secondary{background:transparent;color:var(--fg);border-color:var(--line)}
button[disabled]{opacity:.7;cursor:progress}
b.host{overflow-wrap:anywhere}
`;

function oauthPage({ title, body, status = 200, script = "" }) {
  const nonce = randB64url(16);
  const doc = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><meta name="referrer" content="no-referrer">
<title>${escapeHtml(title)}</title><style>${PAGE_CSS}</style></head>
<body><main>${body}</main>${script ? `<script nonce="${nonce}">${script}</script>` : ""}</body></html>`;
  return new Response(doc, {
    status,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "x-frame-options": "DENY",
      "x-content-type-options": "nosniff",
      "referrer-policy": "no-referrer",
      "content-security-policy":
        `default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}'; base-uri 'none'; frame-ancestors 'none'`,
    },
  });
}

const CONSENT_JS = `document.getElementById("f").addEventListener("submit",function(e){
var s=e.submitter;if(s&&s.value==="deny")return;var b=document.getElementById("allow");
setTimeout(function(){b.disabled=true;b.textContent="Connecting\\u2026";},0);});`;

function consentPage(req, clientName, error = "", status = 200) {
  let host = "";
  try { host = new URL(req.redirectUri).host; } catch { host = req.redirectUri; }
  const hid = (n, v) => `<input type="hidden" name="${n}" value="${escapeHtml(v)}">`;
  const name = escapeHtml(clientName);
  return oauthPage({
    title: "Connect to your team's usage pool",
    status,
    script: CONSENT_JS,
    body: `<h1>Allow <span>${name}</span> to read your team's usage pool?</h1>
<p><b>${name}</b> is asking for read-only access to your team's shared Claude accounts: each account's 5-hour and weekly usage and this month's extra-usage spend. It can't change anything.</p>
<p class="muted">If you allow it, you'll be sent back to <b class="host">${escapeHtml(host)}</b>.</p>
${error ? `<p class="err" role="alert">${escapeHtml(error)}</p>` : ""}
<form method="post" action="/oauth/authorize" id="f">
${hid("client_id", req.clientId)}${hid("redirect_uri", req.redirectUri)}${hid("response_type", "code")}${hid("code_challenge", req.challenge)}${hid("code_challenge_method", req.method)}${hid("state", req.state)}
<label for="ct">Connector token</label>
<input id="ct" name="connector_token" type="password" autocomplete="off" spellcheck="false" autocapitalize="off" required aria-describedby="ct-help"${error ? ' aria-invalid="true"' : ""} autofocus>
<p id="ct-help" class="help">A team admin creates this in the Claude Usage Tracker desktop app: open the dashboard, go to the <b>Team</b> tab, and under <b>claude.ai connector</b> click <b>Mint connector token</b>. Paste it here. It's checked against your team and isn't stored in readable form.</p>
<div class="actions">
<button type="submit" name="action" value="allow" id="allow" class="primary">Allow access</button>
<button type="submit" name="action" value="deny" class="secondary" formnovalidate>Deny</button>
</div>
</form>`,
  });
}

// ---- FCM HTTP v1 ----------------------------------------------------------

async function sendFcm(env, accessToken, token, blob) {
  const msg = {
    message: {
      token,
      android: { priority: "HIGH" },
      data: { v: "1", nonce: blob.nonce, ct: blob.ct },
    },
  };
  const resp = await fetch(
    `https://fcm.googleapis.com/v1/projects/${env.FCM_PROJECT_ID}/messages:send`,
    {
      method: "POST",
      headers: { authorization: `Bearer ${accessToken}`, "content-type": "application/json" },
      body: JSON.stringify(msg),
    }
  );
  if (resp.ok) return true;
  if (resp.status === 404 || resp.status === 410) return "gone"; // unregistered token
  return false;
}

async function googleAccessToken(env) {
  const cached = await env.KV.get("gtoken");
  if (cached) {
    const o = JSON.parse(cached);
    if (o.exp - 60 > Math.floor(Date.now() / 1000)) return o.token;
  }
  if (!env.FCM_CLIENT_EMAIL || !env.FCM_PRIVATE_KEY || !env.FCM_PROJECT_ID) {
    throw new Error("FCM secrets not set");
  }
  const now = Math.floor(Date.now() / 1000);
  const claim = {
    iss: env.FCM_CLIENT_EMAIL,
    scope: "https://www.googleapis.com/auth/firebase.messaging",
    aud: "https://oauth2.googleapis.com/token",
    iat: now,
    exp: now + 3600,
  };
  const jwt = await signJwt(claim, env.FCM_PRIVATE_KEY);
  const resp = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
      assertion: jwt,
    }),
  });
  if (!resp.ok) throw new Error(`token endpoint ${resp.status}`);
  const tok = await resp.json();
  const exp = now + (tok.expires_in || 3600);
  await env.KV.put("gtoken", JSON.stringify({ token: tok.access_token, exp }), {
    expirationTtl: tok.expires_in || 3600,
  });
  return tok.access_token;
}

async function signJwt(claim, pem) {
  const enc = (obj) => b64urlBytes(new TextEncoder().encode(JSON.stringify(obj)));
  const head = enc({ alg: "RS256", typ: "JWT" });
  const body = enc(claim);
  const data = `${head}.${body}`;
  const key = await crypto.subtle.importKey(
    "pkcs8",
    pemToDer(pem),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(data));
  return `${data}.${b64urlBytes(new Uint8Array(sig))}`;
}

// ---- helpers --------------------------------------------------------------

async function readBlob(request) {
  const b = await readJson(request);
  if (!b || typeof b.nonce !== "string" || typeof b.ct !== "string") return null;
  return { v: b.v || 1, nonce: b.nonce, ct: b.ct, ts: b.ts || Math.floor(Date.now() / 1000) };
}
async function readJson(request) {
  try {
    const t = await request.text();
    if (!t || t.length > 2_000_000) return null;
    return JSON.parse(t);
  } catch {
    return null;
  }
}
function json(obj, status = 200, headers = {}) {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json", ...headers } });
}
function cors(resp) {
  const h = new Headers(resp.headers);
  h.set("access-control-allow-origin", "*");
  h.set("access-control-allow-methods", "GET,PUT,POST,DELETE,OPTIONS");
  h.set("access-control-allow-headers", "authorization,content-type,mcp-protocol-version,mcp-session-id");
  h.set("access-control-expose-headers", "www-authenticate");
  return new Response(resp.body, { status: resp.status, headers: h });
}
function pemToDer(pem) {
  const clean = pem
    .replace(/\\n/g, "\n")
    .replace(/-----BEGIN [^-]+-----/, "")
    .replace(/-----END [^-]+-----/, "")
    .replace(/\s+/g, "");
  const bin = atob(clean);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return buf.buffer;
}
