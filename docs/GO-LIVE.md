# v0.3.0 go-live runbook (owner)

Step-by-step for the gates that need dashboard / billing / Cloudflare / Play access — the
things the codebase can't do for itself. Paths are current as of 2026-07-08; `_` in a Supabase
URL means "your project/org". Compliance rationale lives in [COMPLIANCE.md](./COMPLIANCE.md);
the release plan in [RELEASE-0.3.md](./RELEASE-0.3.md).

Project: `sxciunvkygtehhztfjjo` · https://sxciunvkygtehhztfjjo.supabase.co

---

## 0. GATE — Anthropic ToS (do this first; blocks everything below)
The core OAuth-token→`oauth/usage` mechanism reads as **likely against Anthropic's Claude Code
terms** (see COMPLIANCE.md). **Do not publish the public "hosted-for-everyone" 0.3.0 unless**:
- you obtain written permission from Anthropic ([contact sales](https://www.anthropic.com/contact-sales)), **or**
- you accept the risk explicitly and in writing (understanding enforcement can happen without notice).

If neither: keep 0.3.0 private/self-host-only, or pause the public release. Everything below
is prep that is safe to complete regardless.

---

## 1. Supabase auth hardening
Dashboard → your project. Do these in order (SMTP before OTP-as-code).

### 1a. Custom SMTP  (required for public — the built-in sender is rate-limited to a few/hour)
1. **Authentication → Emails → SMTP Settings → Enable Custom SMTP.**
2. Enter your provider's host, port, username, password, sender email + sender name
   (e.g. Resend/Postmark/SES). Save.
3. **Authentication → Rate Limits** (`/dashboard/project/_/auth/rate-limits`) → set a sane
   "emails per hour" for expected sign-in volume.

### 1b. OTP as a 6-digit code, 300s expiry
1. **Authentication → Email Templates → Magic Link** template: include the token variable so a
   code is sent instead of a link, e.g. `Your code: {{ .Token }}`. (The desktop client already
   verifies codes via `sign_in_verify_code`; magic-link paste is only the pre-SMTP fallback.)
2. **Authentication → Providers → Email → Email OTP Expiration** = `300` (seconds). (Max allowed
   is 86400; 300 is the target.)

### 1c. Access token (JWT) expiry = 30 min
1. **Project Settings → JWT Keys** (Legacy JWT Secret section) → **Access token (JWT) expiry
   time** = `1800` seconds. (Docs discourage < 5 min; 1800 is fine.)

### 1d. CAPTCHA (Cloudflare Turnstile)  ⚠ needs a small client change too
1. Cloudflare dashboard → **Turnstile → create widget** → copy **Sitekey** + **Secret Key**.
2. Supabase → **Authentication → Attack Protection** (`/dashboard/project/_/auth/protection`)
   → **Enable CAPTCHA protection** → provider **Turnstile** → paste **Secret key** → Save.
3. **⚠ Client dependency:** once CAPTCHA is ON, every `/auth/v1/otp` call must carry a token or
   it fails. `supabase_pool.sign_in_start(captcha_token=…)` already forwards it, **but the login
   webview must render the Turnstile widget** (embed the Turnstile JS with the Sitekey, capture
   the token, pass it in). Do **not** enable 1d until that widget ships, or sign-in breaks.

### 1e. (optional, moot for OTP) Leaked-password protection
Auth advisor flags it, but this project is OTP-only (no passwords). Enable if you like:
**Authentication → Providers → Password / Attack Protection → Leaked password protection**.

---

## 2. Supabase Pro + egress
Free tier's **5 GB** egress will hit a `402` service restriction under public load (Pro = 250 GB).
1. **Organization → Billing** (`/dashboard/org/_/billing`) → change subscription → **Pro** (~$25/mo).
2. **Cost Control** (same page) → decide **Spend Cap**: ON = hard-capped (services 402 when the
   quota is exceeded) · OFF = pay overage ($0.09/GB uncached beyond 250 GB). For a public launch,
   OFF + monitoring is usually safer than a hard 402.
3. Monitor at **Organization → Usage** (`/dashboard/org/_/usage`).

---

## 3. Supabase DPA + EU region  (GDPR — you are the controller)
1. **Organization → Documents → Legal Documents** → generate the **DPA** (PandaDoc) → complete
   Part 1 (controller details) → e-sign. This also executes the EU SCCs.
2. Confirm the project region is in the **EU**: **Project Settings → General → Region**. Region is
   **fixed at creation** — if it is not EU, you must recreate the project in an EU region and
   re-run the migration (`supabase/migrations/0001_pool.sql`).

---

## 4. Deploy + verify the Worker connector  (needs Cloudflare auth)
The relay half (`relay/src/index.js`, commit `dcd3b18`) is committed but **unverified/undeployed**.
```
npx wrangler login                       # Cloudflare auth (one-time)
cd relay
npx wrangler secret put SUPABASE_SECRET  # paste the sb_secret_… service key — NEVER commit it
# confirm wrangler.toml has [vars] SUPABASE_URL = "https://sxciunvkygtehhztfjjo.supabase.co"
npx wrangler deploy
```
**Verify:** mint a connector token in the desktop app (Team tab → "Mint connector token"),
paste it at the claude.ai connector consent screen → it returns *your team's* pool; an
expired/foreign token is rejected.

---

## 5. Screenshots
Scrubbed pool captures are prepared (`team-pool.scrubbed.png`, `team-login.scrubbed.png` — real
email/domain/device-id removed, broken "OFFLINE/retrying" chrome cropped). **Still missing:** a
true **login + consent** capture (the current "login" shot is actually a signed-in pool view). To
get one: run the app signed-out → Team tab → screenshot the login form with the consent checkbox.

---

## 6. Merge + release
1. Land the unmerged work on a fresh branch off `main` (PR #45 is already merged): today's
   latest-wins model, the relay connector, the `0.3.0` bump, the new docs, the scrubbed
   screenshots → open a PR → merge.
2. `git tag v0.3.0 && git push --tags` (push only when you're ready — it's public).
3. GitHub Release: note the **breaking change** (0.2.x team users re-onboard via email OTP; join
   codes + token escrow removed) and the new sign-in flow.
4. PyPI: `python -m build && twine upload dist/*` (users then `pipx install claude-usage-tracker`).
   Installer: build with `ISCC /DMyAppVersion=0.3.0 installer.iss`.

---

## 7. Android companion — AAB  (Android Studio only; not buildable from this repo as-is)
The repo has **no Gradle wrapper jar and no Firebase config**, so the `.aab` must be built in
Android Studio on a machine that has them.
1. Open `android/` in Android Studio; add your `google-services.json` (Firebase / FCM).
2. Bump `versionCode`/`versionName` to match `0.3.0`.
3. **Build → Generate Signed Bundle / APK → Android App Bundle**, sign with your release keystore
   → produces `app-release.aab`.
4. Play Console → your app → **Production** (or Internal testing first) → **Create release** →
   upload the `.aab` → roll out.
