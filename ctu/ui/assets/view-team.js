// Team pool view (spec §7).
//  Signed out: email -> code -> display name, with the consent note.
//  Signed in:  pool table (best headroom first, you marked), month ledger with
//              a picker capped at the current month + CSV export, and the
//              admin-only claude.ai connector token card.

import { patch, esc, timeSpan, copyText } from "./dom.js";
import { icon, levelIcon } from "./icons.js";
import { bullet } from "./meter.js";
import { act, getJSON, toast } from "./actions.js";
import { refetch } from "./state.js";
import {
  percent, currency, tokens, when, monthLabel, monthKey, addMonths, asOf, LEVEL_WORD, integer, monthShortOfKey,
} from "./format.js";
import { sortPool } from "./model.js";

export const title = "Team pool";

const CONSENT =
  "Your usage numbers (5-hour and weekly %, extra-usage spend, token counts) are shared with teammates on your email domain. Never sessions, projects or conversation content.";

let root = null;
let st = null;
let meta = null;
let nowFn = () => Date.now() / 1000;
let form = { step: "email", email: "", code: "", username: "", error: "", busy: false, done: false };
let ledger = { month: null, status: "idle", data: null, error: "" };
let token = { status: "idle", value: "", expires: null, error: "", copied: false };

const fin = (v) => typeof v === "number" && Number.isFinite(v);

export function mount(el, ctx) {
  root = el;
  nowFn = ctx.now;
  root.addEventListener("click", onClick);
  root.addEventListener("submit", onSubmit);
  root.addEventListener("input", onInput);
  render();
}
export function unmount() {
  root.removeEventListener("click", onClick);
  root.removeEventListener("submit", onSubmit);
  root.removeEventListener("input", onInput);
  root = null;
  token = { status: "idle", value: "", expires: null, error: "", copied: false };
}
export function update(s, m) {
  const wasIn = signedIn(st);
  st = s;
  meta = m;
  if (signedIn(st) && (!wasIn || ledger.status === "idle")) loadLedger(ledger.month || currentMonth());
  if (!signedIn(st) && wasIn) form = { step: "email", email: "", code: "", username: "", error: "", busy: false, done: false };
  render();
}

function signedIn(s) {
  return !!(s && s.team && s.team.signed_in);
}
function currentMonth() {
  const d = ledger.data;
  if (d && d.current_month) return d.current_month;
  return monthKey(nowFn(), d && d.tz ? { timeZone: d.tz } : undefined);
}

/* ---------------- render ---------------- */

function render() {
  if (!root) return;
  const now = nowFn();
  if (!st) {
    if (meta && meta.conn === "offline") {
      patch(root, `<header class="page-head"><h1 class="t-title">Team pool</h1></header><section class="card"><div class="empty">${levelIcon("stale", 24)}<div class="empty-title">Can't reach the tracker</div><p class="empty-body">The background app isn't answering, so the pool can't be shown.</p><button type="button" class="btn btn-accent" data-act="retry">Try again</button></div></section>`);
    } else {
      patch(root, `<header class="page-head"><h1 class="t-title">Team pool</h1></header><div aria-busy="true"><span class="sr-only">Loading team pool…</span><div class="skel" style="height:220px"></div></div>`);
    }
    return;
  }
  patch(root, signedIn(st) ? signedInHTML(now) : signInHTML());
}

function signInHTML() {
  const f = form;
  const steps = [
    ["email", "Email"],
    ["code", "Code"],
    ["username", "Your name"],
  ];
  const idx = steps.findIndex((s) => s[0] === f.step);
  const stepper = `<ol class="stepper" aria-label="Sign-in steps">${steps
    .map(([k, l], i) => `<li class="${i < idx ? "is-done" : i === idx ? "is-current" : ""}"${i === idx ? ' aria-current="step"' : ""}><span class="step-n" aria-hidden="true">${i < idx ? icon("check", { size: 12 }) : i + 1}</span>${l}</li>`)
    .join("")}</ol>`;
  const err = f.error ? `<div class="field-error" role="alert" id="tf-err">${levelIcon("error", 12)}<span>${esc(f.error)}</span></div>` : "";
  const busy = f.busy ? " disabled aria-busy=\"true\"" : "";
  let body;
  if (f.done) {
    body = `<div class="empty"><div class="empty-title">You're in</div><p class="empty-body">Loading the pool…</p></div>`;
  } else if (f.step === "email") {
    body = `<form class="signin-form" data-form="email" novalidate>
      <div class="field">
        <label for="tf-email">Work email</label>
        <input class="input" id="tf-email" data-key="tf-email" name="email" type="email" autocomplete="email" spellcheck="false" required value="${esc(f.email)}" aria-describedby="tf-email-hint${f.error ? " tf-err" : ""}"${f.error ? ' aria-invalid="true"' : ""}>
        <div class="field-hint" id="tf-email-hint">We'll email you a 6-digit code. Teammates with the same email domain share one pool.</div>
        ${err}
      </div>
      <div class="form-actions"><button type="submit" class="btn btn-accent" data-key="tf-send"${busy}>${f.busy ? "Sending…" : "Send code"}</button></div>
    </form>`;
  } else if (f.step === "code") {
    body = `<form class="signin-form" data-form="code" novalidate>
      <div class="field">
        <label for="tf-code">Code from the email sent to <b>${esc(f.email)}</b></label>
        <input class="input input-code" id="tf-code" data-key="tf-code" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="8" required value="${esc(f.code)}" aria-describedby="tf-code-hint${f.error ? " tf-err" : ""}"${f.error ? ' aria-invalid="true"' : ""}>
        <div class="field-hint" id="tf-code-hint">It can take a minute to arrive. Check your spam folder too.</div>
        ${err}
      </div>
      <div class="form-actions">
        <button type="submit" class="btn btn-accent" data-key="tf-verify"${busy}>${f.busy ? "Checking…" : "Verify"}</button>
        <button type="button" class="btn btn-subtle" data-act="resend" data-key="tf-resend"${busy}>Send a new code</button>
        <button type="button" class="btn btn-subtle" data-act="change-email" data-key="tf-change">Use a different email</button>
      </div>
    </form>`;
  } else {
    body = `<form class="signin-form" data-form="username" novalidate>
      <div class="field">
        <label for="tf-name">Your name in the pool</label>
        <input class="input" id="tf-name" data-key="tf-name" name="username" autocomplete="nickname" maxlength="40" required value="${esc(f.username)}" aria-describedby="tf-name-hint${f.error ? " tf-err" : ""}"${f.error ? ' aria-invalid="true"' : ""}>
        <div class="field-hint" id="tf-name-hint">Teammates see this next to your usage.</div>
        ${err}
      </div>
      <div class="form-actions"><button type="submit" class="btn btn-accent" data-key="tf-join"${busy}>${f.busy ? "Joining…" : "Join the pool"}</button></div>
    </form>`;
  }
  return `<header class="page-head"><h1 class="t-title">Team pool</h1></header>
  <section class="card signin" aria-labelledby="signin-title">
    <div class="signin-intro">
      <span class="signin-ic">${icon("team", { size: 24 })}</span>
      <div>
        <h2 class="t-subtitle" id="signin-title">Share headroom with your team</h2>
        <p class="muted">See which teammate's account has room left before you hit a limit, and what extra usage cost this month.</p>
      </div>
    </div>
    ${stepper}
    ${body}
    <div class="consent">${icon("lock")}<p>${esc(CONSENT)}</p></div>
  </section>`;
}

function signedInHTML(now) {
  const t = st.team;
  const role = t.role === "admin" ? "You're an admin" : "Member";
  const head = `<header class="page-head">
    <div><h1 class="t-title">Team pool</h1><p class="page-sub">${esc(t.team || "")}${t.team ? " · " : ""}${esc(t.email || "")} · ${role}</p></div>
    <div class="head-meta"><button type="button" class="btn" data-act="logout" data-key="logout">${icon("sign-out")}Sign out of pool</button></div>
  </header>`;
  const callouts = [];
  if (t.device_ok === false) {
    callouts.push(`<div class="callout tone-watch">${levelIcon("watch", 16)}<div class="callout-body"><b>This PC isn't linked to ${esc(t.email)} yet.</b> Link it so your usage counts in the pool.</div><button type="button" class="btn" data-act="claim" data-key="claim">${icon("link")}Link this PC</button></div>`);
  }
  const sug = st.decision && st.decision.suggestion;
  if (sug && sug.email) {
    callouts.push(`<div class="callout">${icon("sparkle")}<div class="callout-body"><b>${esc(sug.email)}</b> has ${esc(percent(sug.five_hour_left_pct))} of its 5-hour limit left.</div><button type="button" class="btn btn-accent" data-act="switch" data-email="${esc(sug.email)}" data-key="switch" aria-description="Opens Claude sign-in so you can log in as that account" title="Opens Claude sign-in so you can log in as that account">Switch to ${esc(sug.email)}</button></div>`);
  }
  return head + callouts.join("") + poolHTML(now) + ledgerHTML() + (t.role === "admin" ? connectorHTML() : "");
}

function poolHTML(now) {
  const pool = sortPool(st.team.pool || []);
  const month = monthShortOfKey(currentMonth() + "-15");
  if (!pool.length) {
    return `<section class="card" aria-labelledby="pool-h"><div class="card-head"><h2 id="pool-h">Pool accounts</h2></div><div class="empty"><div class="empty-title">No accounts in the pool yet</div><p class="empty-body">Accounts appear here once teammates on ${esc(st.team.team || "your domain")} sign in with the tracker running.</p></div></section>`;
  }
  const rows = pool
    .map((r) => {
      const staleRow = fin(r.updated_at) && now - r.updated_at > 15 * 60;
      const lvl = r.level || "ok";
      const left = fin(r.five_hour_pct) ? Math.max(0, 100 - r.five_hour_pct) : null;
      return `<tr class="${r.is_me ? "is-me" : ""}${staleRow ? " is-stale-row" : ""}">
        <td class="c-acct"><div class="cell-main">${esc(r.name || r.email)}${r.is_me ? ' <span class="pill pill-accent you">You</span>' : ""}</div><div class="cell-sub">${esc(r.email)}<span class="dev-inline"> · ${esc(r.device || "")}</span></div></td>
        <td class="c-5h"><div class="pool-5h"><span class="num t-body-strong">${esc(percent(r.five_hour_pct))}</span>${bullet({ used: r.five_hour_pct, tone: staleRow ? "stale" : lvl, size: "sm", label: `${r.email}: 5-hour ${percent(r.five_hour_pct)} used, ${left === null ? "" : percent(left) + " left, "}refills ${when(r.five_hour_resets_at, now)}` })}</div><div class="cell-sub">refills ${esc(when(r.five_hour_resets_at, now))}</div></td>
        <td class="c-7d r num">${esc(percent(r.seven_day_pct))}</td>
        <td class="c-lvl"><span class="chip tone-${staleRow ? "stale" : lvl}">${levelIcon(staleRow ? "stale" : lvl, 12)}${esc(staleRow ? "Out of date" : LEVEL_WORD[lvl] || lvl)}</span></td>
        <td class="c-spend r num">${esc(currency(r.month_spend, r.currency))}</td>
        <td class="c-dev"><div class="cell-main">${esc(r.device || "—")}</div><div class="cell-sub">${staleRow ? esc(asOf(r.updated_at, now)) : timeSpan(r.updated_at, "ago", now)}</div></td>
      </tr>`;
    })
    .join("");
  return `<section class="card table-card" aria-labelledby="pool-h">
    <div class="card-head card-head-pad"><h2 id="pool-h">Pool accounts</h2><span class="card-sub">Most 5-hour headroom first</span></div>
    <div class="tbl-wrap"><table class="tbl pool-tbl">
      <caption class="sr-only">Pool accounts, most 5-hour headroom first</caption>
      <thead><tr><th scope="col">Account</th><th scope="col">5-hour</th><th scope="col" class="r">Weekly</th><th scope="col">Status</th><th scope="col" class="r">Spend · ${esc(month)}</th><th scope="col" class="c-dev">Last device</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
  </section>`;
}

function ledgerHTML() {
  const cur = currentMonth();
  const m = ledger.month || cur;
  const atMax = m >= cur;
  const picker = `<div class="month-picker">
    <button type="button" class="btn btn-subtle btn-icon btn-sm" data-act="month-prev" data-key="m-prev" aria-label="Previous month">${icon("chevron-left")}</button>
    <span class="month-label" aria-live="polite">${esc(monthLabel(m))}</span>
    <button type="button" class="btn btn-subtle btn-icon btn-sm" data-act="month-next" data-key="m-next" aria-label="Next month"${atMax ? " disabled" : ""}>${icon("chevron-right")}</button>
  </div>`;
  const canExport = ledger.status === "ok" && ledger.data && (ledger.data.rows || []).length;
  const exportBtn = `<button type="button" class="btn btn-sm" data-act="csv" data-key="csv"${canExport ? "" : " disabled"}>${icon("download")}Export CSV</button>`;
  let body;
  if (ledger.status === "loading" || ledger.status === "idle") {
    body = `<div aria-busy="true" class="ledger-loading"><span class="sr-only">Loading the ledger…</span><div class="skel" style="height:16px"></div><div class="skel" style="height:16px"></div><div class="skel" style="height:16px;width:60%"></div></div>`;
  } else if (ledger.status === "error") {
    body = `<div class="empty">${levelIcon("error", 20)}<div class="empty-title">Couldn't load the ledger</div><p class="empty-body">${esc(ledger.error)}</p><button type="button" class="btn" data-act="ledger-retry">Try again</button></div>`;
  } else {
    const d = ledger.data || {};
    const rows = d.rows || [];
    if (!rows.length) {
      body = `<div class="empty"><div class="empty-title">No extra usage in ${esc(monthLabel(m))}</div><p class="empty-body">Nobody in the pool used extra usage this month, or no readings were recorded.</p></div>`;
    } else {
      const STATE = { live: "Live", end_of_day: "End of day", closed: "Closed" };
      const tr = rows
        .map((r) => `<tr class="${r.is_me ? "is-me" : ""}">
          <td><div class="cell-main">${esc(r.name || r.email)}${r.is_me ? ' <span class="pill pill-accent you">You</span>' : ""}</div><div class="cell-sub">${esc(r.email || "")}</div></td>
          <td class="r num t-body-strong">${esc(currency(r.spend, r.currency))}</td>
          <td class="r num c-opt">${esc(tokens(r.tokens))}</td>
          <td class="r num c-opt">${esc(currency(r.meter_end, r.currency))}</td>
          <td class="r num c-opt">${r.cap == null ? "—" : esc(currency(r.cap, r.currency))}</td>
          <td class="r num">${esc(integer(r.days))}</td>
          <td>${esc(STATE[r.state] || r.state || "—")}</td>
        </tr>`)
        .join("");
      const totals = (d.totals || []).map((t) => `${esc(currency(t.spend, t.currency))}`).join(" + ");
      body = `<div class="tbl-wrap"><table class="tbl ledger-tbl">
        <caption class="sr-only">Extra usage per account in ${esc(monthLabel(m))}</caption>
        <thead><tr><th scope="col">Account</th><th scope="col" class="r">Spend</th><th scope="col" class="r c-opt">Tokens</th><th scope="col" class="r c-opt">Meter at end</th><th scope="col" class="r c-opt">Cap</th><th scope="col" class="r">Days</th><th scope="col">Status</th></tr></thead>
        <tbody>${tr}</tbody>
        ${totals ? `<tfoot><tr><th scope="row">Total</th><td class="r num t-body-strong">${totals}</td><td colspan="5"></td></tr></tfoot>` : ""}
      </table></div>`;
    }
  }
  const tzNote = ledger.data && ledger.data.tz ? ` Months follow the team clock (${esc(ledger.data.tz)}).` : "";
  return `<section class="card table-card" aria-labelledby="ledger-h">
    <div class="card-head card-head-pad"><h2 id="ledger-h">Extra usage ledger</h2><div class="ledger-tools">${picker}${exportBtn}</div></div>
    <div class="ledger-body">${body}</div>
    <p class="card-foot t-caption muted">Spend is extra usage billed in the month, worked out from each account's meter.${tzNote}</p>
  </section>`;
}

function connectorHTML() {
  let body;
  if (token.status === "done") {
    body = `<div class="field">
      <label for="tok-val">Connector token</label>
      <div class="token-row"><input class="input token-input" id="tok-val" data-key="tok-val" readonly value="${esc(token.value)}" aria-describedby="tok-warn"><button type="button" class="btn" data-act="copy-token" data-key="copy-token">${icon(token.copied ? "check" : "copy")}${token.copied ? "Copied" : "Copy"}</button></div>
      <div class="field-hint tone-watch" id="tok-warn">${levelIcon("watch", 12)} Copy it now. It won't be shown again${token.expires ? `, and it stops working ${esc(when(token.expires, nowFn()))}` : ""}.</div>
    </div>`;
  } else {
    body = `<div><button type="button" class="btn" data-act="mint" data-key="mint"${token.status === "busy" ? ' disabled aria-busy="true"' : ""}>${icon("key")}${token.status === "busy" ? "Creating…" : "Create token"}</button></div>
    ${token.error ? `<div class="field-error" role="alert">${levelIcon("error", 12)}<span>${esc(token.error)}</span></div>` : ""}`;
  }
  return `<section class="card" aria-labelledby="conn-h">
    <div class="card-head"><h2 id="conn-h">claude.ai connector</h2><span class="pill pill-outline">Admins only</span></div>
    <p>Let Claude answer questions about your team's usage. Create a token, then paste it when you add the connector in claude.ai.</p>
    ${body}
  </section>`;
}

/* ---------------- ledger ---------------- */

async function loadLedger(month) {
  const cur = currentMonth();
  if (month > cur) month = cur;
  ledger = { ...ledger, month, status: "loading", error: "" };
  render();
  try {
    const d = await getJSON("/api/v2/team/ledger?month=" + encodeURIComponent(month));
    if (ledger.month !== month) return;
    if (d && d.error) throw new Error(d.error === "unreachable" ? "Can't reach the team service right now. Check your connection and try again." : d.error);
    ledger = { month, status: "ok", data: d, error: "" };
  } catch (e) {
    if (ledger.month !== month) return;
    ledger = { month, status: "error", data: ledger.data, error: e.message || "Something went wrong." };
  }
  render();
}

function csvCell(v) {
  let s = v == null ? "" : String(v);
  if (/^[=+\-@\t\r]/.test(s)) s = "'" + s; // keep spreadsheets from running formulas
  return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}
/** CSV for the loaded ledger month (client-side). */
export function ledgerCsv(d) {
  const head = ["account", "email", "month", "spend", "currency", "tokens", "meter_end", "cap", "days_sampled", "status"];
  const lines = [head.join(",")];
  for (const r of d.rows || []) {
    lines.push([r.name || r.email, r.email, d.month, r.spend, r.currency, r.tokens, r.meter_end, r.cap, r.days, r.state].map(csvCell).join(","));
  }
  return lines.join("\r\n") + "\r\n";
}
function downloadCsv() {
  const d = ledger.data;
  if (!d) return;
  const blob = new Blob(["﻿" + ledgerCsv(d)], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `claude-team-ledger-${d.month || ledger.month}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

/* ---------------- events ---------------- */

function onInput(e) {
  const n = e.target.name;
  if (n === "email" || n === "code" || n === "username") {
    form[n] = e.target.value;
    if (form.error) {
      form.error = "";
      render();
    }
  }
}

async function onSubmit(e) {
  e.preventDefault();
  const kind = e.target.getAttribute("data-form");
  if (form.busy) return;
  if (kind === "email") {
    const email = form.email.trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      form.error = "Enter a full email address, like you@company.com.";
      return render();
    }
    form.busy = true;
    render();
    await act("team_login_start", { email }, { onError: (err) => (form.error = err.message) });
    form.busy = false;
    if (!form.error) {
      form.email = email;
      form.step = "code";
      form.code = "";
    }
    render();
    focusField();
  } else if (kind === "code" || kind === "username") {
    const code = form.code.replace(/\s+/g, "");
    if (!/^\d{4,8}$/.test(code)) {
      form.error = "Enter the digits from the email.";
      form.step = "code";
      return render();
    }
    const body = { email: form.email.trim(), code };
    if (kind === "username") {
      const u = form.username.trim();
      if (!u) {
        form.error = "Enter the name teammates will see.";
        return render();
      }
      body.username = u;
    }
    form.busy = true;
    render();
    const res = await act("team_login_verify", body, { onError: (err) => (form.error = err.message) });
    form.busy = false;
    if (res) {
      if (res.needs_username && kind !== "username") form.step = "username";
      else {
        form.done = true;
        refetch();
      }
    }
    render();
    focusField();
  }
}
function focusField() {
  const el = root && root.querySelector(".signin-form input");
  if (el) el.focus();
}

async function onClick(e) {
  const b = e.target.closest("[data-act]");
  if (!b) return;
  const a = b.getAttribute("data-act");
  if (a === "retry") refetch();
  else if (a === "change-email") {
    form = { ...form, step: "email", code: "", error: "" };
    render();
    focusField();
  } else if (a === "resend") {
    const ok = await act("team_login_start", { email: form.email.trim() }, { button: b, onError: (err) => (form.error = err.message) });
    if (ok) toast({ title: "New code sent", detail: form.email, kind: "success" });
    render();
  } else if (a === "logout") {
    await act("team_logout", {}, { button: b, success: "Signed out of the pool" });
    refetch();
  } else if (a === "claim") {
    await act("team_claim_device", {}, { button: b, success: "This PC is linked" });
    refetch();
  } else if (a === "switch") {
    await act("login", { email: b.getAttribute("data-email") }, { button: b, success: "Opening Claude sign-in…" });
  } else if (a === "month-prev") loadLedger(addMonths(ledger.month || currentMonth(), -1));
  else if (a === "month-next") loadLedger(addMonths(ledger.month || currentMonth(), 1));
  else if (a === "ledger-retry") loadLedger(ledger.month || currentMonth());
  else if (a === "csv") downloadCsv();
  else if (a === "mint") {
    token = { status: "busy", value: "", expires: null, error: "", copied: false };
    render();
    const res = await act("team_connector_token", {}, { onError: (err) => (token.error = err.message) });
    if (res && res.token) token = { status: "done", value: res.token, expires: res.expires_at || null, error: "", copied: false };
    else token = { ...token, status: "idle", error: token.error || "The tracker didn't return a token." };
    render();
  } else if (a === "copy-token") {
    const ok = await copyText(token.value);
    token.copied = ok;
    if (!ok) toast({ title: "Couldn't copy", detail: "Select the token and press Ctrl+C.", kind: "error" });
    render();
  }
}
