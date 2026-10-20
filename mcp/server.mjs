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
 * we use $CUT_PORT when it's a real port number, else that file, else 8787 (see lib.mjs).
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { trackerPort, localMonth, teamErrorMessage, usageSummary, ledgerSummary } from "./lib.mjs";

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
  title: "Get team account pool",
  description: "For members of a team in the tracker: the shared Claude account pool — per-account 5h/weekly load, " +
    "month-to-date extra-usage spend, and accounts near a limit. Needs you to be signed in to a team in the tracker.",
  inputSchema: {},
  annotations: { readOnlyHint: true, openWorldHint: false },
}, async () => {
  try {
    const d = await trackerCall("GET", "/api/team/overview");
    const err = teamErrorMessage(d);
    return err ? fail(err) : ok(d);
  } catch (e) { return fail(e.message); }
});

server.registerTool("get_team_ledger", {
  title: "Get monthly team spend ledger",
  description: "For members of a team in the tracker: per-account calendar-month extra-usage spend and tokens for a month.",
  inputSchema: {
    month: z.string().regex(/^\d{4}-\d{2}$/).optional()
      .describe("Month as YYYY-MM (defaults to the current month in your local time)."),
  },
  annotations: { readOnlyHint: true, openWorldHint: false },
}, async ({ month }) => {
  try {
    const m = month || localMonth();
    const d = await trackerCall("GET", `/api/team/ledger?month=${encodeURIComponent(m)}`);
    const err = teamErrorMessage(d);
    return err ? fail(err) : ok(ledgerSummary(d));
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

// A spend-report CSV import tool was stubbed here and removed until the tracker has a real
// import endpoint — see issue #2.

await server.connect(new StdioServerTransport());
