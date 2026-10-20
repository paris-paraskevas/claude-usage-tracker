// Pure helpers for the relay Worker. Kept in their own module (no bindings, no I/O beyond
// WebCrypto) so `node --test` can import them directly — see relay/test/.

export const DEFAULT_TEAM_TZ = "Europe/Athens";

// ---- time ------------------------------------------------------------------

export function validTz(tz) {
  if (typeof tz !== "string" || !tz || tz.length > 64) return null;
  try {
    new Intl.DateTimeFormat("en", { timeZone: tz });
    return tz;
  } catch {
    return null;
  }
}

// Local wall-clock parts for a tz, DST-correct.
export function tzParts(tz, d = new Date(Date.now())) {
  const fmt = new Intl.DateTimeFormat("en-CA", {
    timeZone: validTz(tz) || DEFAULT_TEAM_TZ, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  const p = {};
  for (const { type, value } of fmt.formatToParts(d)) p[type] = value;
  return {
    date: `${p.year}-${p.month}-${p.day}`,
    y: +p.year, m: +p.month, d: +p.day, hour: +p.hour, minute: +p.minute,
  };
}

// The current YYYY-MM in a team's tz (not UTC — around midnight on the 1st they differ).
export function monthIn(tz, d = new Date(Date.now())) {
  return tzParts(tz, d).date.slice(0, 7);
}

export function prevMonth(month) {
  const y = +month.slice(0, 4), m = +month.slice(5, 7);
  return m === 1 ? `${y - 1}-12` : `${y}-${String(m - 1).padStart(2, "0")}`;
}

// ---- Supabase pool rows -> the client shape the merge fns parse ------------

// A Supabase usage/finals row -> client shape. `device` is the stable device id (the old `did`);
// display_name -> name.
export function rowFromSb(r) {
  if (!r) return null;
  const ee = r.extra_enabled;
  return {
    name: r.display_name, fh_pct: r.fh_pct, sd_pct: r.sd_pct,
    fh_resets_at: r.fh_resets_at, sd_resets_at: r.sd_resets_at,
    extra: (ee === null || ee === undefined) ? null
      : { enabled: !!ee, used: r.extra_used, limit: r.extra_limit, currency: r.extra_currency, pct: r.extra_pct },
    did: r.device, device: r.device, by_name: r.by_name, tok_month: r.tok_month, ts: r.ts, src: r.src,
  };
}

// The authoritative account-level row for an account's day: an explicit 'account' row if
// present, else the newest device push. Mirrored in Python (_day_account_row).
export function pickAccountRow(devmap) {
  if (!devmap || typeof devmap !== "object") return null;
  if (devmap.account) return devmap.account;
  let best = null;
  for (const did of Object.keys(devmap)) {
    const r = devmap[did];
    if (r && (!best || (r.ts || 0) > (best.ts || 0))) best = r;
  }
  return best;
}

// "Last used by whom/where/when" for a pooled account: the newest device push.
// Returns {ts, device, by} or null.
export function lastUsed(devmap) {
  if (!devmap || typeof devmap !== "object") return null;
  let best = null;
  for (const did of Object.keys(devmap)) {
    if (did === "account") continue;
    const r = devmap[did];
    if (r && (!best || (r.ts || 0) > (best.ts || 0))) best = r;
  }
  return best ? { ts: best.ts, device: best.device, by: best.by_name } : null;
}

// get_team_usage rows (today + yesterday) -> the overview shape.
export function buildOverview(team, tz, today, yesterday, rows) {
  const names = {};
  const byDate = { [today]: {}, [yesterday]: {} };
  for (const r of rows || []) {
    names[r.acct] = r.display_name || r.acct;
    const d = (byDate[r.date] = byDate[r.date] || {});
    (d[r.acct] = d[r.acct] || {})[r.device] = rowFromSb(r);
  }
  const accounts = Object.keys(names).map((acct) => {
    const tdev = byDate[today][acct] || {};
    const ydev = byDate[yesterday][acct] || {};
    return {
      acct, name: names[acct],
      account: pickAccountRow(tdev) || pickAccountRow(ydev),
      account_is_today: !!pickAccountRow(tdev),
      last_used: lastUsed(tdev) || lastUsed(ydev),
      devices: Object.keys(tdev).map((d) => ({ did: d, ...tdev[d] })),
    };
  });
  return { team, tz, today, accounts };
}

// get_team_month rows ({kind, r}) -> the ledger shape.
export function buildLedger(team, month, rows) {
  const names = {}, days = {}, finals = {};
  for (const row of rows || []) {
    const r = (row && row.r) || {};
    if (!r.acct) continue;
    names[r.acct] = r.display_name || r.acct;
    if (row.kind === "final") {
      finals[r.acct] = rowFromSb(r);
    } else {
      const d = (days[r.date] = days[r.date] || {});
      (d[r.acct] = d[r.acct] || {})[r.device] = rowFromSb(r);
    }
  }
  return { team, month, accounts: names, days, finals };
}

// ---- Computed team metrics — mirrors the Python merge in claude_usage_tracker.py so the
// remote MCP tools return the same enriched shape (per-account month €spend + tokens + KPIs)
// the desktop computes locally. Keep semantics identical to the Python originals
// (team_month_spend / member_month_tokens / team_ledger_computed / team_overview_merge) —
// the two must agree or claude.ai and the desktop diverge.

// An account's account-level extra.used values for a ledger month, in date order.
export function ledgerSamples(led, acct) {
  const vals = [], days = led.days || {};
  for (const date of Object.keys(days).sort()) {
    const row = pickAccountRow((days[date] || {})[acct]);
    const used = row && row.extra && row.extra.used;
    if (typeof used === "number" && isFinite(used)) vals.push(used);
  }
  return vals;
}

// Python's round(v, 2): rounds the double's exact decimal value, ties to even — so the
// connector's numbers match the desktop's to the cent (Math.round(v*100)/100 drifts on values
// like 1.105, whose double is just below the tie).
export function round2(v) {
  if (!isFinite(v)) return v;
  const exact = Math.abs(v).toFixed(100);          // exact expansion (doubles terminate)
  const [ip, fp] = exact.split(".");
  let cents = BigInt(ip) * 100n + BigInt(fp.slice(0, 2));
  const rest = fp.slice(2);
  const tie = rest[0] === "5" && /^0*$/.test(rest.slice(1));
  if (rest[0] > "5" || (rest[0] === "5" && !tie) || (tie && cents % 2n === 1n)) cents += 1n;
  const r = Number(cents) / 100;
  return v < 0 ? -r : r;
}

// Calendar-month spend from day-by-day extra.used samples: sum day-over-day increases;
// a drop means the billing cycle reset in the gap, so the new value is all fresh spend.
// baseline = previous month's last sample (seeds the first diff); null = seed only.
export function monthSpend(samples, baseline) {
  let spend = 0, prev = baseline;
  for (const v of samples) {
    if (typeof v !== "number" || !isFinite(v)) continue;
    if (prev === null || prev === undefined) { /* unknown history: seed only */ }
    else if (v >= prev) spend += v - prev;
    else spend += v;
    prev = v;
  }
  return round2(spend);
}

// An account's tokens this month: the tok_month of its LATEST push in the month
// (tok_month is month-cumulative, so the freshest sample is the total).
export function memberMonthTokens(led, acct) {
  const days = led.days || {};
  let bestTs = null, bestTok = 0;
  for (const date of Object.keys(days)) {
    const devmap = (days[date] || {})[acct] || {};
    for (const did of Object.keys(devmap)) {
      if (did === "account") continue;
      const row = devmap[did];
      if (!row || typeof row.tok_month !== "number" || !isFinite(row.tok_month)) continue;
      const ts = row.ts || 0;
      if (bestTs === null || ts > bestTs) { bestTs = ts; bestTok = Math.floor(row.tok_month); }
    }
  }
  return bestTok;
}

// The value to diff the month's first sample against: the previous month's frozen final
// if one was captured, else its last daily row.
export function ledgerBaseline(prevLed, acct) {
  if (!prevLed) return null;
  const f = ((prevLed.finals || {})[acct] || {}).extra || {};
  if (typeof f.used === "number" && isFinite(f.used)) return f.used;
  const prior = ledgerSamples(prevLed, acct);
  return prior.length ? prior[prior.length - 1] : null;
}

// Per-account calendar-month spend for a ledger response.
export function ledgerComputed(led, prevLed) {
  const accts = new Set(Object.keys(led.accounts || {}));
  for (const d of Object.values(led.days || {})) for (const a of Object.keys(d)) accts.add(a);
  const out = {};
  for (const a of accts) out[a] = monthSpend(ledgerSamples(led, a), ledgerBaseline(prevLed, a));
  return out;
}

// Attach month spend/tokens per account + KPI aggregates to an overview. Pure — matches
// team_overview_merge so the connector shows what the desktop does. Spend is totalled per
// currency (org_spend_by_currency); org_spend/org_currency are set only when every account
// bills in one currency — different currencies are never summed (both null otherwise).
export function overviewMerge(ov, led, prevLed) {
  const out = { ...ov };
  const accounts = (ov.accounts || []).map((a) => ({ ...a }));
  const spend = led ? ledgerComputed(led, prevLed) : {};
  const near = [];
  let byCur = {};
  for (const a of accounts) {
    a.month_spend = spend[a.acct] ?? null;
    a.month_tokens = led ? memberMonthTokens(led, a.acct) : 0;
    const row = a.account || {};
    if (typeof a.month_spend === "number") {
      const cur = (row.extra || {}).currency || "";
      byCur[cur] = (byCur[cur] || 0) + a.month_spend;
    }
    for (const [key, label] of [["fh_pct", "5h"], ["sd_pct", "weekly"]]) {
      const p = row[key];
      if (typeof p === "number" && p >= 80) near.push({ name: a.name, window: label, pct: p });
    }
  }
  near.sort((x, y) => y.pct - x.pct);
  const keys = Object.keys(byCur);
  if ("" in byCur && (keys.length > 1 || !byCur[""])) {
    const unknown = byCur[""];
    delete byCur[""];                       // zero spend with no currency (overage off) is noise
    const rest = Object.keys(byCur);
    if (unknown && rest.length === 1) byCur[rest[0]] += unknown;   // unlabeled + one currency: same bill
    else if (unknown) byCur[""] = unknown;
  }
  byCur = Object.fromEntries(Object.entries(byCur).map(([k, v]) => [k, round2(v)]));
  const curs = Object.keys(byCur);
  const single = curs.length <= 1;
  out.accounts = accounts;
  out.kpis = {
    org_spend: single ? round2(Object.values(byCur).reduce((s, v) => s + v, 0)) : null,
    org_currency: single ? (curs[0] ?? "") : null,
    org_spend_by_currency: byCur,
    account_count: accounts.length,
    near,
  };
  return out;
}

// ---- strings / HTML --------------------------------------------------------

export function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Trimmed, control-char-free, length-capped display string ("" if not a string).
export function cleanName(v, max = 64) {
  if (typeof v !== "string") return "";
  // eslint-disable-next-line no-control-regex
  return v.replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g, " ").trim().slice(0, max).trim();
}

// ---- OAuth validation ------------------------------------------------------

export const MAX_REDIRECT_URIS = 5;
export const MAX_REDIRECT_URI_LEN = 512;
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

// RFC 7591 redirect_uris check: 1..5 absolute URIs, each ≤512 chars, https only (plain http
// allowed for loopback dev clients), no fragment, no embedded credentials.
// Returns { uris } on success or { error } with a human-readable reason.
export function validateRedirectUris(input) {
  if (!Array.isArray(input) || input.length === 0) return { error: "redirect_uris must be a non-empty array" };
  if (input.length > MAX_REDIRECT_URIS) return { error: `at most ${MAX_REDIRECT_URIS} redirect_uris are allowed` };
  const out = [];
  for (const u of input) {
    if (typeof u !== "string" || !u) return { error: "each redirect_uri must be a string" };
    if (u.length > MAX_REDIRECT_URI_LEN) return { error: `redirect_uri longer than ${MAX_REDIRECT_URI_LEN} characters` };
    let parsed;
    try { parsed = new URL(u); } catch { return { error: "redirect_uri is not an absolute URL" }; }
    if (parsed.hash || u.includes("#")) return { error: "redirect_uri must not contain a fragment" };
    if (parsed.username || parsed.password) return { error: "redirect_uri must not contain credentials" };
    const httpsOk = parsed.protocol === "https:";
    const loopbackOk = parsed.protocol === "http:" && LOOPBACK_HOSTS.has(parsed.hostname);
    if (!httpsOk && !loopbackOk) return { error: "redirect_uri must use https (http is allowed only for localhost/127.0.0.1)" };
    if (!out.includes(u)) out.push(u);
  }
  return { uris: out };
}

// RFC 7636: S256 challenge = BASE64URL(SHA256(verifier)) → always 43 chars of [A-Za-z0-9_-].
export function isValidChallenge(s) {
  return typeof s === "string" && /^[A-Za-z0-9_-]{43}$/.test(s);
}
// Verifier: 43-128 chars of the unreserved set.
export function isValidVerifier(s) {
  return typeof s === "string" && /^[A-Za-z0-9._~-]{43,128}$/.test(s);
}
export async function verifyPkce(verifier, challenge) {
  if (!isValidVerifier(verifier) || !isValidChallenge(challenge)) return false;
  return timingSafeEqual(await sha256b64url(verifier), challenge);
}

// ---- crypto / encoding -----------------------------------------------------

export async function sha256hex(s) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
export async function sha256b64url(s) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return b64urlBytes(new Uint8Array(d));
}
export function randB64url(bytes = 32) {
  return b64urlBytes(crypto.getRandomValues(new Uint8Array(bytes)));
}
export function b64urlBytes(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
export function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

// ---- MCP JSON-RPC (Streamable HTTP, stateless JSON responses) --------------

// Newest first. initialize echoes the client's requested version when we support it, else
// answers with our latest (the client then decides whether it can proceed).
export const MCP_PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"];

export function negotiateProtocol(requested) {
  return MCP_PROTOCOL_VERSIONS.includes(requested) ? requested : MCP_PROTOCOL_VERSIONS[0];
}

const rpcError = (id, code, message) => ({ jsonrpc: "2.0", id: id ?? null, error: { code, message } });

// Dispatch one parsed JSON-RPC message (or a batch array). Returns { status, body } where
// body === null means "202 Accepted, no body" (notifications and client responses).
// `h` supplies: serverInfo, tools, resources, readResource(uri) -> contents|null,
// callTool(name, args) -> result (may throw; a throw becomes an isError tool result).
export async function dispatchRpc(msg, h) {
  if (Array.isArray(msg)) {
    if (!msg.length) return { status: 400, body: rpcError(null, -32600, "invalid request") };
    const out = [];
    for (const m of msg) {
      const r = await dispatchRpc(m, h);
      if (r.body) out.push(r.body);
    }
    return out.length ? { status: 200, body: out } : { status: 202, body: null };
  }
  if (!msg || typeof msg !== "object" || msg.jsonrpc !== "2.0") {
    return { status: 400, body: rpcError(msg && msg.id, -32600, "invalid request") };
  }
  // A client-sent JSON-RPC *response* (e.g. to a server request) — nothing to do.
  if (typeof msg.method !== "string") {
    if ("result" in msg || "error" in msg) return { status: 202, body: null };
    return { status: 400, body: rpcError(msg.id, -32600, "invalid request") };
  }
  // Notifications carry no id and never get a response (notifications/initialized,
  // notifications/cancelled, …) — answer 202 with an empty body.
  if (!("id" in msg) || msg.id === undefined) return { status: 202, body: null };

  const id = msg.id;
  const params = (msg.params && typeof msg.params === "object") ? msg.params : {};
  const ok = (result) => ({ status: 200, body: { jsonrpc: "2.0", id, result } });

  switch (msg.method) {
    case "initialize":
      return ok({
        protocolVersion: negotiateProtocol(params.protocolVersion),
        capabilities: { tools: {}, resources: {} },
        serverInfo: h.serverInfo,
      });
    case "ping":
      return ok({});
    case "tools/list":
      return ok({ tools: h.tools });
    case "resources/list":
      return ok({ resources: h.resources });
    case "resources/templates/list":
      return ok({ resourceTemplates: [] });
    case "resources/read": {
      const contents = await h.readResource(params.uri);
      if (!contents) return { status: 200, body: rpcError(id, -32002, "resource not found") };
      return ok({ contents });
    }
    case "tools/call": {
      const name = params.name;
      if (!h.tools.some((t) => t.name === name)) {
        return ok({ content: [{ type: "text", text: `Unknown tool: ${String(name)}` }], isError: true });
      }
      const args = (params.arguments && typeof params.arguments === "object") ? params.arguments : {};
      try {
        return ok(await h.callTool(name, args));
      } catch (e) {
        console.error("mcp tool error", name, e && (e.stack || e.message || e));
        return ok({
          content: [{ type: "text", text: "Couldn't read the team pool right now (upstream error). Try again shortly." }],
          isError: true,
        });
      }
    }
    default:
      return { status: 200, body: rpcError(id, -32601, "method not found") };
  }
}
