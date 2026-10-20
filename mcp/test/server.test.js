// Zero-dependency tests: pure helpers from lib.mjs, plus a static check that the tools
// server.mjs registers are exactly the ones manifest.json advertises.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { trackerPort, portFile, localMonth, teamErrorMessage, ledgerSummary, DEFAULT_PORT } from "../lib.mjs";

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

test("manifest tools match the tools server.mjs registers", () => {
  const registered = [...read("../server.mjs").matchAll(/server\.registerTool\(\s*"([^"]+)"/g)].map((m) => m[1]).sort();
  const manifest = JSON.parse(read("../manifest.json")).tools.map((t) => t.name).sort();
  assert.ok(registered.length > 0);
  assert.deepEqual(manifest, registered);
  assert.ok(!registered.includes("import_spend_report"), "stub tool removed (issue #2)");
});

test("trackerPort: valid CUT_PORT wins; blank/placeholder/junk fall back to the port file, then default", () => {
  const noFile = () => { throw new Error("ENOENT"); };
  const file = (v) => () => v;
  const base = { LOCALAPPDATA: "/x" };
  assert.equal(trackerPort({ ...base, CUT_PORT: "8790" }, noFile), 8790);
  for (const bad of ["", "   ", "${user_config.port}", "abc", "0", "70000", "87.5", undefined]) {
    assert.equal(trackerPort({ ...base, CUT_PORT: bad }, file("8791\n")), 8791, `CUT_PORT=${bad}`);
    assert.equal(trackerPort({ ...base, CUT_PORT: bad }, noFile), DEFAULT_PORT, `CUT_PORT=${bad}, no file`);
  }
  assert.equal(trackerPort({ ...base }, file("garbage")), DEFAULT_PORT);
  assert.match(portFile(base), /ClaudeUsageTracker[\\/]server_port$/);
  assert.match(portFile({}, "/home/u"), /^[\\/]home[\\/]u[\\/]AppData[\\/]Local[\\/]ClaudeUsageTracker[\\/]server_port$/);
});

test("localMonth uses local time", () => {
  assert.equal(localMonth(new Date(2026, 0, 1, 0, 5)), "2026-01");
  assert.equal(localMonth(new Date(2026, 11, 31, 23, 55)), "2026-12");
});

test("team errors map to the tracker's real error codes", () => {
  assert.equal(teamErrorMessage({ accounts: [] }), null);
  assert.match(teamErrorMessage({ error: "not_signed_in" }), /not signed in/);
  assert.match(teamErrorMessage({ error: "bad_month" }), /YYYY-MM/);
  assert.match(teamErrorMessage({ error: "supabase unreachable" }), /couldn't reach/);
  assert.match(teamErrorMessage({ error: "other" }), /other/);
});

test("ledgerSummary reads accounts (not members) from /api/team/ledger", () => {
  const d = { month: "2026-10", accounts: { "a@x.com": "A" }, days: {}, finals: {},
              computed_spend: { "a@x.com": 4.5 }, month_tokens: { "a@x.com": 1200 } };
  assert.deepEqual(ledgerSummary(d), {
    month: "2026-10", accounts: { "a@x.com": "A" }, spend: { "a@x.com": 4.5 }, tokens: { "a@x.com": 1200 },
  });
});
