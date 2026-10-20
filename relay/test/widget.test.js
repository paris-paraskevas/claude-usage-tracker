// Runs the pool widget's inline script in a vm with a minimal DOM, to check that hostile
// values never reach innerHTML unescaped and that only the parent frame can feed it data.
import { test } from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import { POOL_WIDGET_HTML } from "../src/widget.js";

function boot() {
  const script = POOL_WIDGET_HTML.match(/<script>([\s\S]*?)<\/script>/)[1];
  const els = {};
  const el = (id) => (els[id] = els[id] || { id, innerHTML: "", className: "", textContent: "" });
  const listeners = [];
  const timers = [];
  const posted = [];
  const parent = { postMessage: (m) => posted.push(m) };
  const attrs = {};
  const window = {
    parent,
    addEventListener: (type, fn) => { if (type === "message") listeners.push(fn); },
  };
  const ctx = {
    window,
    document: { getElementById: el, documentElement: { setAttribute: (k, v) => { attrs[k] = v; } } },
    setTimeout: (fn, ms) => timers.push({ fn, ms }),
    Date, Math, Number, String, Array, isFinite, JSON,
  };
  vm.runInNewContext(script, ctx);
  const send = (data, source = parent) => listeners.forEach((fn) => fn({ source, data }));
  return { els, send, timers, posted, attrs, window };
}

const EVIL = `<img src=x onerror=alert(1)>`;

test("widget escapes every teammate-supplied value", () => {
  const w = boot();
  w.window.render({
    today: EVIL, tz: EVIL,
    kpis: { org_spend: 12.5, account_count: `${EVIL}`, near: [{}] },
    accounts: [{
      acct: EVIL, name: EVIL, month_spend: 3,
      account: { fh_pct: 91, sd_pct: `50"><script>x</script>`, ts: 1, extra: { currency: EVIL, enabled: true, pct: 10 } },
      last_used: { ts: 1, device: EVIL, by: EVIL },
    }],
  });
  const all = w.els.hd.innerHTML + w.els.cards.innerHTML;
  assert.ok(!/<img|<script/i.test(all), all);
  assert.ok(all.includes("&lt;img"));
  assert.match(all, /role='progressbar'/);
  assert.match(all, /aria-label='5-hour usage'/);
  assert.match(all, /near limit/);
  assert.match(w.els.hd.innerHTML, /Sorted by 5-hour usage/);
});

test("widget shows per-currency org spend instead of summing", () => {
  const w = boot();
  w.window.render({ kpis: { org_spend: null, org_currency: null, org_spend_by_currency: { EUR: 12, USD: 3.5, [EVIL]: 1 } }, accounts: [] });
  const hd = w.els.hd.innerHTML;
  assert.match(hd, /EUR 12\.00 · USD 3\.50/);
  assert.ok(!hd.includes("<img"));
  const w2 = boot();
  w2.window.render({ kpis: { org_spend: 7, org_currency: "EUR", org_spend_by_currency: { EUR: 7 } }, accounts: [] });
  assert.match(w2.els.hd.innerHTML, /EUR 7\.00/);
});

test("widget only accepts well-formed data from the parent frame", () => {
  const w = boot();
  w.send({ structuredContent: { accounts: [{ name: "spoofed" }] } }, { postMessage() {} });
  const cards = () => (w.els.cards ? w.els.cards.innerHTML : "");
  assert.ok(!cards().includes("spoofed"), "messages from other frames are ignored");
  w.send({ structuredContent: "nope" });
  w.send({ accounts: "not-an-array" });
  assert.equal(cards(), "");
  w.send({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { structuredContent: { accounts: [{ name: "Real" }], kpis: {} } } });
  assert.match(cards(), /Real/);
});

test("widget: MCP Apps handshake, host theme, and a no-data timeout", () => {
  const w = boot();
  const init = w.posted.find((m) => m && m.method === "ui/initialize");
  assert.ok(init);
  w.send({ jsonrpc: "2.0", id: init.id, result: { hostContext: { theme: "light" } } });
  assert.equal(w.attrs["data-theme"], "light");
  assert.ok(w.posted.some((m) => m && m.method === "ui/notifications/initialized"));
  const t = w.timers.find((x) => x.ms === 10000);
  t.fn();
  assert.match(w.els.cards.textContent, /No data received from the host/);
});
