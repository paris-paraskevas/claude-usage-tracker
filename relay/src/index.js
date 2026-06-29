/**
 * Claude Usage Tracker — remote relay (Cloudflare Worker).
 *
 * A zero-knowledge dumb pipe between the desktop tracker (producer) and the
 * Android app (consumer). It stores and forwards END-TO-END-ENCRYPTED blobs and
 * never sees the e2eeKey or any plaintext. See docs/REMOTE.md for the contract.
 *
 * Bindings: KV namespace `KV`.
 * Secrets (for push): FCM_PROJECT_ID, FCM_CLIENT_EMAIL, FCM_PRIVATE_KEY.
 */

const WRITE_MIN_GAP_MS = 8000;     // per-account throttle for snapshot writes
const SNAPSHOT_TTL_S = 7 * 86400;  // forget stale accounts that stop syncing

export default {
  async fetch(request, env, ctx) {
    try {
      return await handle(request, env, ctx);
    } catch (err) {
      return json({ error: "internal", detail: String(err && err.message || err) }, 500);
    }
  },
};

async function handle(request, env, ctx) {
  const url = new URL(request.url);
  const { method } = request;

  if (method === "OPTIONS") return cors(new Response(null, { status: 204 }));
  if (url.pathname === "/" || url.pathname === "/health") {
    return cors(json({ ok: true, service: "claude-usage-relay", v: 1 }));
  }

  // /v1/acct/{accountId}/{resource}
  const m = url.pathname.match(/^\/v1\/acct\/([A-Za-z0-9_-]{8,64})\/(snapshot|push-token|push)$/);
  if (!m) return cors(json({ error: "not_found" }, 404));
  const accountId = m[1];
  const resource = m[2];

  const bearer = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "").trim();
  if (!bearer) return cors(json({ error: "unauthorized" }, 401));
  const bearerHash = await sha256hex(bearer);

  const authKey = `auth:${accountId}`;
  const storedHash = await env.KV.get(authKey);

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
      const last = await env.KV.get(`wts:${accountId}`);
      if (last && Date.now() - Number(last) < WRITE_MIN_GAP_MS) {
        return cors(json({ error: "rate_limited" }, 429));
      }
      const blob = await readBlob(request);
      if (!blob) return cors(json({ error: "bad_blob" }, 400));
      if (isFirstWrite) await env.KV.put(authKey, bearerHash, { expirationTtl: SNAPSHOT_TTL_S });
      await env.KV.put(`snapshot:${accountId}`, JSON.stringify(blob), { expirationTtl: SNAPSHOT_TTL_S });
      await env.KV.put(`wts:${accountId}`, String(Date.now()), { expirationTtl: 3600 });
      // refresh auth TTL so an actively-syncing account never expires
      if (storedHash) await env.KV.put(authKey, bearerHash, { expirationTtl: SNAPSHOT_TTL_S });
      return cors(new Response(null, { status: 204 }));
    }
    if (method === "GET") {
      const v = await env.KV.get(`snapshot:${accountId}`);
      if (!v) return cors(new Response(null, { status: 204 }));
      return cors(new Response(v, { status: 200, headers: { "content-type": "application/json" } }));
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

  return cors(json({ error: "method_not_allowed" }, 405));
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
  const enc = (obj) => b64url(new TextEncoder().encode(JSON.stringify(obj)));
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
function b64url(bytes) {
  return b64urlBytes(bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes));
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
