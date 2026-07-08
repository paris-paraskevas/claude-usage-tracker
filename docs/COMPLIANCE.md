# Compliance read — Anthropic ToS + Supabase DPA (v0.3.0 gate)

_Compiled 2026-07-08 from primary sources. This is a research summary to support the
owner's go/no-go decision — **not legal advice.** The Anthropic quotes below were
re-verified verbatim against the live source on 2026-07-08._

## Bottom line
- **The gate is Anthropic, not Supabase.** The app's core mechanism — reusing the Claude
  Code **OAuth token** to call `GET /api/oauth/usage` from a non-native app — is **GRAY,
  leaning PROHIBITED**. Anthropic states OAuth is "intended _exclusively_ … to support
  ordinary use of Claude Code and **other native Anthropic applications**," and that
  developers "should use API key authentication." The tracker is not a native app and uses
  the OAuth token, not an API key.
- **`oauth/usage` is not a sanctioned developer surface.** The sanctioned path (API key via
  Console) does **not** expose plan-usage windows, so there may be **no fully compliant path**
  for this feature without explicit Anthropic permission.
- **Decision rule (owner):** do **not** publish the public "hosted-for-everyone" 0.3.0 while
  this reads as violating. Unblock = written Anthropic permission (contact sales), or drop
  the OAuth-token mechanism.
- **What keeps it from being worse:** only *usage numbers* are centralized (never prompts/
  outputs, never the token itself). Anthropic's model-scraping and credential-sharing bans are
  **not** triggered *as long as the OAuth token is never centralized or relayed* — keep it
  strictly local, per-user.
- **Supabase side is clean & self-serve:** maintainer = GDPR **controller**, Supabase =
  **processor**; DPA is click-through/e-sign; EU residency = pick an EU region at project
  creation.

## Anthropic ToS

**Governing docs** (fetched 2026-07-08): [Usage Policy](https://www.anthropic.com/legal/aup) ·
[Consumer Terms](https://www.anthropic.com/legal/consumer-terms) (Free/Pro/Max) ·
[Commercial Terms](https://www.anthropic.com/legal/commercial-terms) (Team/Enterprise/API) ·
[Claude Code — Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance).

### Decisive clause — Claude Code "Authentication and credential use" (verified verbatim)
> "**OAuth authentication** is intended exclusively for purchasers of Claude Free, Pro, Max,
> Team, and Enterprise subscription plans and is designed to support ordinary use of Claude
> Code and **other native Anthropic applications**."
> "**Developers** building products or services that interact with Claude's capabilities …
> **should use API key authentication through Claude Console** or a supported cloud provider.
> **Anthropic does not permit third-party developers to offer Claude.ai login or to route
> requests through Free, Pro, or Max plan credentials on behalf of their users.**"
> "**Anthropic reserves the right to take measures to enforce these restrictions and may do so
> without prior notice.**"

Consumer Terms **§3** (prohibited uses):
> "Except when you are accessing our Services via an Anthropic API Key or where we otherwise
> explicitly permit it, **to access the Services through automated or non-human means**,
> whether through a bot, script, or otherwise."

**Analysis.**
- **Token reuse from a non-native app → GRAY-leaning-PROHIBITED.** The tracker is not a native
  Anthropic app and hits a Services endpoint via a script using the OAuth token, not an API
  key — squarely inside §3's automated-access ban (which carves out only API-key access) and
  outside OAuth's stated "native applications" scope. The narrower "on behalf of their users"
  sentence is **not** cleanly triggered (each install reads its *own* local token on its *own*
  machine; the token is never routed to the maintainer), but the "intended exclusively … native
  applications" framing plus the enforcement reservation still put the mechanism outside
  sanctioned use.
- **Centralizing usage numbers → GRAY (no direct Anthropic bar).** §2 (no credential sharing)
  is *not* triggered as designed — only derived numbers are stored, never login/token. The
  model-scraping ban is *not* triggered — no prompts/outputs are read. **Load-bearing: never
  centralize or transmit the OAuth token.**

**Unblock paths:** (a) get written permission from Anthropic for `oauth/usage` read access
([contact sales](https://www.anthropic.com/contact-sales)); or (b) migrate to API-key/Console
auth — but document that it does **not** currently return plan-usage windows, so it likely
cannot replace the feature.

## Supabase DPA
- **Roles (DPA cl. 2):** "Supabase acts as a processor/service provider, and Customer as
  controller." → **maintainer = controller**, Supabase = processor for all installers' PII.
- **Sign it (self-serve):** Supabase Dashboard → **Organization → Documents → Legal Documents**
  → generate the DPA (PandaDoc) → complete Part 1 (customer/controller details) → e-sign.
  Signing also executes the EU SCCs (Module Two, controller→processor). No sales contact needed.
  Static copy: <https://supabase.com/legal/dpa>.
- **EU residency (DPA cl. 6.2):** guaranteed by **selecting an EU region at project creation**
  (region is fixed after creation). Keep any read replicas in-EU too. GDPR Art. 28 requires the
  processor contract → **both** the EU region **and** a signed DPA are needed.
- **Sub-processors (DPA Schedule 3):** mostly US entities (AWS, Google, Cloudflare, Vercel,
  Fly.io, Upstash; Supabase contracting entity in Singapore) — hence the SCCs. Supabase gives
  ≥30 days' notice of sub-processor changes.
- **Controller duties you inherit:** Art. 13/14 notice + lawful basis to installers (the in-app
  consent gate covers disclosure), an Art. 30 processing record, and a route for data-subject
  requests (Supabase forwards these to you as controller).

## Owner action items
- [ ] **Decide the Anthropic question first.** Do not ship public 0.3.0 while GRAY-leaning-PROHIBITED.
- [ ] Seek written Anthropic permission for third-party `oauth/usage` read access, or record the
      API-key gap. Keep it in writing.
- [ ] Verify the push payload contains only derived numbers — never the OAuth token.
- [ ] Sign the Supabase DPA; confirm the project sits in an EU region.
- [ ] Keep the in-app consent gate (central EU DB, not E2EE, domain-visible) before first sign-in.

## Sources (fetched 2026-07-08)
- https://code.claude.com/docs/en/legal-and-compliance — "Authentication and credential use" (verified verbatim)
- https://www.anthropic.com/legal/consumer-terms · /commercial-terms · /aup
- https://supabase.com/legal/dpa · current DPA PDF (roles cl. 2, residency 6.2, sub-processors Schedule 3, SCCs cl. 12)
- https://supabase.com/docs/guides/platform/regions
