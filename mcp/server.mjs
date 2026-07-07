#!/usr/bin/env node
/**
 * Claude Usage Tracker — local MCP server (stdio).
 *
 * A thin bridge so Claude (Claude Code, or Claude Desktop via a .mcpb extension) can
 * query and drive the desktop tracker through chat. It talks to the tracker's own
 * loopback dashboard API (127.0.0.1) — the same endpoints the dashboard UI uses — so
 * it never needs the Claude login token or any secret of its own.
 *
 * The tracker writes its live port to %LOCALAPPDATA%\ClaudeUsageTracker\server_port;
 * we read that (falling back to the 8787..8796 range start_server scans, or $CUT_PORT).
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const APP_DIR = join(process.env.LOCALAPPDATA || process.env.HOME || ".", "ClaudeUsageTracker");
const PORT_FILE = join(APP_DIR, "server_port");

function trackerPort() {
  if (process.env.CUT_PORT) return Number(process.env.CUT_PORT);
  try {
    const p = Number(readFileSync(PORT_FILE, "utf-8").trim());
    if (p > 0) return p;
  } catch { /* fall through to default */ }
  return 8787;
}

// A tracker call, with an actionable error if the app isn't running.
async function trackerCall(method, path, body) {
  const port = trackerPort();
  const url = `http://127.0.0.1:${port}${path}`;
  let resp;
  try {
    resp = await fetch(url, {
      method,
      headers: body ? { "content-type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(8000),
    });
  } catch (e) {
    throw new Error(
      `Couldn't reach the Claude Usage Tracker on 127.0.0.1:${port} (${e.message}). ` +
      `Make sure the tray app is running — launch it, or set CUT_PORT if it's on another port.`
    );
  }
  const text = await resp.text();
  if (!resp.ok) throw new Error(`Tracker returned HTTP ${resp.status}: ${text.slice(0, 200)}`);
  return text ? JSON.parse(text) : {};
}

const ok = (obj) => ({ content: [{ type: "text", text: JSON.stringify(obj, null, 2) }] });
const fail = (msg) => ({ isError: true, content: [{ type: "text", text: msg }] });

// Trim the big snapshot down to the numbers worth putting in a chat.
function usageSummary(d) {
  const win = {};
  for (const w of d.windows || []) win[w.key] = { pct: w.pct, resets_at: w.resets_at, level: w.level };
  return {
    ok: d.ok, updated_at: d.updated_at,
    account: (d.account || {}).org || (d.account || {}).email || d.subscription || null,
    verdict: (d.verdict || {}).text || null,
    five_hour: win.five_hour || null,
    seven_day: win.seven_day || null,
    context: d.context ? { pct: d.context.used_percentage, tokens: d.context.total_input_tokens } : null,
    extra_usage: d.extra && d.extra.enabled
      ? { used: d.extra.used, limit: d.extra.limit, currency: d.extra.currency, pct: d.extra.pct } : null,
  };
}

const server = new McpServer({ name: "claude-usage-tracker", version: "0.1.0" });

server.registerTool("get_usage", {
  title: "Get current Claude usage",
  description: "Your live Claude usage: 5-hour and weekly limit utilization (with reset times), " +
    "active context window, extra-usage spend (€), and the overall verdict.",
  inputSchema: {},
  annotations: { readOnlyHint: true, openWorldHint: false },
}, async () => {
  try { return ok(usageSummary(await trackerCall("GET", "/api/usage"))); }
  catch (e) { return fail(e.message); }
});

server.registerTool("get_status", {
  title: "Get tracker + Anthropic status",
  description: "Whether the tracker is live/stale, the Anthropic service status, and whether a tracker update is available.",
  inputSchema: {},
  annotations: { readOnlyHint: true, openWorldHint: true },
}, async () => {
  try {
    const d = await trackerCall("GET", "/api/usage");
    return ok({
      tracker: d.ok ? "live" : "stale", updated_at: d.updated_at,
      anthropic_status: (d.status || {}).description || null,
      update_available: (d.update || {}).available || null,
      verdict: (d.verdict || {}).text || null,
    });
  } catch (e) { return fail(e.message); }
});

server.registerTool("get_team_overview", {
  title: "Get team overview (admin only)",
  description: "For team-plan admins: live per-member 5h/weekly load, month-to-date spend, and who is near a limit. " +
    "Returns an error if you aren't a team admin.",
  inputSchema: {},
  annotations: { readOnlyHint: true, openWorldHint: false },
}, async () => {
  try {
    const d = await trackerCall("GET", "/api/team/overview");
    if (d.error === "not_admin") return fail("Not a team admin — no team overview available on this account.");
    if (d.error) return fail(`Team overview unavailable: ${d.error}`);
    return ok(d);
  } catch (e) { return fail(e.message); }
});

server.registerTool("get_team_ledger", {
  title: "Get monthly team spend ledger (admin only)",
  description: "For team-plan admins: per-member calendar-month extra-usage spend and tokens for a given month.",
  inputSchema: {
    month: z.string().regex(/^\d{4}-\d{2}$/).optional()
      .describe("Month as YYYY-MM (defaults to the current month)."),
  },
  annotations: { readOnlyHint: true, openWorldHint: false },
}, async ({ month }) => {
  try {
    const m = month || new Date().toISOString().slice(0, 7);
    const d = await trackerCall("GET", `/api/team/ledger?month=${encodeURIComponent(m)}`);
    if (d.error === "not_admin") return fail("Not a team admin — no ledger available on this account.");
    if (d.error) return fail(`Ledger unavailable: ${d.error}`);
    return ok({ month: d.month, members: d.members, spend: d.computed_spend, tokens: d.month_tokens });
  } catch (e) { return fail(e.message); }
});

server.registerTool("sync_now", {
  title: "Refresh usage now",
  description: "Force the tracker to re-check usage immediately (and re-mirror to the phone if remote sync is on).",
  inputSchema: {},
  annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: false },
}, async () => {
  try { await trackerCall("POST", "/api/refresh"); return ok({ ok: true, refreshed: true }); }
  catch (e) { return fail(e.message); }
});

server.registerTool("import_spend_report", {
  title: "Import an exported spend report (CSV)",
  description: "Ingest a spend report exported from the Claude analytics console into the tracker's monthly ledger. " +
    "Paste the CSV contents. NOTE: the tracker's import endpoint is not wired yet (pending the export format), " +
    "so this currently reports the columns it received so the format can be finalized.",
  inputSchema: {
    csv: z.string().min(1).describe("Raw CSV contents of the exported spend report."),
  },
  annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false },
}, async ({ csv }) => {
  // TODO(#2): POST to /api/team/import-spend once the tracker exposes it (needs the real export format).
  const header = (csv.split(/\r?\n/)[0] || "").trim();
  return ok({
    imported: false,
    reason: "Spend-report import isn't wired into the tracker yet — share this header so the parser can be built.",
    detected_header: header,
    detected_columns: header ? header.split(",").map((s) => s.trim()) : [],
  });
});

await server.connect(new StdioServerTransport());
