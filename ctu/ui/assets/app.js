// Dashboard shell: hash router (#/now, #/sessions, #/team, #/history,
// #/settings), navigation rail, connection banner, shared ticking clock.

import { start, subscribe, onTick, now, getState, getMeta, refetch, tickNow } from "./state.js";
import { applyAppearance } from "./theme.js";
import { refreshTimes, esc } from "./dom.js";
import { icon } from "./icons.js";
import { asOf, count } from "./format.js";
import { waitingSessions } from "./model.js";
import * as nowView from "./view-now.js";
import * as sessionsView from "./view-sessions.js";
import * as teamView from "./view-team.js";
import * as historyView from "./view-history.js";
import * as settingsView from "./view-settings.js";

const ROUTES = { now: nowView, sessions: sessionsView, team: teamView, history: historyView, settings: settingsView };
const viewEl = document.getElementById("view");
const mainEl = document.getElementById("main");
const connEl = document.getElementById("conn-banner");
let current = null;
let currentName = null;
let firstRoute = true;

// nav icons
for (const el of document.querySelectorAll("[data-icon]")) el.innerHTML = icon(el.getAttribute("data-icon"));

function parseHash() {
  const h = location.hash.replace(/^#\/?/, "");
  const [name, ...rest] = h.split("/");
  return { name: ROUTES[name] ? name : "now", sub: rest.join("/") || null };
}

function route() {
  const { name, sub } = parseHash();
  if (name !== currentName) {
    if (current && current.unmount) current.unmount();
    viewEl.innerHTML = "";
    viewEl.__html = null;
    viewEl.className = "view view-" + name;
    current = ROUTES[name];
    currentName = name;
    for (const a of document.querySelectorAll(".nav-item")) {
      if (a.getAttribute("data-route") === name) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    }
    document.title = current.title + " · Claude Usage";
    current.mount(viewEl, { sub, getState, getMeta, now });
    if (getState() || getMeta().conn !== "connecting") current.update(getState(), getMeta());
    mainEl.scrollTop = 0;
    if (!firstRoute) {
      const h1 = viewEl.querySelector("h1");
      if (h1) {
        h1.setAttribute("tabindex", "-1");
        h1.focus({ preventScroll: true });
      }
    }
  } else if (current.route) {
    current.route(sub);
  }
  firstRoute = false;
  refreshTimes(viewEl, now());
  tickNow();
}
window.addEventListener("hashchange", route);

function renderChrome(state, meta) {
  // Sessions badge: waiting + needs approval
  const badge = document.getElementById("nav-badge-sessions");
  const n = state ? waitingSessions(state.sessions).length : 0;
  badge.hidden = !n;
  badge.textContent = n ? String(n) : "";
  const sessLink = document.querySelector('.nav-item[data-route="sessions"]');
  sessLink.setAttribute("aria-label", n ? `Sessions, ${count(n, "waiting for you", "waiting for you")}` : "Sessions");

  // Account
  const acc = document.getElementById("account");
  const a = state && state.account;
  if (a && a.email) {
    const initial = (a.name || a.email).trim().charAt(0).toUpperCase();
    const plan = a.plan ? a.plan.charAt(0).toUpperCase() + a.plan.slice(1) : "";
    const html = `<span class="avatar" aria-hidden="true">${esc(initial)}</span><span class="account-text"><span class="account-email">${esc(a.email)}</span><span class="account-plan">${esc([plan && plan + " plan", a.org].filter(Boolean).join(" · "))}</span></span>`;
    if (acc.__html !== html) {
      acc.innerHTML = html;
      acc.__html = html;
    }
    acc.title = `Signed in to Claude as ${a.email}`;
    acc.hidden = false;
  } else acc.hidden = true;

  // Connection banner (only once we had data; views show the first-load error)
  if (meta.conn === "offline" && meta.everLoaded && state) {
    const at = state.source && state.source.updated_at;
    connEl.innerHTML = `<span class="conn-text">${icon("info")}<span>Lost connection to the tracker. Showing data ${esc(at ? asOf(at, now()) : "from earlier")}.</span></span><button type="button" class="btn btn-sm" data-conn-retry>Retry</button>`;
    connEl.hidden = false;
  } else if (meta.conn === "polling") {
    connEl.hidden = true;
  } else connEl.hidden = true;
}
connEl.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-conn-retry]");
  if (!b) return;
  b.disabled = true;
  await refetch();
  b.disabled = false;
});

subscribe((state, meta) => {
  applyAppearance(state);
  renderChrome(state, meta);
  if (current) current.update(state, meta);
  refreshTimes(viewEl, now());
  tickNow();
});

onTick((t) => {
  if (current && current.tick) current.tick(t);
  return refreshTimes(viewEl, t);
});

route();
start();
