# Claude Usage — Android app

A native Android client that pairs with the desktop tracker (QR scan) and shows your
**5h / weekly / per-model weekly / context / sessions / all-time** stats, a **home-screen
widget**, an optional **lock-screen notification**, and **push alerts** near a limit. All data
is **end-to-end encrypted**; the relay only sees ciphertext. Contract:
[`../docs/REMOTE.md`](../docs/REMOTE.md).

> **Android only — no iOS.** iOS support is out of scope for now.

## What's in v0.3.0 (versionCode 16)

- **Home:** 5-hour hero gauge + weekly and context gauges, live reset countdowns, a
  **burn-rate** line ("≈7%/h · hits 100% in ~1h 10m", or "steady"), **per-model weekly**
  rows (Opus / Sonnet) when the plan has them, **extra-usage** spend, and a sessions preview.
  Two-column layout on tablets / landscape.
- **Multiple accounts:** pair several desktops; switch from the name at the top of Home.
  Remove / Unpair ask for confirmation and also remove this phone from that desktop's push list.
- **Team pool:** if the desktop is signed in to a team, the Team tab shows the pooled Claude
  **accounts** (5h / weekly per account, month spend per currency, near-limit list) for admins
  and members alike.
- **Conversation (read-only):** shown only when the desktop mirrors conversations
  (Settings → Remote (phone) → *mirror the active conversation*). The phone never sends
  prompts — the old Chat feature is gone.
- **History:** a recent-usage sparkline plus all-time totals.
- **Resilience:** the last snapshot is cached per account, so a cold start or rotation shows
  data immediately; a failed refresh shows a non-blocking "Couldn't refresh — showing data
  from 3 min ago · Retry" banner; "synced … ago" turns amber when the desktop stops syncing; the
  desktop's own problems (e.g. its Claude login expired) are surfaced as a banner.
- **Widget:** responsive (compact / full), reset times, "as of" time (amber when stale), the
  account name when more than one desktop is paired, and a ↻ refresh button. The visual design
  is due for a redesign; this release only fixes its plumbing.
- **Notifications:** permission is requested after pairing (with a one-line reason), not on
  first launch; tapping an alert or the lock-screen card opens the app. The FCM token is re-sent
  to the relay at most once a day from the background refresh, because the relay forgets push
  tokens after 7 days.
- **Look & accessibility:** follows the system light/dark setting (brand dark palette; light
  uses wallpaper colours on Android 12+), themed (monochrome) launcher icon, TalkBack labels on
  gauges, status shown with icons + words (not colour alone), 48 dp touch targets, gauges that
  grow with the font size.

## Build & run

Requirements: JDK 17+, the Android SDK (platform 35, build-tools), and either Android Studio or
the command line. `local.properties` (gitignored) must point at the SDK — Android Studio writes
it; on the CLI create it with `sdk.dir=/path/to/Android/sdk` or set `ANDROID_HOME`.

```
cd android
./gradlew :app:assembleDebug        # -> app/build/outputs/apk/debug/app-debug.apk
./gradlew :app:lintDebug
./gradlew :app:bundleRelease        # signed AAB if keystore.properties exists (see ../docs/PLAY.md)
```

The Gradle wrapper (Gradle 8.11.1) is included, so no local Gradle install is needed.
Run on a device/emulator (Android 8.0 / API 26+), tap **Scan pairing QR** and scan the QR from
the desktop dashboard → *Settings → Remote (phone)* (or open a `cutpair1:` link and confirm).

## Push notifications — Firebase

Viewing works without Firebase; push needs a Firebase project:

1. Firebase console → create a project → add an Android app with package
   **`com.claudeusage.tracker`**.
2. Download **`google-services.json`** into **`app/`** (gitignored). The build auto-detects it
   and applies the Google Services plugin (see `app/build.gradle.kts`); without it the build
   still succeeds and push is simply off.
3. Wire the **relay**'s `FCM_*` secrets from the same project's service account
   (see [`../relay/README.md`](../relay/README.md)).
4. Rebuild. After pairing, the app registers its FCM token with the relay; desktop alerts then
   arrive as notifications. Push messages are **data-only** and decrypted on-device, so
   Google/relay never see the contents.

## How it fits together

```
desktop (producer) --E2EE snapshot/push--> relay (Cloudflare Worker) --> this app (consumer)
```

- `Pairing.kt` parses the `cutpair1:` QR; secrets live in `EncryptedSharedPreferences`
  (`Prefs.kt`, one cached instance per process — the library is deprecated but kept so existing
  pairings don't need migrating).
- `Crypto.kt` uses libsodium `crypto_secretbox` (lazysodium) — byte-for-byte compatible with the
  desktop's PyNaCl. The phone only decrypts.
- `RelayClient.kt` fetches + decrypts the snapshot and adds/removes the push token;
  `Snapshot.kt` parses it for the UI.
- `ui/DashboardViewModel.kt` holds the dashboard state across rotation; `ui/DashboardScreen.kt`
  and `ui/Components.kt` render it.
- `widget/` — the Glance widget, the WorkManager refresh (every 15 min) and the lock-screen
  notification; `push/` — FCM receive + token bookkeeping.

## Release checklist

- Bump `versionCode` / `versionName` in `app/build.gradle.kts`.
- Native libraries (libsodium, JNA) are 16 KB page-aligned as of lazysodium-android 5.2.0 /
  JNA 5.17.0 — keep them at or above those versions (Play requirement for API 35+ targets).
- `isMinifyEnabled` is off for release; turning on R8 needs keep rules for lazysodium/JNA,
  zxing and Firebase and a tested release build.
