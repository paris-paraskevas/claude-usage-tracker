# Claude Usage Tracker: MCP server

This server lets **Claude drive the tracker from a chat**. You can ask for your usage or your
team's account pool and monthly ledger, or force a sync. It's a thin bridge to the tracker's
own loopback API (`127.0.0.1`). It holds **no secrets**, and the tray app must be running.

> **What it is and isn't.** This is a *control/interface* layer, not a new data source. It
> exposes exactly what the tracker already knows. It does **not** unlock Anthropic's org
> analytics, because a Team plan can't grant an app that access (see `docs/TEAM.md`).

## Tools
| Tool | What it returns |
|---|---|
| `get_usage` | 5h/weekly utilization and reset times, context, extra-usage spend |
| `get_status` | tracker live/stale, Anthropic status, whether an update is available |
| `get_team_overview` | *(signed in to a team)* the shared account pool: per-account load, month-to-date spend, near-limit accounts |
| `get_team_ledger` | *(signed in to a team)* per-account monthly spend and tokens; `month` defaults to the current local month |
| `sync_now` | force an immediate usage refresh |

The team tools work for any team member signed in to the tracker (dashboard → **Team** tab),
not only admins. If you aren't signed in, they say so. A spend-report CSV import was
removed until the tracker has a real import endpoint (issue #2).

## Use it in Claude Code
```bash
cd mcp && npm install
claude mcp add claude-usage-tracker -- node "/absolute/path/to/claude-usage-tracker/mcp/server.mjs"
```
Or add it to a project's `.mcp.json`:
```json
{ "mcpServers": { "claude-usage-tracker": { "command": "node", "args": ["/absolute/path/to/claude-usage-tracker/mcp/server.mjs"] } } }
```
On Windows, use a path such as `C:/Users/you/claude-usage-tracker/mcp/server.mjs`.

## Use it in Claude Desktop (one-click `.mcpb`)
```bash
cd mcp && npm install
npm install -g @anthropic-ai/mcpb
mcpb pack            # produces claude-usage-tracker.mcpb (bundles server + deps)
```
Then double-click the `.mcpb`, or drag it into **Claude Desktop → Settings → Extensions**.

## Config
- The server finds the tracker's port automatically from the `server_port` file
  (`%LOCALAPPDATA%\ClaudeUsageTracker\server_port`; on macOS/Linux, `~/AppData/Local/ClaudeUsageTracker/server_port`, the same place the tracker writes it).
  If that fails, it uses 8787.
- To override the port, set the `CUT_PORT` env var or the extension's "Tracker port"
  setting. A blank or invalid value is ignored.
- If the tray app isn't running, the tools return a "can't reach the tracker" error that
  tells you what to do.

## Tests
```bash
cd mcp && npm test   # zero-dependency: port resolution, error mapping, manifest ↔ server tool list
```

## claude.ai / mobile
These clients reach MCP servers from Anthropic's cloud, so they can't use this local server.
They need the **remote** connector on the relay (Cloudflare Worker); see `relay/README.md`
and `docs/MCP-REMOTE.md`. The remote connector serves only the team account pool, read
from Supabase, and you authorize it with a connector token minted in the desktop app. Your
personal usage stays local, because the phone-sync snapshot is end-to-end encrypted.
