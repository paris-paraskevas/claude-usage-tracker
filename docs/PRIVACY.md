# Privacy Policy — Claude Usage Tracker (Android)

_Last updated: 2026-06-28_

Claude Usage Tracker (the "App") shows your own Claude Code usage on your phone. It is a
companion to the desktop app, which is the only source of the data. This policy explains
what the App handles.

## Summary

- The App displays **your own** Claude usage statistics, relayed from **your** desktop.
- Everything that carries your data is **end-to-end encrypted**. The relay server and
  Google's push service only ever see **ciphertext** — neither we nor they can read it.
- **No advertising, no analytics, no trackers, no sale of data.**
- Your Claude account credentials are **never** transmitted to or stored by the App.

## What the App handles

- **Pairing keys** — when you pair, the desktop generates an account id, an access token,
  and an encryption key. These are stored **only** on your phone (encrypted at rest) and on
  your desktop. The encryption key is never sent to any server.
- **Usage snapshot** — your desktop encrypts a snapshot (usage percentages, reset times,
  per-project token counts, all-time stats) and uploads the **ciphertext** to the relay.
  The App downloads and decrypts it locally for display.
- **Push token** — to deliver notifications, the App registers a Firebase Cloud Messaging
  (FCM) token with the relay. Notification contents are themselves **end-to-end encrypted**
  and decrypted on your device; the push service cannot read them.

## What the App does NOT collect

- No Claude/Anthropic login, API keys, or message content.
- No name, email, contacts, location, or device identifiers for tracking.
- No usage analytics or crash/behavioral telemetry.

## Third-party services

- **Cloudflare** hosts the relay. It stores and forwards encrypted blobs and your FCM push
  token; it cannot read your snapshot or notification contents.
- **Google Firebase Cloud Messaging** delivers push notifications. Payloads are encrypted
  end-to-end; Google cannot read them.

## Data retention

Relayed data is transient: the relay automatically deletes a paired account's data after
about 7 days of inactivity. You can **unpair** in the App at any time to stop syncing; you
can rotate the keys from the desktop, which invalidates the old data.

## Security

Encryption uses libsodium's `crypto_secretbox` (XSalsa20-Poly1305) with a key shared only
between your desktop and phone via the pairing QR.

## Open source

The App, relay, and desktop are open source (MIT):
https://github.com/paris-paraskevas/claude-usage-tracker

## Contact

Questions: &lt;your-contact-email&gt;
