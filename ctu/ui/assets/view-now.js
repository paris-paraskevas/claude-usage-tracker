// Now view (spec §7): header -> verdict banner -> runway chart -> limit cards
// -> active session / waiting for you / extra usage.

import { patch, esc, timeSpan, announce } from "./dom.js";
import { icon, levelIcon } from "./icons.js";
import { bullet, limitSentence } from "./meter.js";
import { runwayModel, renderRunway, valueAt } from "./runway.js";
import { act, getJSON } from "./actions.js";
import { refetch } from "./state.js";
import {
  percent, when, clock, currency, tokens, monthName, prettyModel, limitNoun, asOf, rate,
} from "./format.js";
import {
  bindingLimit, limitById, toneOf, levelWord, verdict, isStale, waitingSessions, activeSession, serviceInfo,
} from "./model.js";

export const title = "Now";

let root = null;
let st = null;
let meta = null;
let nowFn = () => Date.now() / 1000;
let ro = null;
let chartW = 0;
let model = null; // current runway model, for hover
let lastVerdict = "";
let hist = { key: "", at: 0, data: [], loading: false, failed: false };
let hovering = false;
let pending = false;

const fin = (v) => typeof v === "number" && Number.isFinite(v);
const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);

export function mount(el, ctx) {
  root = el;
  hovering = false;
  pending = false;
  nowFn = ctx.now;
  root.addEventListener("click", onClick);
  root.addEventListener("pointermove", onHover);
  root.addEventListener("pointerleave", hideHover);
  ro = new ResizeObserver(() => {
    const wrap = root.querySelector(".rw-wrap");
    if (wrap && Math.abs(wrap.clientWidth - chartW) > 1) render();
  });
  ro.observe(root);
  render();
}
export function unmount() {
  if (ro) ro.disconnect();
  root.removeEventListener("click", onClick);
  root.removeEventListener("pointermove", onHover);
  root.removeEventListener("pointerleave", hideHover);
  root = null;
}
export function update(state, m) {
  st = state;
  meta = m;
  if (hovering) pending = true; // keep the tooltip steady; render when the pointer leaves
  else render();
  maybeFetchHistory();
}
export function tick() {
  if (!hovering) render();
}

/* ---------------- history for the runway ---------------- */

async function maybeFetchHistory() {
  const L = bindingLimit(st);
  if (!L) return;
  const key = L.id + ":" + L.resets_at;
  const age = Date.now() - hist.at;
  if (hist.loading || (hist.key === key && age < 60000)) return;
  hist.loading = true;
  try {
    const data = await getJSON("/api/v2/history?window=" + encodeURIComponent(L.id));
    const samples = Array.isArray(data) ? data : data.samples || data.points || [];
    hist = { key, at: Date.now(), data: samples, loading: false, failed: false };
  } catch {
    hist = { key, at: Date.now(), data: hist.key === key ? hist.data : [], loading: false, failed: true };
  }
  if (root) render();
}

/* ---------------- render ---------------- */

function render() {
  if (!root) return;
  const now = nowFn();
  if (!st) {
    patch(root, meta && meta.conn === "offline" ? errorHTML() : loadingHTML());
    return;
  }
  const stale = isStale(st);
  const html = [
    headHTML(now),
    verdictHTML(now),
    st.limits && st.limits.length ? runwayHTML(now, stale) + limitCardsHTML(now, stale) : noLimitsHTML(),
    smallCardsHTML(now, stale),
  ].join("");
  patch(root, `<div class="stack now${stale ? " is-stale" : ""}">${html}</div>`);
  // second pass when the chart's real width differs from the estimate
  const wrap = root.querySelector(".rw-wrap");
  if (wrap && Math.abs(wrap.clientWidth - chartW) > 1) {
    chartW = wrap.clientWidth;
    render();
    return;
  }
  // announce verdict changes
  const v = verdict(st, now);
  const key = v.tone + "|" + v.title;
  if (lastVerdict && key !== lastVerdict) announce(v.title);
  lastVerdict = key;
}

function loadingHTML() {
  return `<div class="stack now" aria-busy="true">
    <header class="page-head"><h1 class="t-title">Now</h1></header>
    <span class="sr-only">Loading usage…</span>
    <div class="skel" style="height:120px"></div>
    <div class="skel" style="height:300px"></div>
    <div class="grid-2"><div class="skel" style="height:160px"></div><div class="skel" style="height:160px"></div></div>
  </div>`;
}

function errorHTML() {
  return `<div class="stack now">
    <header class="page-head"><h1 class="t-title">Now</h1></header>
    <section class="card"><div class="empty">
      ${levelIcon("stale", 24)}
      <div class="empty-title">Can't reach the tracker</div>
      <p class="empty-body">The background app isn't answering. If you just started it, give it a few seconds. If it keeps happening, restart Claude Usage Tracker from the Start menu.</p>
      <button type="button" class="btn btn-accent" data-act="retry">Try again</button>
    </div></section>
  </div>`;
}

function headHTML(now) {
  const svc = serviceInfo(st.service);
  const s = st.service || {};
  const svcInner = `${levelIcon(svc.tone, 14)}<span>${esc(svc.text)}</span>`;
  const svcHTML = s.url && s.indicator !== "none"
    ? `<a class="svc-chip" href="${esc(s.url)}" target="_blank" rel="noopener">${svcInner}${icon("external", { size: 12 })}</a>`
    : `<span class="svc-chip">${svcInner}</span>`;
  const src = st.source || {};
  const upd = isStale(st)
    ? `<span class="upd tone-stale">${levelIcon("stale", 12)}${timeSpan(src.updated_at, "asof", now)}</span>`
    : `<span class="upd">Updated ${esc(fin(src.updated_at) ? clock(src.updated_at) : "—")}</span>`;
  return `<header class="page-head">
    <h1 class="t-title">Now</h1>
    <div class="head-meta">${svcHTML}${upd}
      <button type="button" class="btn btn-icon" data-act="refresh" data-key="refresh" aria-label="Refresh now" title="Refresh now">${icon("refresh")}</button>
    </div>
  </header>`;
}

function verdictHTML(now) {
  const v = verdict(st, now);
  const d = st.decision || {};
  const mute = st.mute || {};
  const actions = [];
  const sug = d.suggestion;
  if (sug && sug.email && !isStale(st)) {
    actions.push(`<button type="button" class="btn btn-accent" data-act="switch" data-email="${esc(sug.email)}" data-key="switch" aria-description="Opens Claude sign-in so you can log in as that account" title="Opens Claude sign-in so you can log in as that account">${esc(sug.label || `Switch to ${sug.email} — ${percent(sug.five_hour_left_pct)} left`)}</button>`);
  }
  if (isStale(st)) {
    actions.push(`<button type="button" class="btn btn-accent" data-act="refresh" data-key="v-refresh">Refresh now</button>`);
    const auth = (st.source || {}).auth;
    if (auth === "expired" || auth === "missing")
      actions.push(`<button type="button" class="btn" data-act="login" data-key="v-login" aria-description="Opens Claude sign-in" title="Opens Claude sign-in">Sign in to Claude</button>`);
  }
  let muteHTML = "";
  if (fin(mute.until) && mute.until > now) {
    muteHTML = `<div class="mute-line">${icon("bell-off", { size: 14 })}<span>Alerts muted until ${esc(when(mute.until, now))}</span><button type="button" class="link-btn" data-act="unmute" data-key="unmute">Unmute</button></div>`;
  } else if (!isStale(st) && ["will_hit", "watch", "limited"].includes(v.tone)) {
    actions.push(`<button type="button" class="btn" data-act="mute" data-key="mute">${icon("bell-off")}Mute until refill</button>`);
  }
  const iconTone = v.tone === "gathering" ? "gathering" : v.tone;
  return `<section class="verdict verdict--${esc(v.tone)}" aria-labelledby="verdict-title">
    ${levelIcon(iconTone, 24, "verdict-ic")}
    <div class="verdict-body">
      <h2 class="verdict-title" id="verdict-title">${esc(v.title)}</h2>
      ${v.detail ? `<p class="verdict-detail">${esc(v.detail)}</p>` : ""}
      ${actions.length ? `<div class="verdict-actions">${actions.join("")}</div>` : ""}
      ${muteHTML}
    </div>
  </section>`;
}

function legendHTML(m, tone, stale) {
  const items = [`<span class="key"><span class="key-line"></span>Used</span>`];
  if (stale) {
    items.push(`<span class="key"><span class="key-dash rw-key-even"></span>Even pace</span>`);
    return `<div class="rw-legend" aria-hidden="true">${items.join("")}</div>`;
  }
  if (m.projection || m.flat) items.push(`<span class="key"><span class="key-dash rw-key-proj tone-line-${tone}"></span>At this rate</span>`);
  items.push(`<span class="key"><span class="key-dash rw-key-even"></span>Even pace</span>`);
  if (m.sustainable) items.push(`<span class="key"><span class="key-line rw-key-sust"></span>${esc(rate(m.sustainable.rate))} makes it</span>`);
  return `<div class="rw-legend" aria-hidden="true">${items.join("")}</div>`;
}

function runwayHTML(now, stale) {
  const L = bindingLimit(st);
  if (!L) return "";
  const tone = toneOf(L);
  const h = hist.key === L.id + ":" + L.resets_at ? hist.data : [];
  const width = chartW || Math.max(320, (root.clientWidth || 900) - 56 - 38);
  const height = width < 520 ? 180 : 200;
  const src = st.source || {};
  const at = stale && fin(src.updated_at) ? src.updated_at : now;
  model = runwayModel({ limit: L, history: h, now: Math.min(at, L.resets_at), width, height });
  const asOfText = stale && fin(src.updated_at) ? cap(asOf(src.updated_at, now)) : "";
  const svg = renderRunway(model, { stale, asOfText });
  const p = L.pace || {};

  let runVal, runSub, runTone;
  if (stale) {
    runVal = "—";
    runSub = asOfText || "Out of date";
    runTone = "stale";
  } else if (model.mode === "eta") {
    runVal = timeSpan(p.eta_100, "left", now);
    runSub = "Limit around " + esc(clock(p.eta_100));
    runTone = "will_hit";
  } else if (model.mode === "limited") {
    runVal = "Limit reached";
    runSub = "Back at " + esc(when(L.resets_at, now));
    runTone = "limited";
  } else if (model.mode === "gathering") {
    runVal = "—";
    runSub = "Gathering pace…";
    runTone = "gathering";
  } else {
    runVal = "Past refill";
    runSub = fin(model.projEnd) ? "About " + esc(percent(Math.min(model.projEnd, 100))) + " at refill" : "On pace";
    runTone = tone === "watch" ? "watch" : "ok";
  }
  const noun = cap(limitNoun(L).replace(/ limit$/, ""));
  return `<section class="card runway${stale ? " is-stale" : ""}" aria-label="${esc(noun)} runway">
    <div class="rw-top">
      <div class="rw-readouts">
        <div class="ro">
          <div class="lab">${esc(noun)} runway</div>
          <div class="ro-val tone-${runTone}">${runVal}</div>
          <div class="ro-sub">${runSub}</div>
        </div>
        <div class="ro">
          <div class="lab">Refill</div>
          <div class="ro-val">${timeSpan(L.resets_at, "left", now)}</div>
          <div class="ro-sub">at ${esc(when(L.resets_at, now))}</div>
        </div>
      </div>
      ${legendHTML(model, tone, stale)}
    </div>
    <div class="rw-wrap">${svg}<div class="rw-tip" role="presentation" hidden></div></div>
  </section>`;
}

function barLegend(L, now, stale) {
  const p = L.pace || {};
  const tone = toneOf(L);
  const cls = stale ? "stale" : tone === "gathering" ? "ok" : tone;
  const items = [`<span class="key"><span class="key-fill"></span>Used ${esc(percent(L.used_pct))}</span>`];
  if (stale) {
    items.push(`<span class="key">No forecast while the data is out of date</span>`);
    return `<div class="bar-legend lvl-${cls}">${items.join("")}</div>`;
  }
  if (tone === "limited") {
    items.push(`<span class="key">Back at ${esc(when(L.resets_at, now))}</span>`);
    return `<div class="bar-legend lvl-${cls}">${items.join("")}</div>`;
  }
  if (fin(L.elapsed_pct)) items.push(`<span class="key"><span class="key-tick"></span>Even pace ${esc(percent(L.elapsed_pct))}</span>`);
  if (tone === "gathering") items.push(`<span class="key">Gathering pace…</span>`);
  else if (tone === "will_hit" && fin(p.eta_100)) items.push(`<span class="key"><span class="key-proj"></span>At this rate: full at ${esc(clock(p.eta_100))}</span>`);
  else if (fin(p.projected_at_reset)) items.push(`<span class="key"><span class="key-proj"></span>At this rate: ~${esc(percent(Math.min(p.projected_at_reset, 100)))} by ${esc(when(L.resets_at, now))}</span>`);
  return `<div class="bar-legend lvl-${cls}">${items.join("")}</div>`;
}

function chip(tone, stale, now) {
  if (stale) {
    const at = (st.source || {}).updated_at;
    return `<span class="chip tone-stale">${levelIcon("stale", 12)}${esc(cap(asOf(at, now)))}</span>`;
  }
  return `<span class="chip tone-${tone}">${levelIcon(tone, 12)}${esc(levelWord(tone))}</span>`;
}

function limitCard(L, now, stale, scoped = []) {
  const tone = toneOf(L);
  const p = L.pace || {};
  const left = Math.max(0, 100 - L.used_pct);
  let sub;
  if (tone === "limited") sub = "used · none left";
  else if (tone === "ok" && fin(p.projected_at_reset) && !stale) sub = `used · ~${percent(Math.min(p.projected_at_reset, 100))} by refill`;
  else sub = `used · ${percent(left)} left`;
  const name = L.id === "five_hour" ? "5-hour window" : L.label || "Weekly";
  const meter = bullet({
    used: L.used_pct,
    projected: fin(p.projected_at_reset) ? p.projected_at_reset : null,
    elapsed: L.elapsed_pct,
    tone: stale ? "stale" : tone === "gathering" ? "ok" : tone,
    stale,
    label: limitSentence(L, now, { stale, asOfText: asOf((st.source || {}).updated_at, now) }),
  });
  let extra = "";
  if (scoped.length) {
    extra = `<div class="scoped">${scoped
      .map((s) => {
        const t = toneOf(s);
        const bind = s.binding || ["will_hit", "watch", "limited"].includes(t);
        const lab = s.scope ? cap(s.scope) : s.label;
        return `<span class="scoped-name">${esc(lab)}${bind ? " " + levelIcon(t, 12) : ""}</span>${bullet({
          used: s.used_pct,
          tone: stale ? "stale" : bind ? t : "neutral",
          size: "sm",
          label: limitSentence(s, now),
        })}<span class="scoped-pct">${esc(percent(s.used_pct))}</span>`;
      })
      .join("")}</div>`;
  } else extra = barLegend(L, now, stale);
  return `<section class="card limit-card" aria-labelledby="lc-${esc(L.id)}">
    <div class="card-head"><h2 id="lc-${esc(L.id)}">${esc(name)}</h2>${chip(tone, stale, now)}</div>
    <div class="lc-main">
      <div class="lc-used"><span class="t-hero">${esc(percent(L.used_pct))}</span><span class="muted"> ${esc(sub)}</span></div>
      <div class="lc-reset"><div class="t-body-strong">Refills ${esc(when(L.resets_at, now))}</div><div class="t-caption muted">${timeSpan(L.resets_at, "in", now)}</div></div>
    </div>
    ${meter}
    ${extra}
  </section>`;
}

function limitCardsHTML(now, stale) {
  const five = limitById(st, "five_hour");
  const week = limitById(st, "seven_day");
  const scoped = (st.limits || []).filter((l) => l.scope && l.scope !== "all");
  const cards = [];
  if (five) cards.push(limitCard(five, now, stale));
  if (week) cards.push(limitCard(week, now, stale, scoped));
  else for (const s of scoped) cards.push(limitCard(s, now, stale));
  for (const l of st.limits || []) if (l !== five && l !== week && !scoped.includes(l)) cards.push(limitCard(l, now, stale));
  return `<div class="grid-2 limit-cards">${cards.join("")}</div>`;
}

function noLimitsHTML() {
  const auth = (st.source || {}).auth;
  let title = "Waiting for the first reading", body = "Usage appears here as soon as Claude Code reports it. Run a prompt in Claude Code, or refresh.";
  let btn = `<button type="button" class="btn btn-accent" data-act="refresh">Refresh now</button>`;
  if (auth === "missing") {
    title = "Sign in to Claude Code on this PC";
    body = "The tracker reads your limits from your Claude Code sign-in. Sign in once and usage shows up here.";
    btn = `<button type="button" class="btn btn-accent" data-act="login" aria-description="Opens Claude sign-in">Open Claude sign-in</button>`;
  } else if (auth === "expired") {
    title = "Your Claude sign-in expired";
    body = "Sign in again so the tracker can read your limits.";
    btn = `<button type="button" class="btn btn-accent" data-act="login" aria-description="Opens Claude sign-in">Sign in again</button>`;
  }
  return `<section class="card"><div class="empty">${levelIcon("gathering", 24)}<div class="empty-title">${esc(title)}</div><p class="empty-body">${esc(body)}</p>${btn}</div></section>`;
}

function smallCardsHTML(now, stale) {
  const sessions = st.sessions || [];
  // Active session
  const a = activeSession(sessions);
  let act;
  if (a && a.context) {
    const c = a.context;
    const high = c.pct >= 90;
    act = `<section class="card" aria-labelledby="c-active">
      <h2 class="lab" id="c-active">Active session</h2>
      <div class="sess-head"><span class="t-body-strong sess-name" title="${esc([a.project, prettyModel(a.model)].filter(Boolean).join(" · "))}">${esc(a.project)}</span><span class="t-body-strong num${high && !stale ? " tone-will_hit" : ""}">${esc(percent(c.pct))}</span></div>
      ${bullet({ used: c.pct, mark: 90, tone: stale ? "stale" : high ? "will_hit" : "ink", size: "md", label: `${a.project} context ${percent(c.pct)} full; compact at 90%`, valueText: `${percent(c.pct)} full` })}
      <div class="t-caption ${high && !stale ? "tone-will_hit" : "muted"} ctx-line">${high && !stale ? levelIcon("will_hit", 12) : ""}<span>${esc(tokens(c.tokens))} of ${esc(tokens(c.window))} · ${high ? "compact now" : "compact at 90%"}</span></div>
    </section>`;
  } else {
    act = `<section class="card" aria-labelledby="c-active">
      <h2 class="lab" id="c-active">Active session</h2>
      <div class="empty"><div class="empty-title">No active session</div><p class="empty-body t-caption">Start Claude Code and its context fill shows up here.</p></div>
    </section>`;
  }

  // Waiting for you
  const w = waitingSessions(sessions);
  let wait;
  if (w.length) {
    const rows = w.slice(0, 3).map((s) => `<li class="wait-row"><span class="t-body-strong wait-name">${esc(s.project)}</span>${s.state === "needs_approval" ? `<span class="pill pill-watch">Needs approval</span>` : ""}<span class="muted num wait-time">${timeSpan(s.since, "since", now)}</span></li>`).join("");
    const more = w.length > 3 ? `<a class="t-caption" href="#/sessions">${w.length - 3} more in Sessions</a>` : "";
    const anyApproval = w.some((s) => s.state === "needs_approval");
    wait = `<section class="card" aria-labelledby="c-wait">
      <h2 class="lab" id="c-wait">Waiting for you</h2>
      <ul class="wait-list" role="list">${rows}</ul>${more}
      <div class="t-caption muted">${anyApproval ? "Finished a turn or needs your approval" : "Finished a turn and waiting for your reply"}</div>
    </section>`;
  } else {
    wait = `<section class="card" aria-labelledby="c-wait">
      <h2 class="lab" id="c-wait">Waiting for you</h2>
      <div class="empty"><div class="empty-title">Nothing waiting</div><p class="empty-body t-caption">Sessions that finish a turn or need approval show up here.</p></div>
    </section>`;
  }

  // Extra usage
  const o = st.overage;
  const month = monthName(st.generated_at || now);
  let extra;
  if (o && o.enabled) {
    const t = o.pct >= 100 ? "limited" : o.pct >= 80 ? "watch" : "neutral";
    extra = `<section class="card" aria-labelledby="c-extra">
      <h2 class="lab" id="c-extra">Extra usage · ${esc(month)}</h2>
      <div><span class="t-subtitle">${esc(currency(o.used, o.currency))}</span><span class="muted"> of ${esc(currency(o.limit, o.currency))}</span></div>
      ${bullet({ used: o.pct, tone: stale ? "stale" : t, size: "md", label: `Extra usage ${currency(o.used, o.currency)} of ${currency(o.limit, o.currency)} this month, ${percent(o.pct)} of the cap`, valueText: `${percent(o.pct)} of cap` })}
      <div class="t-caption muted">${o.pct >= 80 ? `${esc(percent(o.pct))} of your monthly cap` : "Used only after a limit is hit"}</div>
    </section>`;
  } else {
    extra = `<section class="card" aria-labelledby="c-extra">
      <h2 class="lab" id="c-extra">Extra usage</h2>
      <div class="empty"><div class="empty-title">Extra usage is off</div><p class="empty-body t-caption">With it on in your Claude account, you can keep working after a limit and pay per use.</p></div>
    </section>`;
  }
  return `<div class="grid-3">${act}${wait}${extra}</div>`;
}

/* ---------------- actions ---------------- */

async function onClick(e) {
  const b = e.target.closest("[data-act]");
  if (!b) return;
  const a = b.getAttribute("data-act");
  if (a === "refresh") await act("refresh", {}, { button: b });
  else if (a === "retry") await refetch();
  else if (a === "mute") await act("mute", { for: "refill" }, { button: b, success: "Alerts muted until the refill" });
  else if (a === "unmute") await act("mute", { for: "off" }, { button: b, success: "Alerts are on again" });
  else if (a === "switch") await act("login", { email: b.getAttribute("data-email") }, { button: b, success: "Opening Claude sign-in…" });
  else if (a === "login") await act("login", {}, { button: b, success: "Opening Claude sign-in…" });
}

/* ---------------- chart hover ---------------- */

function onHover(e) {
  const svg = e.target.closest && e.target.closest(".rw-wrap svg");
  if (!svg || !model) return hideHover();
  const r = svg.getBoundingClientRect();
  const x = e.clientX - r.left;
  if (x < model.x0 || x > model.x1) return hideHover();
  hovering = true;
  const t = model.start + ((x - model.x0) / (model.x1 - model.x0)) * (model.end - model.start);
  const v = valueAt(model, t);
  const g = svg.querySelector(".rw-hover");
  const line = g.querySelector(".rw-cross");
  const dot = g.querySelector(".rw-hdot");
  line.setAttribute("x1", x);
  line.setAttribute("x2", x);
  const pct = v.past ? v.used : v.projected;
  if (fin(pct)) {
    dot.removeAttribute("hidden");
    dot.setAttribute("cx", x);
    dot.setAttribute("cy", model.Y(Math.min(pct, 100)));
  } else dot.setAttribute("hidden", "");
  g.removeAttribute("hidden");
  const tip = svg.parentElement.querySelector(".rw-tip");
  tip.textContent = "";
  const head = document.createElement("div");
  head.className = "tip-head";
  head.textContent = (model.end - model.start > 86400 ? when(t, model.now) : clock(t)) + (v.past ? "" : " · forecast");
  tip.appendChild(head);
  const row = (val, lab, cls) => {
    const d = document.createElement("div");
    d.className = "tip-row";
    const k = document.createElement("span");
    k.className = "tip-key " + cls;
    const b = document.createElement("b");
    b.textContent = val;
    const s = document.createElement("span");
    s.textContent = lab;
    d.append(k, b, s);
    tip.appendChild(d);
  };
  if (v.past) row(percent(v.used), "used", "tk-used");
  else if (fin(v.projected)) row(percent(Math.min(v.projected, 100)), "at this rate", "tk-proj");
  row(percent(Math.max(0, Math.min(100, v.even))), "even pace", "tk-even");
  tip.hidden = false;
  const tw = tip.offsetWidth;
  let left = x + 12;
  if (left + tw > r.width - 4) left = x - 12 - tw;
  tip.style.left = Math.max(0, left) + "px";
  tip.style.top = model.y0 + "px";
}
function hideHover() {
  if (!hovering || !root) return;
  hovering = false;
  if (pending) {
    pending = false;
    render();
    return;
  }
  const g = root.querySelector(".rw-hover");
  if (g) g.setAttribute("hidden", "");
  const tip = root.querySelector(".rw-tip");
  if (tip) tip.hidden = true;
}
