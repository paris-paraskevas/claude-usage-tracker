/**
 * Claude Usage Tracker — remote relay (Cloudflare Worker).
 *
 * Two independent features share this Worker:
 *
 * 1. Phone sync (`/v1/acct/...`) — a zero-knowledge dumb pipe between the desktop
 *    tracker (producer) and the Android app (consumer). Stores and forwards
 *    END-TO-END-ENCRYPTED blobs and never sees the e2eeKey or any plaintext.
 *    See docs/REMOTE.md for the contract.
 *
 * 2. Team mode (`/v1/team/...`) — an admin-owned aggregator for a Claude Team
 *    plan. Members push compact PLAINTEXT usage rows (numbers only — never
 *    conversation content), and may opt in to escrowing their short-lived OAuth
 *    access token so the nightly cron can capture the 23:59 ledger row even when
 *    their machine is off. Escrowed tokens are sealed with AES-GCM under the
 *    TEAM_SEAL_KEY secret and are never returned by any route.
 *    See docs/TEAM.md for the contract.
 *
 * Bindings: KV namespace `KV`.
 * Secrets: FCM_PROJECT_ID, FCM_CLIENT_EMAIL, FCM_PRIVATE_KEY (phone push),
 *          TEAM_SEAL_KEY (32 bytes base64; enables team token escrow).
 */

const WRITE_MIN_GAP_MS = 5000;             // per-account snapshot-write floor (~10s sync; D1-backed)
const SNAPSHOT_TTL_S = 7 * 86400;          // forget stale accounts that stop syncing
const AUTH_REFRESH_GAP_MS = 86400 * 1000;  // refresh the auth key's 7-day TTL at most once/day, not every push

const REPORT_MIN_GAP_MS = 5000;            // per-device team-report write floor (~10s reporting; D1-backed)
const DAY_ROW_TTL_S = 400 * 86400;         // keep ~13 months of daily rows (month finals never expire)
const DEFAULT_TEAM_TZ = "Europe/Athens";
const USAGE_URL = "https://api.anthropic.com/api/oauth/usage";
// Mirrors what the Claude Code CLI sends for OAuth requests (same as the desktop app).
const OAUTH_HEADERS = {
  "anthropic-beta": "oauth-2025-04-20",
  "anthropic-version": "2023-06-01",
  "User-Agent": "claude-cli/2.0.0 (external, cli)",
  "Accept": "application/json",
};

export default {
  async fetch(request, env, ctx) {
    try {
      return await handle(request, env, ctx);
    } catch (err) {
      return json({ error: "internal", detail: String(err && err.message || err) }, 500);
    }
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil(teamCron(env));
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

  const tm = url.pathname.match(/^\/v1\/team\/([A-Za-z0-9_-]{8,64})(\/.*)?$/);
  if (tm) return cors(await handleTeam(request, env, url, tm[1], tm[2] || ""));

  // /v1/acct/{accountId}/{resource}
  const m = url.pathname.match(/^\/v1\/acct\/([A-Za-z0-9_-]{8,64})\/(snapshot|push-token|push|command)$/);
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
    const body = await readJson(request);
    const token = body && typeof body.token === "string" ? body.token : null;
    if (!token) return cors(json({ error: "bad_token" }, 400));
    const setKey = `tokens:${accountId}`;
    const cur = JSON.parse((await env.KV.get(setKey)) || "[]");
    const set = new Set(cur);
    if (method === "PUT") set.add(token);
    else if (method === "DELETE") set.delete(token);
    else return cors(json({ error: "method" }, 405));
    await env.KV.put(setKey, JSON.stringify([...set]), { expirationTtl: SNAPSHOT_TTL_S });
    return cors(new Response(null, { status: 204 }));
  }

  if (resource === "push" && method === "POST") {
    const blob = await readBlob(request);
    if (!blob) return cors(json({ error: "bad_blob" }, 400));
    const setKey = `tokens:${accountId}`;
    const tokens = JSON.parse((await env.KV.get(setKey)) || "[]");
    if (!tokens.length) return cors(json({ sent: 0 }));
    let access;
    try {
      access = await googleAccessToken(env);
    } catch (e) {
      return cors(json({ error: "fcm_unconfigured", detail: String(e.message || e) }, 503));
    }
    const dead = [];
    let sent = 0;
    for (const token of tokens) {
      const ok = await sendFcm(env, access, token, blob);
      if (ok === "gone") dead.push(token);
      else if (ok === true) sent++;
    }
    if (dead.length) {
      const left = tokens.filter((t) => !dead.includes(t));
      await env.KV.put(setKey, JSON.stringify(left), { expirationTtl: SNAPSHOT_TTL_S });
    }
    return cors(json({ sent }));
  }

  // Phone -> desktop command channel (E2EE blob; e.g. a prompt to run). The phone PUTs a
  // command, the desktop GETs then DELETEs it. One pending command at a time; 5-min TTL.
  if (resource === "command") {
    const key = `cmd:${accountId}`;
    if (method === "PUT") {
      const blob = await readBlob(request);
      if (!blob) return cors(json({ error: "bad_blob" }, 400));
      await env.KV.put(key, JSON.stringify(blob), { expirationTtl: 300 });
      return cors(new Response(null, { status: 204 }));
    }
    if (method === "GET") {
      const v = await env.KV.get(key);
      if (!v) return cors(new Response(null, { status: 204 }));
      return cors(new Response(v, { status: 200, headers: { "content-type": "application/json" } }));
    }
    if (method === "DELETE") {
      await env.KV.delete(key);
      return cors(new Response(null, { status: 204 }));
    }
  }

  return cors(json({ error: "method_not_allowed" }, 405));
}

// ---- Team mode (docs/TEAM.md) ---------------------------------------------
//
// Storage: Cloudflare D1 (SQLite), schema in relay/schema.sql —
//   teams(tid, admin_hash, tz, org)          · members(tid, mid, token_hash, name)
//   usage_rows(tid, date, mid, did, …)        one row per member DEVICE per LOCAL day;
//                                             did='account' is the cron's account row
//   finals(tid, month, mid, …)                frozen month-end row (never pruned)
//   escrow(tid, mid, iv, ct, exp)             sealed OAuth access token
// Phone sync stays in KV. The HTTP contract is unchanged from the KV era, so the
// desktop/phone clients don't change — this is purely a storage swap.

async function handleTeam(request, env, url, tid, sub) {
  const { method } = request;
  const bearer = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "").trim();
  if (!bearer) return json({ error: "unauthorized" }, 401);
  const bearerHash = await sha256hex(bearer);

  const adm = await env.DB.prepare("SELECT admin_hash, tz, org FROM teams WHERE tid=?").bind(tid).first();
  const isAdmin = !!(adm && timingSafeEqual(adm.admin_hash, bearerHash));

  // POST /init — trust-on-first-use pin of the admin bearer; idempotent for the admin.
  if (sub === "/init" && method === "POST") {
    const body = (await readJson(request)) || {};
    const tz = validTz(body.tz) || (adm && adm.tz) || DEFAULT_TEAM_TZ;
    const org = typeof body.org === "string" && body.org.length <= 64 ? body.org : (adm && adm.org) || null;
    if (adm && !isAdmin) return json({ error: "forbidden" }, 403);
    await env.DB.prepare(
      "INSERT INTO teams(tid,admin_hash,tz,org) VALUES(?1,?2,?3,?4) " +
      "ON CONFLICT(tid) DO UPDATE SET admin_hash=?2, tz=?3, org=?4"
    ).bind(tid, bearerHash, tz, org).run();
    return json({ ok: true, tz, org });
  }
  if (!adm) return json({ error: "unknown_team" }, 404);

  // /member/{mid}[ /report | /token ]
  const mm = sub.match(/^\/member\/([A-Za-z0-9_-]{8,64})(\/report|\/token)?$/);
  if (mm) {
    const mid = mm[1];
    const leaf = mm[2] || "";

    if (leaf === "") {
      // Registry management — admin only. The admin mints the member token and
      // registers its hash here, so there is no first-come TOFU race for members.
      if (!isAdmin) return json({ error: "forbidden" }, 403);
      if (method === "PUT") {
        const body = await readJson(request);
        const hash = body && typeof body.token_hash === "string" && /^[0-9a-f]{64}$/.test(body.token_hash)
          ? body.token_hash : null;
        if (!hash) return json({ error: "bad_member" }, 400);
        const name = cleanName(body.name) || mid.slice(0, 8);
        await env.DB.prepare(
          "INSERT INTO members(tid,mid,token_hash,name) VALUES(?1,?2,?3,?4) " +
          "ON CONFLICT(tid,mid) DO UPDATE SET token_hash=?3, name=?4"
        ).bind(tid, mid, hash, name).run();
        return new Response(null, { status: 204 });
      }
      if (method === "DELETE") {
        // Drop the registry entry + escrow; ledger rows are kept for history.
        await env.DB.batch([
          env.DB.prepare("DELETE FROM members WHERE tid=? AND mid=?").bind(tid, mid),
          env.DB.prepare("DELETE FROM escrow WHERE tid=? AND mid=?").bind(tid, mid),
        ]);
        return new Response(null, { status: 204 });
      }
      return json({ error: "method" }, 405);
    }

    // Member-authenticated leaves.
    const mem = await env.DB.prepare("SELECT token_hash, name FROM members WHERE tid=? AND mid=?")
      .bind(tid, mid).first();
    if (!mem) return json({ error: "unknown_member" }, 404);
    if (!timingSafeEqual(mem.token_hash, bearerHash)) return json({ error: "forbidden" }, 403);

    if (leaf === "/report" && method === "PUT") {
      const body = await readJson(request);
      const did = body && typeof body.did === "string" && /^[A-Za-z0-9_-]{4,64}$/.test(body.did)
        ? body.did : null;
      const row = sanitizeReport(body, mem.name);
      // Device rows are keyed per did, distinct from the cron's reserved `account`
      // row, so a device push and the 23:59 cron row can coexist without clobbering.
      if (!row || !did || did === "account") return json({ error: "bad_report" }, 400);
      const date = tzParts(adm.tz).date;
      const prev = await env.DB.prepare("SELECT wts FROM usage_rows WHERE tid=? AND date=? AND mid=? AND did=?")
        .bind(tid, date, mid, did).first();
      if (prev && prev.wts && Date.now() - prev.wts < REPORT_MIN_GAP_MS) {
        return json({ error: "rate_limited" }, 429);
      }
      row.src = "push";
      await env.DB.prepare(USAGE_UPSERT).bind(...usageBind(tid, date, mid, did, row, Date.now())).run();
      return new Response(null, { status: 204 });
    }

    if (leaf === "/token") {
      if (method === "DELETE") {
        await env.DB.prepare("DELETE FROM escrow WHERE tid=? AND mid=?").bind(tid, mid).run();
        return new Response(null, { status: 204 });
      }
      if (method !== "PUT") return json({ error: "method" }, 405);
      if (!env.TEAM_SEAL_KEY) return json({ error: "escrow_unconfigured" }, 503);
      const body = await readJson(request);
      const tok = body && typeof body.access_token === "string" ? body.access_token : null;
      const exp = body && Number(body.expires_at) || 0; // epoch ms
      if (!tok || tok.length > 4096 || exp <= Date.now()) return json({ error: "bad_token" }, 400);
      // Org binding: a token escrowed to this team must belong to the team's org.
      // Defeats a leaked join code being used from an outside account.
      if (adm.org) {
        let prof;
        try {
          const pr = await fetch("https://api.anthropic.com/api/oauth/profile", {
            headers: { ...OAUTH_HEADERS, Authorization: `Bearer ${tok}` },
          });
          if (!pr.ok) return json({ error: "verify_failed" }, 403);
          prof = await pr.json();
        } catch {
          return json({ error: "verify_failed" }, 403);
        }
        const org = prof && prof.organization && prof.organization.uuid;
        if (org !== adm.org) return json({ error: "wrong_org" }, 403);
      }
      const sealed = await seal(env.TEAM_SEAL_KEY, tok);
      await env.DB.prepare(
        "INSERT INTO escrow(tid,mid,iv,ct,exp) VALUES(?1,?2,?3,?4,?5) " +
        "ON CONFLICT(tid,mid) DO UPDATE SET iv=?3, ct=?4, exp=?5"
      ).bind(tid, mid, sealed.iv, sealed.ct, exp).run();
      return new Response(null, { status: 204 });
    }
  }

  // Admin read endpoints.
  if (!isAdmin) return json({ error: "forbidden" }, 403);

  if (sub === "/overview" && method === "GET") {
    return json(await teamOverviewData(env, tid, adm.tz || DEFAULT_TEAM_TZ));
  }

  if (sub === "/ledger" && method === "GET") {
    const month = (url.searchParams.get("month") || "").trim();
    if (!/^\d{4}-\d{2}$/.test(month)) return json({ error: "bad_month" }, 400);
    return json(await teamLedgerData(env, tid, month));
  }

  return json({ error: "not_found" }, 404);
}

// Shared team reads — used by the HTTP admin routes above and the remote MCP tools below.
async function teamOverviewData(env, tid, tz) {
  const now = new Date();
  const today = tzParts(tz, now).date;
  const yesterday = tzParts(tz, new Date(now.getTime() - 86400_000)).date;
  const [mRes, eRes, uRes] = await env.DB.batch([
    env.DB.prepare("SELECT mid,name FROM members WHERE tid=?").bind(tid),
    env.DB.prepare("SELECT mid,exp FROM escrow WHERE tid=? AND exp>?").bind(tid, Date.now()),
    env.DB.prepare("SELECT * FROM usage_rows WHERE tid=? AND date IN (?,?)").bind(tid, today, yesterday),
  ]);
  const esc = {};
  for (const r of eRes.results) esc[r.mid] = r.exp;
  const byDate = { [today]: {}, [yesterday]: {} };
  for (const r of uRes.results) {
    const d = (byDate[r.date] = byDate[r.date] || {});
    (d[r.mid] = d[r.mid] || {})[r.did] = rowFromDb(r);
  }
  const members = mRes.results.map((m) => {
    const tdev = (byDate[today] && byDate[today][m.mid]) || {};
    const ydev = (byDate[yesterday] && byDate[yesterday][m.mid]) || {};
    const devices = Object.keys(tdev).filter((d) => d !== "account").map((d) => ({ did: d, ...tdev[d] }));
    return {
      mid: m.mid, name: m.name || m.mid.slice(0, 8),
      account: pickAccountRow(tdev) || pickAccountRow(ydev),
      account_is_today: !!pickAccountRow(tdev), devices,
      escrow: m.mid in esc ? { present: true, exp: esc[m.mid] } : { present: false },
    };
  });
  return { team: tid, tz, today, members };
}

async function teamLedgerData(env, tid, month) {
  const [mRes, uRes, fRes] = await env.DB.batch([
    env.DB.prepare("SELECT mid,name FROM members WHERE tid=?").bind(tid),
    env.DB.prepare("SELECT * FROM usage_rows WHERE tid=? AND date LIKE ?").bind(tid, month + "-%"),
    env.DB.prepare("SELECT * FROM finals WHERE tid=? AND month=?").bind(tid, month),
  ]);
  const names = {};
  for (const m of mRes.results) names[m.mid] = m.name || m.mid.slice(0, 8);
  const days = {};
  for (const r of uRes.results) {
    const d = (days[r.date] = days[r.date] || {});
    (d[r.mid] = d[r.mid] || {})[r.did] = rowFromDb(r);
  }
  const finals = {};
  for (const r of fRes.results) finals[r.mid] = rowFromDb(r);
  return { team: tid, month, members: names, days, finals };
}

// ---- Remote MCP (docs/MCP-REMOTE.md) — team tools over Streamable HTTP (stateless JSON) --
//
// Serves the plaintext team data (D1) to claude.ai / mobile. The personal usage snapshot is
// E2EE, so it stays on the local MCP server — never here. Interim auth: Bearer = the team
// admin token (hashed, matched to a team). The claude.ai OAuth layer (DCR + PKCE) is added on
// top next; it will mint tokens that resolve to a tid the same way.

const MCP_TOOLS = [
  {
    name: "get_team_overview",
    description: "Team admin: live per-member 5h/weekly load, month-to-date spend, and near-limit members.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "get_team_ledger",
    description: "Team admin: per-member calendar-month extra-usage spend and tokens for a month.",
    inputSchema: {
      type: "object",
      properties: { month: { type: "string", description: "Month as YYYY-MM (defaults to the current month)." } },
      additionalProperties: false,
    },
  },
];

// Resolve the caller's Bearer to a team ({tid, tz}) — an OAuth access token or, as a
// fallback, the team admin token directly. Returns null if neither matches / is expired.
async function mcpResolveTeam(request, env) {
  const bearer = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "").trim();
  if (!bearer) return null;
  const hash = await sha256hex(bearer);
  const tok = await env.DB.prepare("SELECT tid, exp FROM oauth_tokens WHERE token_hash=?").bind(hash).first();
  if (tok && tok.exp > Date.now()) {
    const t = await env.DB.prepare("SELECT tid, tz FROM teams WHERE tid=?").bind(tok.tid).first();
    if (t) return t;
  }
  return (await env.DB.prepare("SELECT tid, tz FROM teams WHERE admin_hash=?").bind(hash).first()) || null;
}

async function handleMcp(request, env, url) {
  // Auth first, for ANY method: an unauthenticated probe (GET or POST) must get 401 +
  // WWW-Authenticate so the client discovers the OAuth server (MCP authorization spec).
  const team = await mcpResolveTeam(request, env);
  if (!team) {
    return new Response(
      JSON.stringify({ jsonrpc: "2.0", id: null, error: { code: -32001, message: "unauthorized" } }),
      { status: 401, headers: {
        "content-type": "application/json",
        "www-authenticate": `Bearer resource_metadata="${url.origin}/.well-known/oauth-protected-resource"`,
      } });
  }
  if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);
  const rpc = await readJson(request);
  if (!rpc || rpc.jsonrpc !== "2.0" || typeof rpc.method !== "string") {
    return json({ jsonrpc: "2.0", id: (rpc && rpc.id) || null, error: { code: -32600, message: "invalid request" } }, 400);
  }
  const reply = (result) => json({ jsonrpc: "2.0", id: rpc.id, result });

  if (rpc.method === "initialize") {
    return reply({ protocolVersion: "2024-11-05", capabilities: { tools: {} },
      serverInfo: { name: "claude-usage-tracker-team", version: "0.1.0" } });
  }
  if (rpc.method === "notifications/initialized") return new Response(null, { status: 202 });
  if (rpc.method === "tools/list") return reply({ tools: MCP_TOOLS });
  if (rpc.method === "tools/call") {
    const name = rpc.params && rpc.params.name;
    const args = (rpc.params && rpc.params.arguments) || {};
    const asText = (obj) => reply({ content: [{ type: "text", text: JSON.stringify(obj, null, 2) }] });
    try {
      if (name === "get_team_overview") return asText(await teamOverviewData(env, team.tid, team.tz || DEFAULT_TEAM_TZ));
      if (name === "get_team_ledger") {
        const month = /^\d{4}-\d{2}$/.test(args.month || "") ? args.month : new Date().toISOString().slice(0, 7);
        return asText(await teamLedgerData(env, team.tid, month));
      }
      return reply({ content: [{ type: "text", text: `Unknown tool: ${name}` }], isError: true });
    } catch (e) {
      return reply({ content: [{ type: "text", text: `Tool error: ${String((e && e.message) || e)}` }], isError: true });
    }
  }
  return json({ jsonrpc: "2.0", id: rpc.id ?? null, error: { code: -32601, message: "method not found" } });
}

// usage_rows / finals row  ->  the client JSON shape the desktop parses (unchanged
// from the KV era). extra_enabled NULL means "no extra block".
function rowFromDb(r) {
  if (!r) return null;
  return {
    name: r.name, fh_pct: r.fh_pct, sd_pct: r.sd_pct,
    fh_resets_at: r.fh_resets_at, sd_resets_at: r.sd_resets_at,
    extra: (r.extra_enabled === null || r.extra_enabled === undefined) ? null
      : { enabled: !!r.extra_enabled, used: r.extra_used, limit: r.extra_limit, currency: r.extra_currency, pct: r.extra_pct },
    did: r.did, device: r.device, tok_month: r.tok_month, ts: r.ts, src: r.src,
  };
}

const USAGE_UPSERT =
  "INSERT INTO usage_rows(tid,date,mid,did,name,fh_pct,sd_pct,fh_resets_at,sd_resets_at," +
  "extra_enabled,extra_used,extra_limit,extra_currency,extra_pct,tok_month,device,src,ts,wts) " +
  "VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19) " +
  "ON CONFLICT(tid,date,mid,did) DO UPDATE SET name=?5,fh_pct=?6,sd_pct=?7,fh_resets_at=?8,sd_resets_at=?9," +
  "extra_enabled=?10,extra_used=?11,extra_limit=?12,extra_currency=?13,extra_pct=?14,tok_month=?15,device=?16,src=?17,ts=?18,wts=?19";

function usageBind(tid, date, mid, did, row, wts) {
  const e = row.extra;
  return [tid, date, mid, did, row.name, row.fh_pct, row.sd_pct, row.fh_resets_at, row.sd_resets_at,
    e ? (e.enabled ? 1 : 0) : null, e ? e.used : null, e ? e.limit : null, e ? e.currency : null, e ? e.pct : null,
    row.tok_month ?? null, row.device ?? null, row.src ?? null, row.ts ?? null, wts ?? null];
}

const FINAL_UPSERT =
  "INSERT INTO finals(tid,month,mid,name,fh_pct,sd_pct,fh_resets_at,sd_resets_at," +
  "extra_enabled,extra_used,extra_limit,extra_currency,extra_pct,tok_month,ts) " +
  "VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15) " +
  "ON CONFLICT(tid,month,mid) DO UPDATE SET name=?4,fh_pct=?5,sd_pct=?6,fh_resets_at=?7,sd_resets_at=?8," +
  "extra_enabled=?9,extra_used=?10,extra_limit=?11,extra_currency=?12,extra_pct=?13,tok_month=?14,ts=?15";

function finalBind(tid, month, mid, row) {
  const e = row.extra;
  return [tid, month, mid, row.name, row.fh_pct, row.sd_pct, row.fh_resets_at, row.sd_resets_at,
    e ? (e.enabled ? 1 : 0) : null, e ? e.used : null, e ? e.limit : null, e ? e.currency : null, e ? e.pct : null,
    row.tok_month ?? null, row.ts ?? null];
}

// The authoritative account-level row for a member's day: the cron's row if
// present, else the newest device push. Mirrored in Python (_day_account_row).
function pickAccountRow(devmap) {
  if (!devmap || typeof devmap !== "object") return null;
  if (devmap.account) return devmap.account;
  let best = null;
  for (const did of Object.keys(devmap)) {
    const r = devmap[did];
    if (r && (!best || (r.ts || 0) > (best.ts || 0))) best = r;
  }
  return best;
}

// Compact plaintext usage row a member pushes: display numbers only, no content.
function sanitizeReport(body, fallbackName) {
  if (!body || typeof body !== "object") return null;
  const pct = (v) => (typeof v === "number" && isFinite(v) ? Math.max(0, Math.min(100, v)) : null);
  const money = (v) => (typeof v === "number" && isFinite(v) && v >= 0 && v < 1e7 ? Math.round(v * 100) / 100 : null);
  const row = {
    name: cleanName(body.name) || fallbackName,
    fh_pct: pct(body.fh_pct),
    sd_pct: pct(body.sd_pct),
    fh_resets_at: cleanTs(body.fh_resets_at),
    sd_resets_at: cleanTs(body.sd_resets_at),
    extra: null,
    ts: Number(body.ts) > 0 ? Math.floor(Number(body.ts)) : Math.floor(Date.now() / 1000),
  };
  if (body.extra && typeof body.extra === "object") {
    row.extra = {
      enabled: !!body.extra.enabled,
      used: money(body.extra.used),
      limit: money(body.extra.limit),
      currency: String(body.extra.currency || "").slice(0, 8),
      pct: pct(body.extra.pct),
    };
  }
  row.did = typeof body.did === "string" ? body.did.slice(0, 64) : null;
  row.device = cleanName(body.device) || null;
  row.tok_month = typeof body.tok_month === "number" && isFinite(body.tok_month) && body.tok_month >= 0
    ? Math.floor(body.tok_month) : null;
  if (row.fh_pct === null && row.sd_pct === null && !row.extra) return null;
  return row;
}

function cleanName(v) {
  return typeof v === "string" ? v.trim().slice(0, 64) : "";
}
function cleanTs(v) {
  return typeof v === "string" && v.length <= 40 ? v : null;
}
function validTz(tz) {
  if (typeof tz !== "string" || !tz) return null;
  try {
    new Intl.DateTimeFormat("en", { timeZone: tz });
    return tz;
  } catch {
    return null;
  }
}

// Local wall-clock parts for a tz, DST-correct (the cron's "is it 23:59 in Athens" test).
function tzParts(tz, d = new Date()) {
  const fmt = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  const p = {};
  for (const { type, value } of fmt.formatToParts(d)) p[type] = value;
  return {
    date: `${p.year}-${p.month}-${p.day}`,
    y: +p.year, m: +p.month, d: +p.day, hour: +p.hour, minute: +p.minute,
  };
}

// ---- Team cron: the 23:59 ledger capture -----------------------------------
//
// Fires at 20:59 and 21:59 UTC (wrangler.toml). Exactly one of the two lands on
// 23:59 in a UTC+2/+3 zone depending on DST; each team is checked against its own
// tz, so other zones just need cron entries that cover their offset.

async function teamCron(env) {
  // Housekeeping (runs regardless of the seal key): prune day rows past the retention
  // window and expired escrow. D1 has no TTL, so this replaces KV's automatic expiry.
  const cutoff = new Date(Date.now() - DAY_ROW_TTL_S * 1000).toISOString().slice(0, 10);
  await env.DB.batch([
    env.DB.prepare("DELETE FROM usage_rows WHERE date < ?").bind(cutoff),
    env.DB.prepare("DELETE FROM escrow WHERE exp < ?").bind(Date.now()),
    env.DB.prepare("DELETE FROM snapshots WHERE exp < ?").bind(Date.now()),  // KV used to auto-expire these
  ]);
  if (!env.TEAM_SEAL_KEY) return;

  const teams = (await env.DB.prepare("SELECT tid, tz FROM teams").all()).results;
  for (const t of teams) {
    const tz = t.tz || DEFAULT_TEAM_TZ;
    const local = tzParts(tz);
    if (local.hour !== 23 || local.minute < 50) continue; // not this team's end-of-day
    const lastDay = local.d === new Date(Date.UTC(local.y, local.m, 0)).getUTCDate();
    const month = local.date.slice(0, 7);

    const escrowed = (await env.DB.prepare(
      "SELECT e.mid AS mid, e.iv AS iv, e.ct AS ct, m.name AS name " +
      "FROM escrow e JOIN members m ON e.tid=m.tid AND e.mid=m.mid WHERE e.tid=? AND e.exp>?"
    ).bind(t.tid, Date.now()).all()).results;

    for (const em of escrowed) {
      let token;
      try {
        token = await unseal(env.TEAM_SEAL_KEY, { iv: em.iv, ct: em.ct });
      } catch {
        continue;
      }
      const row = await fetchUsageRow(token);
      if (row === "dead") {
        await env.DB.prepare("DELETE FROM escrow WHERE tid=? AND mid=?").bind(t.tid, em.mid).run(); // revoked
        continue;
      }
      if (!row) continue; // transient failure: keep the pushed rows
      row.name = em.name || em.mid.slice(0, 8);
      row.src = "cron";
      // Reserved device id 'account': an account-level row distinct from any device's
      // push row, so both survive on the same day.
      await env.DB.prepare(USAGE_UPSERT).bind(...usageBind(t.tid, local.date, em.mid, "account", row, Date.now())).run();
      if (lastDay) {
        await env.DB.prepare(FINAL_UPSERT).bind(...finalBind(t.tid, month, em.mid, row)).run();
      }
    }
  }
}

// GET /api/oauth/usage with an escrowed token → a report row, "dead" on auth
// rejection, or null on transient failure.
async function fetchUsageRow(token) {
  let resp;
  try {
    resp = await fetch(USAGE_URL, {
      headers: { ...OAUTH_HEADERS, Authorization: `Bearer ${token}` },
    });
  } catch {
    return null;
  }
  if (resp.status === 401 || resp.status === 403) return "dead";
  if (!resp.ok) return null;
  let data;
  try {
    data = await resp.json();
  } catch {
    return null;
  }
  const pct = (w) => (w && typeof w.utilization === "number" ? Math.max(0, Math.min(100, w.utilization)) : null);
  const row = {
    fh_pct: pct(data.five_hour),
    sd_pct: pct(data.seven_day),
    fh_resets_at: (data.five_hour && data.five_hour.resets_at) || null,
    sd_resets_at: (data.seven_day && data.seven_day.resets_at) || null,
    extra: null,
    ts: Math.floor(Date.now() / 1000),
  };
  const minor = (o) => (o && typeof o.amount_minor === "number" && typeof o.exponent === "number"
    ? o.amount_minor / 10 ** o.exponent : null);
  const spend = data.spend;
  const eu = data.extra_usage;
  if (spend && spend.enabled) {
    row.extra = {
      enabled: true,
      used: minor(spend.used),
      limit: minor(spend.limit),
      currency: (spend.used && spend.used.currency) || (spend.limit && spend.limit.currency) || "",
      pct: typeof spend.percent === "number" ? spend.percent : null,
    };
  } else if (eu && eu.is_enabled) {
    const dp = typeof eu.decimal_places === "number" ? eu.decimal_places : 2;
    row.extra = {
      enabled: true,
      used: typeof eu.used_credits === "number" ? eu.used_credits / 10 ** dp : null,
      limit: typeof eu.monthly_limit === "number" ? eu.monthly_limit / 10 ** dp : null,
      currency: eu.currency || "",
      pct: typeof eu.utilization === "number" ? eu.utilization : null,
    };
  }
  return row;
}

// ---- AES-GCM sealing for escrowed tokens ------------------------------------

async function sealKey(b64) {
  const raw = Uint8Array.from(atob(b64.trim()), (c) => c.charCodeAt(0));
  return crypto.subtle.importKey("raw", raw, { name: "AES-GCM" }, false, ["encrypt", "decrypt"]);
}

async function seal(keyB64, plaintext) {
  const key = await sealKey(keyB64);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, new TextEncoder().encode(plaintext));
  return { iv: b64urlBytes(iv), ct: b64urlBytes(new Uint8Array(ct)) };
}

async function unseal(keyB64, sealed) {
  const key = await sealKey(keyB64);
  const iv = b64urlToBytes(sealed.iv);
  const ct = b64urlToBytes(sealed.ct);
  const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, key, ct);
  return new TextDecoder().decode(pt);
}

function b64urlToBytes(s) {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

// ---- OAuth 2.1 provider (docs/MCP-REMOTE.md) -------------------------------
//
// The minimum a claude.ai custom connector needs: metadata + Dynamic Client Registration
// + an authorization-code flow with PKCE. Consent authenticates the team admin (they paste
// their admin token), so the issued token is bound to exactly their team (tid).

const OAUTH_TOKEN_TTL_MS = 30 * 86400 * 1000;
const OAUTH_CODE_TTL_MS = 5 * 60 * 1000;

async function sha256b64url(s) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return b64urlBytes(new Uint8Array(d));
}
function randB64url(bytes = 32) {
  return b64urlBytes(crypto.getRandomValues(new Uint8Array(bytes)));
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function html(body, status = 200) {
  return new Response(body, { status, headers: { "content-type": "text/html; charset=utf-8" } });
}

async function handleOAuth(request, env, url) {
  const p = url.pathname;
  const origin = url.origin;

  if ((p === "/.well-known/oauth-authorization-server" || p === "/.well-known/oauth-authorization-server/mcp") && request.method === "GET") {
    return json({
      issuer: origin,
      authorization_endpoint: `${origin}/oauth/authorize`,
      token_endpoint: `${origin}/oauth/token`,
      registration_endpoint: `${origin}/oauth/register`,
      response_types_supported: ["code"],
      grant_types_supported: ["authorization_code"],
      code_challenge_methods_supported: ["S256"],
      token_endpoint_auth_methods_supported: ["none"],
    });
  }
  if ((p === "/.well-known/oauth-protected-resource" || p === "/.well-known/oauth-protected-resource/mcp") && request.method === "GET") {
    return json({ resource: `${origin}/mcp`, authorization_servers: [origin] });
  }

  // Dynamic Client Registration (RFC 7591).
  if (p === "/oauth/register" && request.method === "POST") {
    const body = (await readJson(request)) || {};
    const uris = Array.isArray(body.redirect_uris) ? body.redirect_uris.filter((u) => typeof u === "string") : [];
    if (!uris.length) return json({ error: "invalid_redirect_uri", error_description: "redirect_uris required" }, 400);
    const clientId = randB64url(16);
    await env.DB.prepare("INSERT INTO oauth_clients(client_id,redirect_uris,name,created) VALUES(?1,?2,?3,?4)")
      .bind(clientId, JSON.stringify(uris), cleanName(body.client_name) || "MCP client", Date.now()).run();
    return json({
      client_id: clientId, redirect_uris: uris,
      token_endpoint_auth_method: "none", grant_types: ["authorization_code"], response_types: ["code"],
    }, 201);
  }

  // Authorization endpoint — consent page (GET) + submission (POST).
  if (p === "/oauth/authorize") {
    const q = request.method === "POST" ? await request.text().then((t) => new URLSearchParams(t)) : url.searchParams;
    const clientId = q.get("client_id") || "";
    const redirectUri = q.get("redirect_uri") || "";
    const challenge = q.get("code_challenge") || "";
    const method = q.get("code_challenge_method") || "";
    const state = q.get("state") || "";
    const client = await env.DB.prepare("SELECT redirect_uris FROM oauth_clients WHERE client_id=?").bind(clientId).first();
    const registered = client ? JSON.parse(client.redirect_uris) : [];
    if (!client || !registered.includes(redirectUri)) return html("<h3>Invalid client or redirect_uri.</h3>", 400);
    if (method !== "S256" || !challenge) return html("<h3>PKCE (code_challenge_method=S256) is required.</h3>", 400);

    if (request.method === "GET") {
      const hid = (n, v) => `<input type="hidden" name="${n}" value="${escapeHtml(v)}">`;
      return html(`<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Connect Claude to your team tracker</title>
<body style="font:15px system-ui;max-width:460px;margin:8vh auto;padding:0 20px;background:#100e0c;color:#f2ede5">
<h2 style="color:#d97757">Connect your team tracker</h2>
<p style="color:#a99f93">Paste your <b>team admin token</b> to let Claude read this team's analytics. It's matched to your team and never shown again.</p>
<form method="POST" action="/oauth/authorize">
${hid("client_id", clientId)}${hid("redirect_uri", redirectUri)}${hid("code_challenge", challenge)}${hid("code_challenge_method", method)}${hid("state", state)}${hid("response_type", "code")}
<input name="admin_token" type="password" placeholder="team admin token" autocomplete="off" required
  style="width:100%;box-sizing:border-box;padding:11px;border-radius:8px;border:1px solid #332e28;background:#1e1a16;color:#f2ede5;font:13px monospace">
<button type="submit" style="margin-top:12px;padding:11px 18px;border:0;border-radius:8px;background:#d97757;color:#1c0f08;font-weight:600;cursor:pointer">Authorize</button>
</form></body>`);
    }

    // POST — validate the admin token → tid, mint a single-use code.
    const adminToken = (q.get("admin_token") || "").trim();
    if (!adminToken) return html("<h3>Missing admin token.</h3>", 400);
    const team = await env.DB.prepare("SELECT tid FROM teams WHERE admin_hash=?").bind(await sha256hex(adminToken)).first();
    if (!team) {
      return html(`<h3 style="color:#d4694f">That admin token doesn't match a team.</h3><p><a href="javascript:history.back()">Try again</a></p>`, 403);
    }
    const code = randB64url(32);
    await env.DB.prepare(
      "INSERT INTO oauth_codes(code_hash,client_id,redirect_uri,code_challenge,tid,exp) VALUES(?1,?2,?3,?4,?5,?6)"
    ).bind(await sha256hex(code), clientId, redirectUri, challenge, team.tid, Date.now() + OAUTH_CODE_TTL_MS).run();
    const sep = redirectUri.includes("?") ? "&" : "?";
    const loc = `${redirectUri}${sep}code=${encodeURIComponent(code)}${state ? "&state=" + encodeURIComponent(state) : ""}`;
    return new Response(null, { status: 302, headers: { location: loc } });
  }

  // Token endpoint — authorization_code + PKCE.
  if (p === "/oauth/token" && request.method === "POST") {
    const ct = request.headers.get("content-type") || "";
    const q = ct.includes("application/json")
      ? new URLSearchParams(Object.entries((await readJson(request)) || {}).map(([k, v]) => [k, String(v)]))
      : new URLSearchParams(await request.text());
    if (q.get("grant_type") !== "authorization_code") return json({ error: "unsupported_grant_type" }, 400);
    const code = q.get("code") || "";
    const verifier = q.get("code_verifier") || "";
    const redirectUri = q.get("redirect_uri") || "";
    const clientId = q.get("client_id") || "";
    const row = await env.DB.prepare("SELECT client_id,redirect_uri,code_challenge,tid,exp FROM oauth_codes WHERE code_hash=?")
      .bind(await sha256hex(code)).first();
    // Single-use: delete on any lookup hit so a code can never be replayed.
    if (row) await env.DB.prepare("DELETE FROM oauth_codes WHERE code_hash=?").bind(await sha256hex(code)).run();
    if (!row || row.exp < Date.now() || row.client_id !== clientId || row.redirect_uri !== redirectUri) {
      return json({ error: "invalid_grant" }, 400);
    }
    if (!verifier || (await sha256b64url(verifier)) !== row.code_challenge) {
      return json({ error: "invalid_grant", error_description: "PKCE verification failed" }, 400);
    }
    const token = randB64url(32);
    const exp = Date.now() + OAUTH_TOKEN_TTL_MS;
    await env.DB.prepare("INSERT INTO oauth_tokens(token_hash,tid,exp) VALUES(?1,?2,?3)")
      .bind(await sha256hex(token), row.tid, exp).run();
    return json({ access_token: token, token_type: "Bearer", expires_in: Math.floor(OAUTH_TOKEN_TTL_MS / 1000) });
  }

  return json({ error: "not_found" }, 404);
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
function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });
}
function cors(resp) {
  const h = new Headers(resp.headers);
  h.set("access-control-allow-origin", "*");
  h.set("access-control-allow-methods", "GET,PUT,POST,DELETE,OPTIONS");
  h.set("access-control-allow-headers", "authorization,content-type");
  return new Response(resp.body, { status: resp.status, headers: h });
}
async function sha256hex(s) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}
function b64urlBytes(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
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
