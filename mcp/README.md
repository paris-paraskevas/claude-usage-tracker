# Claude Usage Tracker — MCP server

Lets **Claude drive the tracker through chat**: ask for your usage, the team overview/ledger,
force a sync, or hand it an exported spend report. It's a thin bridge to the tracker's own
loopback API (`127.0.0.1`) — it holds **no secrets** and needs the tray app running.

> **What it is / isn't.** This is a *control/interface* layer, not a new data source. It
> exposes exactly what the tracker already knows. It does **not** unlock Anthropic's org
> analytics — a Team plan can't grant an app that access (see `docs/TEAM.md`).

## Tools
| Tool | What it returns |
|---|---|
| `get_usage` | 5h/weekly utilization + resets, context, extra-usage € |
| `get_status` | tracker live/stale, Anthropic status, update available |
| `get_team_overview` | *(team admin)* per-member load, MTD spend, near-limit members |
| `get_team_ledger` | *(team admin)* per-member monthly spend + tokens |
| `sync_now` | force an immediate usage refresh |
| `import_spend_report` | ingest an exported spend-report CSV (parser pending the export format) |

## Use it in Claude Code
```bash
cd mcp && npm install
claude mcp add claude-usage-tracker -- node "C:/Dev/Personal/claude-usage/mcp/server.mjs"
```
Or add to a project `.mcp.json`:
```json
{ "mcpServers": { "claude-usage-tracker": { "command": "node", "args": ["C:/Dev/Personal/claude-usage/mcp/server.mjs"] } } }
```

## Use it in Claude Desktop (one-click `.mcpb`)
```bash
cd mcp && npm install
npm install -g @anthropic-ai/mcpb
mcpb pack            # produces claude-usage-tracker.mcpb (bundles server + deps)
```
Then double-click the `.mcpb`, or drag it into **Claude Desktop → Settings → Extensions**.

## Config
- The tracker's port is auto-detected from `%LOCALAPPDATA%\ClaudeUsageTracker\server_port`.
  Override with the `CUT_PORT` env var (or the extension's "Tracker port" setting) if needed.
- The tray app must be running; otherwise the tools return an actionable "can't reach the tracker" error.

## claude.ai / mobile
Those clients reach an MCP server from Anthropic's cloud, so they need the **remote** connector
on the relay (Cloudflare Worker), not this local server — see `docs/MCP-REMOTE.md` (separate).
The remote connector can serve the **plaintext team data** (D1); your personal usage stays
local because the snapshot is end-to-end encrypted.
