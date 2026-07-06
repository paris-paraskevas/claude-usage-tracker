# Claude Usage — Android app

A native Android client that pairs with the desktop tracker (QR scan) and shows your
**5h / weekly / context / sessions / all-time** stats, plus **push notifications** for
usage alerts. All data is **end-to-end encrypted**; the relay only sees ciphertext.
Contract: [`../docs/REMOTE.md`](../docs/REMOTE.md).

**v0.2.0 (redesign):** warm palette matching the desktop, a Home bento (hero 5h + weekly
+ **extra-usage €** + context + sessions preview), and — for **team admins** — a **Team**
destination showing org spend, near-limit members, and per-member load. Team data reaches
the admin's phone via the E2EE snapshot (no team credentials on the device). Bump on
release: `versionCode`/`versionName` in `app/build.gradle.kts` (already at 15 / 0.2.0).

> **Android only — no iOS.** iOS support is out of scope for now.

> **Not built in CI / this repo's dev box.** There's no Gradle CLI or Firebase config
> here, so the APK is **not** compiled in this environment. Open the project in **Android
> Studio** (which supplies Gradle + the wrapper jar) to build and run.

## Build & run

1. **Android Studio** → *Open* → select this `android/` folder. Let it sync Gradle.
   (CLI alternative once the wrapper jar exists: `./gradlew assembleDebug`.)
2. Phase-1 (viewing, no push) builds **without** any extra setup.
3. Run on a device/emulator (Android 8.0 / API 26+), then tap **Scan pairing QR** and scan
   the QR from the desktop dashboard → *Settings → Remote (phone)*.

## Push notifications (Phase 2) — Firebase

Push needs a Firebase project (yours):

1. Firebase console → create a project → add an Android app with package
   **`com.claudeusage.tracker`**.
2. Download **`google-services.json`** into **`app/`**. The build auto-detects it and
   applies the Google Services plugin (see `app/build.gradle.kts`).
3. Wire the **relay**'s `FCM_*` secrets from the same project's service account
   (see [`../relay/README.md`](../relay/README.md)).
4. Rebuild. On first launch after pairing, the app registers its FCM token with the relay;
   desktop alerts then arrive as notifications. Push messages are **data-only** and
   decrypted on-device, so Google/relay never see the contents.

## How it fits together

```
desktop (producer) --E2EE snapshot/push--> relay (Cloudflare Worker) --> this app (consumer)
```

- `Pairing.kt` parses the `cutpair1:` QR; secrets live in `EncryptedSharedPreferences` (`Prefs.kt`).
- `Crypto.kt` uses libsodium `crypto_secretbox` — byte-for-byte compatible with the
  desktop's PyNaCl.
- `RelayClient.kt` fetches + decrypts the snapshot; `Snapshot.kt` parses it for the UI.
- `PushService.kt` decrypts FCM data messages and posts local notifications.

## Status

Phase 1 (pairing + encrypted viewing) and Phase 2 (push) are implemented. The project is
provided as buildable source; compile/run it in Android Studio. If a dependency version
needs nudging for your Android Studio / AGP, the versions are all in
`build.gradle.kts` / `app/build.gradle.kts`.
