# Remote MCP connector (claude.ai + mobile) — design & plan

**Status: planned, not built.** The local MCP server (`mcp/`) already covers Claude Code and
Claude Desktop. This document scopes the *remote* connector that claude.ai web and the mobile
apps require, because those clients call an MCP server **from Anthropic's cloud**, not from your
machine.

## What it can and can't serve
The remote server runs on the **relay** (Cloudflare Worker), so it can only see what the relay
stores. That's the **plaintext team data in D1** (per-member 5h/weekly, monthly spend, tokens).
It **cannot** serve your personal live usage: that rides in the phone snapshot, which is
**end-to-end encrypted** — the relay holds ciphertext it can't read. So:

- ✅ `get_team_overview`, `get_team_ledger` — team admins, from D1.
- ✅ `import_spend_report` — writes the ingested ledger into D1 (so claude.ai and the phone see it).
- ❌ `get_usage` (personal) — stays on the local server only (E2EE).

That's a real consequence of the zero-knowledge design, not a gap to fix.

## Architecture
- **Transport:** Streamable HTTP MCP (Anthropic's recommended remote transport as of 2026),
  added to the existing Worker at e.g. `POST /mcp`. Stateless JSON keeps the Worker simple.
- **Server:** reuse the same tool definitions as `mcp/server.mjs`, but the handlers read D1
  directly (`teams`, `usage_rows`, `finals`) instead of the localhost API.
- **Identity → team:** each request must resolve to one `tid`. The OAuth subject maps to the
  admin of that team (the admin token's hash, or a dedicated MCP identity minted at connect).

## Status
- ✅ **Transport + tools (done, verified):** `POST /mcp` (stateless JSON-RPC) with
  `get_team_overview` + `get_team_ledger` reading D1. Interim auth: Bearer = the team admin
  token (hashed → tid). Verified via `wrangler dev` (initialize / tools-list / tools-call +
  the unauthorized guard). Safe to deploy as-is (bearer-gated); claude.ai just can't use it
  until the OAuth layer below exists.
- ⏳ **OAuth layer (next):** the piece that lets claude.ai's connector actually authenticate.

## Auth (the hard part)
claude.ai custom connectors authenticate via **OAuth**, and Anthropic's client uses **Dynamic
Client Registration**. So the Worker must implement an OAuth provider:
- `/.well-known/oauth-authorization-server` metadata,
- dynamic client registration, authorization + token endpoints (PKCE),
- issue a bearer that maps to a `tid`, checked on every `/mcp` call.
- Recommended: Cloudflare's `@cloudflare/workers-oauth-provider` + the `agents` SDK MCP support,
  which wrap most of this. Secrets/token store in KV or D1.

This is an **authentication boundary on your production relay** — it needs careful review, not a
rushed pass. Scope it as its own change with its own verification.

**Planned shape (hand-rolled, dependency-free to match the Worker):**
- New D1 tables: `oauth_clients` (from DCR), `oauth_codes` (short-lived, single-use, PKCE
  challenge), `oauth_tokens` (opaque bearer → tid + expiry).
- `GET /.well-known/oauth-authorization-server` + `/.well-known/oauth-protected-resource`
  metadata; `POST /oauth/register` (DCR); `GET/POST /oauth/authorize` (consent); `POST /oauth/token`.
- **Consent = prove you're the team admin:** the authorize page asks the admin to paste their
  **team admin token**; we match it to a `tid` and mint the code. That's the human-auth step
  (we have no other way to tie a claude.ai user to a team).
- `/mcp` accepts either an OAuth token (→ tid) or the admin token (interim); a 401 returns
  `WWW-Authenticate: Bearer resource_metadata=...` so the client discovers the auth server.
- Verifiable locally with a mock client (register → authorize → token → `/mcp`); the **real
  claude.ai DCR + redirect handshake** is validated only after you deploy and add the connector.

## Setup once built (Team plan)
Remote connectors on a Team plan are added by the **Owner**, org-wide:
1. claude.ai → **Organization settings → Connectors → Add → Custom** → paste the relay's MCP URL
   (`https://<worker>/mcp`) + the OAuth client details.
2. Members enable it per conversation via the **+ → Connectors** toggle.
(Anthropic's cloud reaches the Worker over the public internet — the relay already is public.)

## Verification limits
The full claude.ai handshake (DCR + OAuth + the cloud calling `/mcp`) **can't be exercised from a
dev box** — only `wrangler dev` + a mock client can test the transport and a stubbed OAuth flow.
Real end-to-end validation happens after the Owner adds the connector in claude.ai. Plan for a
staged rollout (internal test first).

## Effort & recommendation
Meaningfully larger than the local server (an OAuth provider + MCP-over-HTTP + D1-backed tools +
the org-connector setup), only partially verifiable here, and it serves the team subset only.
Worth doing if you want the team ledger in claude.ai/mobile; if the local server (Claude Code +
Claude Desktop, where you already view analytics) is enough, this can wait.
