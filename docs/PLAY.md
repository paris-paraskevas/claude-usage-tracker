# Publishing the Android app to Google Play

Status of the build side (done in this repo):
- Release signing wired (`app/build.gradle.kts` reads `keystore.properties`).
- Signed **AAB** builds: `android/app/build/outputs/bundle/release/app-release.aab`.
- Privacy policy: [`PRIVACY.md`](PRIVACY.md). Store assets: `docs/play/`.

## 0. The infrastructure decision (do this first)

A public app can't ask each user to deploy their own relay/Firebase. You host **one
shared relay + one Firebase project** for everyone — the design already supports it (the
relay is multi-tenant and zero-knowledge; each user gets their own keys via the QR).

- Relay: your Worker is live at `https://claude-usage-relay.businessofzeus.workers.dev`.
  Consider a **custom domain** (e.g. `relay.yourapp.com`) so a personal subdomain isn't
  baked into the public desktop app.
- Desktop: set `remote_relay_url` default to your production relay so users don't deploy
  anything (ask me to do this once you've picked the domain).
- Firebase: one project provides FCM for all users (see step 4).

## 1. Generate YOUR upload key (replace the throwaway one)

The repo currently has a **demo** keystore so the AAB builds. Before publishing, make your
own and keep it backed up forever (it signs every future update):

```
cd android
keytool -genkeypair -v -keystore app/upload-keystore.jks -alias upload \
  -keyalg RSA -keysize 2048 -validity 10000
```
Then put your passwords in `android/keystore.properties` (gitignored):
```
storeFile=app/upload-keystore.jks
storePassword=YOUR_STORE_PASSWORD
keyAlias=upload
keyPassword=YOUR_KEY_PASSWORD
```
With **Play App Signing** (default), Google holds the real signing key and this is just
your *upload* key — recoverable if lost, but treat it as a secret.

## 2. Build the release AAB

```
cd android
gradlew bundleRelease            # or the Gradle you already have
# -> app/build/outputs/bundle/release/app-release.aab
```

## 3. Play Console account (you, in a browser)

- Create a developer account at https://play.google.com/console — **$25 one-time**.
- Complete **identity verification** (and, for organizations, D-U-N-S).
- ⚠️ New **personal** developer accounts must run a **closed test with 20+ testers for 14
  consecutive days** before they can request production access. Start this early.

## 4. Firebase for push (you + me)

- You: Firebase console → create project → add Android app, package
  `com.claudeusage.tracker` → download `google-services.json` into `android/app/`.
- You: Project settings → Service accounts → generate a private key.
- Me: set the relay `FCM_*` secrets + rebuild the AAB with Firebase (say "rebuild with Firebase").

## 5. Create the app + store listing (you, in Play Console)

- **App name:** Claude Usage Tracker
- **Default language:** English (US)
- **App or game:** App · **Free**
- **Privacy policy URL:** host `docs/PRIVACY.md` (e.g. GitHub Pages) and paste the URL.
- **Store listing** text: see `docs/play/listing.md`.
- **Graphics:** app icon `docs/play/icon-512.png`, feature graphic `docs/play/feature-1024x500.png`,
  phone screenshots `docs/play/01-pair.png`, `docs/play/02-dashboard.png`.

## 6. Required questionnaires (you)

- **Data safety:** Data is **encrypted in transit**; you (and Google) **cannot** read it.
  Declare: no data collected for analytics/ads; FCM token used only for app functionality;
  data is E2E-encrypted; users can request deletion (unpair). Use `PRIVACY.md` as the basis.
- **Content rating:** complete the questionnaire (utility app → typically "Everyone").
- **Target audience:** not directed at children.
- **Ads:** none. **Government app:** no.

## 7. Releases

- Upload the AAB to **Internal testing** first (instant, you + a few testers).
- Then **Closed testing** (the 20-tester/14-day requirement, if applicable).
- Then **Production** → submit for review (typically a few days).

## Division of labor

- **I can do:** build/sign the AAB, generate icon + screenshots + listing text + privacy
  policy, set the desktop default relay URL, set relay FCM secrets, rebuild with Firebase.
- **Only you can do:** Play Console account + identity verification, hosting the privacy
  policy URL, Firebase project + service-account key (browser, your Google account), filling
  the Data safety / content-rating forms, and clicking submit.
