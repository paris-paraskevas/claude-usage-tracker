// Test doubles for the Worker bindings — no Workers runtime needed.
//   fakeD1(sql)  — the D1 API surface the relay uses (prepare/bind/first/all/run + batch), backed
//                  by node:sqlite so the real schema.sql and the real SQL statements are exercised.
//   fakeKV()     — get/getWithMetadata/put/delete with metadata, counting writes.
//   fakeSupabase — a fetch() stub answering the service_role RPC/REST calls the relay makes.
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

export const SCHEMA = readFileSync(fileURLToPath(new URL("../schema.sql", import.meta.url)), "utf8");

export function fakeD1(sql = SCHEMA) {
  const db = new DatabaseSync(":memory:");
  db.exec(sql);
  const plain = (r) => (r ? { ...r } : null);
  const stmt = (text, args = []) => ({
    bind: (...a) => stmt(text, a),
    async first(col) {
      const r = plain(db.prepare(text).get(...args));
      return r && col ? r[col] : r;
    },
    async all() {
      return { success: true, results: db.prepare(text).all(...args).map(plain) };
    },
    async run() {
      const info = db.prepare(text).run(...args);
      return { success: true, meta: { changes: Number(info.changes) } };
    },
  });
  return {
    sqlite: db,
    prepare: (text) => stmt(text),
    async batch(stmts) {
      db.exec("BEGIN");
      try {
        const out = [];
        for (const s of stmts) out.push(await s.all());
        db.exec("COMMIT");
        return out;
      } catch (e) {
        db.exec("ROLLBACK");
        throw e;
      }
    },
  };
}

export function fakeKV() {
  const m = new Map();
  return {
    map: m,
    writes: 0,
    async get(k) { return m.has(k) ? m.get(k).value : null; },
    async getWithMetadata(k) {
      const e = m.get(k);
      return { value: e ? e.value : null, metadata: e ? e.metadata : null };
    },
    async put(k, v, opts = {}) {
      this.writes++;
      m.set(k, { value: String(v), metadata: opts.metadata ?? null, ttl: opts.expirationTtl });
    },
    async delete(k) { m.delete(k); },
  };
}

const jsonResp = (v, status = 200) =>
  new Response(JSON.stringify(v), { status, headers: { "content-type": "application/json" } });

// state: { connectors: Map(sha256hex -> team), tz: {team: tz}, usage: [...], month: [...],
//          down: bool, calls: [] }
export function fakeSupabase(state) {
  state.calls = state.calls || [];
  return async function fetch(input, init = {}) {
    const url = typeof input === "string" ? input : input.url;
    state.calls.push(url);
    if (url.startsWith("https://fcm.googleapis.com/")) return jsonResp({ name: "ok" });
    if (!url.startsWith("https://sb.test/")) throw new Error(`unexpected fetch ${url}`);
    if (state.down) return jsonResp({ message: "down" }, 503);
    const body = init.body ? JSON.parse(init.body) : {};
    if (url.endsWith("/rest/v1/rpc/resolve_connector_token")) {
      return jsonResp(state.connectors.get(body.p_hash) ?? null);
    }
    if (url.endsWith("/rest/v1/rpc/get_team_usage")) return jsonResp(state.usage || []);
    if (url.endsWith("/rest/v1/rpc/get_team_month")) {
      state.months = state.months || [];
      state.months.push(body.p_month);
      return jsonResp(state.month || []);
    }
    if (url.includes("/rest/v1/teams?")) {
      const team = decodeURIComponent(new URL(url).searchParams.get("team").replace(/^eq\./, ""));
      return jsonResp(state.tz && state.tz[team] ? [{ tz: state.tz[team] }] : []);
    }
    throw new Error(`unexpected supabase call ${url}`);
  };
}
