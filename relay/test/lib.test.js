import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import {
  tzParts, monthIn, prevMonth, validTz, monthSpend, overviewMerge, ledgerComputed, buildLedger,
  buildOverview, escapeHtml, cleanName, validateRedirectUris, isValidChallenge, verifyPkce,
  negotiateProtocol, dispatchRpc, MCP_PROTOCOL_VERSIONS, round2,
} from "../src/lib.js";

test("round2 matches Python round(v, 2)", () => {
  // Expected values are Python 3 outputs.
  const cases = [[1.105, 1.1], [2.675, 2.67], [0.125, 0.12], [0.375, 0.38], [1.005, 1], [3.31, 3.31],
    [12.344999, 12.34], [-0.125, -0.12], [0, 0], [99999.995, 99999.99], [7, 7]];
  for (const [v, want] of cases) assert.equal(round2(v), want, `round2(${v})`);
});

test("tzParts / monthIn follow the team tz, not UTC", () => {
  // 2026-10-31T22:30Z is already Nov 1st in Athens (UTC+2 after DST ends) but Oct in New York.
  const d = new Date("2026-10-31T22:30:00Z");
  assert.equal(tzParts("Europe/Athens", d).date, "2026-11-01");
  assert.equal(monthIn("Europe/Athens", d), "2026-11");
  assert.equal(monthIn("America/New_York", d), "2026-10");
  assert.equal(tzParts("Europe/Athens", d).hour, 0);
  // Invalid tz falls back to the default rather than throwing.
  assert.equal(tzParts("Not/AZone", d).date, "2026-11-01");
  assert.equal(validTz("Not/AZone"), null);
  assert.equal(validTz("Asia/Tokyo"), "Asia/Tokyo");
});

test("prevMonth wraps the year", () => {
  assert.equal(prevMonth("2026-01"), "2025-12");
  assert.equal(prevMonth("2026-10"), "2026-09");
});

test("monthSpend sums increases and treats drops as a reset", () => {
  assert.equal(monthSpend([10, 12, 15], 8), 7);
  assert.equal(monthSpend([10, 12, 3, 5], null), 2 + 3 + 2);
  assert.equal(monthSpend([], 5), 0);
  assert.equal(monthSpend([1.005, 2.01], 0), 2.01);
  assert.equal(monthSpend([1.105], 0), 1.1, "Python-compatible rounding");
  assert.equal(monthSpend([NaN, "x", 4], 1), 3);
});

test("overviewMerge attaches month spend, tokens and KPIs", () => {
  const row = (date, acct, device, used, extra = {}) => ({
    kind: "usage",
    r: { date, acct, device, display_name: acct.toUpperCase(), extra_enabled: true, extra_used: used,
         extra_currency: "EUR", fh_pct: 10, sd_pct: 20, ts: Date.parse(date) / 1000, ...extra },
  });
  const led = buildLedger("acme.com", "2026-10", [
    row("2026-10-01", "a@acme.com", "d1", 5, { tok_month: 100 }),
    row("2026-10-02", "a@acme.com", "d1", 9, { tok_month: 250 }),
    row("2026-10-02", "b@acme.com", "d2", 3),
  ]);
  const prev = buildLedger("acme.com", "2026-09", [row("2026-09-30", "a@acme.com", "d1", 4)]);
  assert.deepEqual(ledgerComputed(led, prev), { "a@acme.com": 5, "b@acme.com": 0 });
  const ov = buildOverview("acme.com", "Europe/Athens", "2026-10-02", "2026-10-01", [
    { ...row("2026-10-02", "a@acme.com", "d1", 9).r, fh_pct: 85 },
    row("2026-10-02", "b@acme.com", "d2", 3).r,
  ]);
  const m = overviewMerge(ov, led, prev);
  const a = m.accounts.find((x) => x.acct === "a@acme.com");
  assert.equal(a.month_spend, 5);
  assert.equal(a.month_tokens, 250);
  assert.equal(a.name, "A@ACME.COM");
  assert.deepEqual(m.kpis, {
    org_spend: 5, org_currency: "EUR", org_spend_by_currency: { EUR: 5 },
    account_count: 2, near: [{ name: "A@ACME.COM", window: "5h", pct: 85 }],
  });
  assert.ok(!("escrow" in a), "escrow is gone with the D1 team stack");
});

test("overviewMerge never sums across currencies (parity with team_overview_merge)", () => {
  const acct = (acct, currency) => ({ acct, name: acct, account: { extra: currency === undefined ? null : { currency } } });
  const ledFor = (spends) => {
    // One account-level sample per day: baseline 0 then the spend, so monthSpend == spend.
    const rows = [];
    for (const [a, v] of Object.entries(spends)) {
      rows.push({ kind: "usage", r: { date: "2026-10-01", acct: a, device: "pc", extra_enabled: true, extra_used: 0 } });
      rows.push({ kind: "usage", r: { date: "2026-10-02", acct: a, device: "pc", extra_enabled: true, extra_used: v } });
    }
    return buildLedger("t", "2026-10", rows);
  };
  const kp = (accounts, spends) => overviewMerge({ accounts }, ledFor(spends), null).kpis;
  // Mixed currencies → no single total.
  let k = kp([acct("a", "EUR"), acct("b", "USD")], { a: 10, b: 5 });
  assert.equal(k.org_spend, null);
  assert.equal(k.org_currency, null);
  assert.deepEqual(k.org_spend_by_currency, { EUR: 10, USD: 5 });
  // Unlabelled spend alongside one currency folds into it.
  k = kp([acct("a", "EUR"), acct("b")], { a: 10, b: 2.5 });
  assert.deepEqual(k.org_spend_by_currency, { EUR: 12.5 });
  assert.equal(k.org_spend, 12.5);
  assert.equal(k.org_currency, "EUR");
  // Zero unlabelled spend is dropped as noise.
  k = kp([acct("a", "EUR"), acct("b")], { a: 10, b: 0 });
  assert.deepEqual(k.org_spend_by_currency, { EUR: 10 });
  // Only unlabelled, non-zero → kept under "".
  k = kp([acct("b")], { b: 3 });
  assert.deepEqual(k.org_spend_by_currency, { "": 3 });
  assert.equal(k.org_spend, 3);
  assert.equal(k.org_currency, "");
  // Unlabelled with two currencies stays separate.
  k = kp([acct("a", "EUR"), acct("b", "USD"), acct("c")], { a: 1, b: 2, c: 3 });
  assert.deepEqual(k.org_spend_by_currency, { EUR: 1, USD: 2, "": 3 });
  // No spend at all.
  k = overviewMerge({ accounts: [] }, null, null).kpis;
  assert.deepEqual(k, { org_spend: 0, org_currency: "", org_spend_by_currency: {}, account_count: 0, near: [] });
});

test("escapeHtml and cleanName", () => {
  assert.equal(escapeHtml(`<img src=x onerror="a('b')">&`), "&lt;img src=x onerror=&quot;a(&#39;b&#39;)&quot;&gt;&amp;");
  assert.equal(escapeHtml(null), "");
  assert.equal(cleanName("  Claude\u0000 Desktop\n "), "Claude  Desktop");
  assert.equal(cleanName("x".repeat(100)).length, 64);
  assert.equal(cleanName(42), "");
});

test("validateRedirectUris", () => {
  assert.deepEqual(validateRedirectUris(["https://claude.ai/api/mcp/auth_callback"]).uris,
    ["https://claude.ai/api/mcp/auth_callback"]);
  assert.ok(validateRedirectUris(["http://localhost:6274/cb", "http://127.0.0.1/cb"]).uris);
  for (const bad of [
    undefined, [], "https://a.example/cb", [42],
    ["http://evil.example/cb"], ["javascript:alert(1)"], ["https://a.example/cb#frag"],
    ["https://user:pw@a.example/cb"], ["not a url"], ["https://a.example/" + "x".repeat(600)],
    Array.from({ length: 6 }, (_, i) => `https://a.example/${i}`),
  ]) {
    assert.ok(validateRedirectUris(bad).error, `should reject ${JSON.stringify(bad)?.slice(0, 60)}`);
  }
});

test("PKCE S256", async () => {
  const verifier = randomBytes(32).toString("base64url");
  const challenge = createHash("sha256").update(verifier).digest("base64url");
  assert.ok(isValidChallenge(challenge));
  assert.equal(await verifyPkce(verifier, challenge), true);
  assert.equal(await verifyPkce(verifier + "x", challenge), false);
  assert.equal(await verifyPkce("short", challenge), false);
  assert.equal(isValidChallenge("plain-text-challenge"), false);
});

test("protocol negotiation echoes supported versions, else latest", () => {
  assert.equal(negotiateProtocol("2024-11-05"), "2024-11-05");
  assert.equal(negotiateProtocol("2025-06-18"), "2025-06-18");
  assert.equal(negotiateProtocol("1999-01-01"), MCP_PROTOCOL_VERSIONS[0]);
  assert.equal(negotiateProtocol(undefined), MCP_PROTOCOL_VERSIONS[0]);
});

test("dispatchRpc", async () => {
  const h = {
    serverInfo: { name: "t", version: "0" },
    tools: [{ name: "ok_tool" }, { name: "boom" }],
    resources: [],
    readResource: async (uri) => (uri === "ui://x" ? [{ uri, text: "hi" }] : null),
    callTool: async (name) => {
      if (name === "boom") throw new Error("secret upstream detail");
      return { content: [{ type: "text", text: "fine" }] };
    },
  };
  const init = await dispatchRpc({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-03-26" } }, h);
  assert.equal(init.body.result.protocolVersion, "2025-03-26");
  assert.deepEqual((await dispatchRpc({ jsonrpc: "2.0", id: 2, method: "ping" }, h)).body.result, {});
  assert.deepEqual(await dispatchRpc({ jsonrpc: "2.0", method: "notifications/initialized" }, h), { status: 202, body: null });
  assert.deepEqual(await dispatchRpc({ jsonrpc: "2.0", method: "notifications/cancelled", params: {} }, h), { status: 202, body: null });
  assert.deepEqual(await dispatchRpc({ jsonrpc: "2.0", method: "tools/call" }, h), { status: 202, body: null },
    "a request without an id is a notification and gets no response");
  assert.equal((await dispatchRpc({ jsonrpc: "2.0", id: 3, method: "nope" }, h)).body.error.code, -32601);
  assert.equal((await dispatchRpc({ id: 3, method: "ping" }, h)).status, 400);
  const boom = await dispatchRpc({ jsonrpc: "2.0", id: 4, method: "tools/call", params: { name: "boom" } }, h);
  assert.equal(boom.body.result.isError, true);
  assert.ok(!boom.body.result.content[0].text.includes("secret"), "upstream error details are not leaked");
  const unknown = await dispatchRpc({ jsonrpc: "2.0", id: 5, method: "tools/call", params: { name: "x" } }, h);
  assert.equal(unknown.body.result.isError, true);
  const res = await dispatchRpc({ jsonrpc: "2.0", id: 6, method: "resources/read", params: { uri: "ui://y" } }, h);
  assert.equal(res.body.error.code, -32002);
  const batch = await dispatchRpc([
    { jsonrpc: "2.0", id: 7, method: "ping" },
    { jsonrpc: "2.0", method: "notifications/initialized" },
  ], h);
  assert.equal(batch.body.length, 1);
  assert.deepEqual(await dispatchRpc([{ jsonrpc: "2.0", method: "notifications/initialized" }], h), { status: 202, body: null });
});
