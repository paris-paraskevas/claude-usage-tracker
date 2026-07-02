# Remote / mobile access — architecture & contract

This is the frozen contract shared by the three components of the optional remote
feature: the **desktop** tracker (producer), a **relay** (Cloudflare Worker, dumb
pipe), and the **Android app** (consumer). It is **off by default** and **opt-in**.

> **Scope:** Android only for now — **no iOS support** (iOS Web/native push and the
> store process are out of scope for v1). The relay and crypto are platform-neutral, so
> an iOS client could be added later without changing this contract.

## Why a relay at all

Most of what the app shows (sessions, context %, per-project, all-time, history) is
derived from files Claude Code writes on the **desktop** — Anthropic has no API for it.
Only the 5h/weekly account limits come from an endpoint. So the phone cannot generate
this data; the desktop must relay it. Consequence that does **not** go away: you get
live data + alerts only while the **desktop is running and online**. The relay only
removes the "same network" requirement.

## Trust model — the relay is zero-knowledge

The relay never sees plaintext. Everything content-bearing is **end-to-end encrypted**
between desktop and phone with a key the relay never receives.

Three identifiers, all generated on the **desktop** at first pairing:

| id | bytes | who sees it | purpose |
|----|-------|-------------|---------|
| `accountId` | 16 random → base64url | desktop, phone, relay | routing/storage namespace |
| `readToken` | 32 random → base64url | desktop, phone, relay | bearer auth for **all** relay calls |
| `e2eeKey`   | 32 random (secretbox key) | desktop, phone **only** | encrypts/decrypts every payload |

- The relay stores only `sha256(readToken)` and compares; on first write it pins the
  hash (trust-on-first-use). A breached relay yields **ciphertext only** — undecryptable
  without `e2eeKey`, and forged ciphertext fails the phone's Poly1305 auth (detected).
- The Claude OAuth token **never leaves the desktop**. `readToken` authorizes *your relay*,
  not Claude.

## Pairing (QR)

Desktop renders a QR in the dashboard **Settings → Remote** containing:

```
cutpair1:<base64url(JSON)>
JSON = { "u": "<relay base url>", "a": "<accountId>", "t": "<readToken>", "k": "<e2eeKey base64url>" }
```

The Android app scans it, stores `{u,a,t,k}` in encrypted prefs, and is paired. Re-show
the QR to add devices; **rotate** (new readToken+e2eeKey, old data invalidated) or
**unpair** (delete relay record) from the desktop.

## Encrypted blob format

libsodium **`crypto_secretbox`** (XSalsa20-Poly1305), 24-byte random nonce, 32-byte key
(`e2eeKey`). Wire form (JSON, fields base64 standard):

```json
{ "v": 1, "nonce": "<24 bytes b64>", "ct": "<secretbox output b64>", "ts": 1719600000 }
```

- **Snapshot** plaintext = UTF-8 JSON of the desktop `snap` dict (the same object the
  local dashboard consumes from `/api/usage`).
- **Push** plaintext = `{"title": "...", "body": "...", "tag": "5h-80"}` (tag collapses
  repeats). Must stay < ~3 KB (FCM data limit is 4 KB).

Interop is verified both ways: PyNaCl (`nacl.secret.SecretBox`) ↔ libsodium-android
(`crypto_secretbox_easy` / `_open_easy`).

## Relay API

Base: `https://<worker>`. All calls require `Authorization: Bearer <readToken>`.
`{accountId}` is path-segment, URL-safe.

| Method + path | caller | body | returns |
|---|---|---|---|
| `PUT /v1/acct/{accountId}/snapshot` | desktop | encrypted blob | `204` (auto-registers `readTokenHash` on first call; `403` on hash mismatch) |
| `GET /v1/acct/{accountId}/snapshot` | phone | — | encrypted blob, or `204` if none |
| `PUT /v1/acct/{accountId}/push-token` | phone | `{ "token": "<fcm>", "platform": "android" }` | `204` |
| `DELETE /v1/acct/{accountId}/push-token` | phone | `{ "token": "<fcm>" }` | `204` |
| `POST /v1/acct/{accountId}/push` | desktop | encrypted blob (push plaintext) | `200 {"sent":N}` — fans out an FCM **data** message to every registered token |

- Storage: Cloudflare **KV** (`snapshot:{accountId}`, `tokens:{accountId}` set,
  `auth:{accountId}` = readTokenHash). The snapshot's last-write time rides in its KV
  metadata (the write throttle), and the auth TTL is refreshed at most once a day, so a
  steady sync costs **one KV write per push**. v2 may switch to a Durable Object per
  account for strong consistency + lower latency.
- Per-account write throttle (8 s min gap). Desktop syncs on a throttle
  (`remote_sync_seconds`, default 300), not every UI poll. At one write/push, 300s is
  ~288 writes/day — well under a free-tier Worker's 1000 writes/day.

## Push (FCM HTTP v1, E2EE-preserving)

The relay sends **data-only**, **high-priority** messages (never `notification`
messages — those would expose plaintext to Google and bypass our decryption):

```json
{ "message": { "token": "<fcm>", "android": { "priority": "HIGH" },
  "data": { "v": "1", "nonce": "<b64>", "ct": "<b64>" } } }
```

The Android `FirebaseMessagingService.onMessageReceived` decrypts `data` with `e2eeKey`
and posts a local notification. The relay mints a Google OAuth token from a service
account (RS256 JWT → token endpoint, cached ~50 min) using Worker **secrets**:
`FCM_PROJECT_ID`, `FCM_CLIENT_EMAIL`, `FCM_PRIVATE_KEY`.

## What each side must do when a usage threshold crosses

The desktop already computes alerts (`check_thresholds`, `check_danger`, `check_alerts`).
On a crossing, in addition to the local Windows toast, it builds the push plaintext,
encrypts it, and `POST /v1/.../push`. The relay only fans out. (Desktop-decided alerts
keep the logic in one place and stay compatible with E2EE.)

## Operator setup (user-owned accounts)

1. **Cloudflare:** `cd relay && npm i`, create a KV namespace, set IDs in
   `wrangler.toml`, `npx wrangler deploy`. Note the Worker URL.
2. **Firebase (FCM):** create a project, add an Android app (package
   `com.claudeusage.tracker`), download `google-services.json` → `android/app/`. Create a
   service account, download its JSON, set the three `FCM_*` Worker secrets
   (`npx wrangler secret put ...`).
3. **Desktop:** `pip install "claude-usage-tracker[remote]"` (adds `pynacl`, `qrcode`),
   enable **Settings → Remote**, paste the Worker URL, scan the QR with the app.
4. **Android:** build `android/` (`./gradlew assembleDebug`), sideload the APK, scan the QR.

See each subdir's README for specifics.
