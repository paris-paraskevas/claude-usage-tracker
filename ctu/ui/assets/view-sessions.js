// Sessions view (spec §7): waiting / needs-approval first, then active.

import { patch, esc, timeSpan } from "./dom.js";
import { levelIcon, icon } from "./icons.js";
import { bullet } from "./meter.js";
import { percent, tokens, prettyModel, count, SESSION_WORD, asOf } from "./format.js";
import { sortSessions, waitingSessions, isStale } from "./model.js";
import { refetch } from "./state.js";

export const title = "Sessions";
let root = null;
let st = null;
let meta = null;
let nowFn = () => Date.now() / 1000;

export function mount(el, ctx) {
  root = el;
  nowFn = ctx.now;
  root.addEventListener("click", onClick);
  render();
}
export function unmount() {
  root.removeEventListener("click", onClick);
  root = null;
}
export function update(s, m) {
  st = s;
  meta = m;
  render();
}

function onClick(e) {
  if (e.target.closest("[data-act=retry]")) refetch();
}

const STATE_PILL = {
  waiting: "pill-accent",
  needs_approval: "pill-watch",
  working: "pill-neutral",
  idle: "pill-outline",
};

function render() {
  if (!root) return;
  const now = nowFn();
  const head = (sub) => `<header class="page-head"><div><h1 class="t-title">Sessions</h1>${sub ? `<p class="page-sub">${sub}</p>` : ""}</div></header>`;
  if (!st) {
    if (meta && meta.conn === "offline") {
      patch(root, head("") + `<section class="card"><div class="empty">${levelIcon("stale", 24)}<div class="empty-title">Can't reach the tracker</div><p class="empty-body">The background app isn't answering, so sessions can't be listed.</p><button type="button" class="btn btn-accent" data-act="retry">Try again</button></div></section>`);
    } else {
      patch(root, head("") + `<div aria-busy="true"><span class="sr-only">Loading sessions…</span><div class="skel" style="height:44px;margin-bottom:8px"></div><div class="skel" style="height:240px"></div></div>`);
    }
    return;
  }
  const list = sortSessions(st.sessions);
  const waiting = waitingSessions(st.sessions).length;
  const stale = isStale(st);
  if (!list.length) {
    patch(root, head("") + `<section class="card"><div class="empty">
      <span class="empty-ic">${icon("sessions", { size: 24 })}</span>
      <div class="empty-title">No Claude Code sessions in the last 5 hours</div>
      <p class="empty-body">Sessions show up here while Claude Code runs, with their context fill and token use. To see when a session finishes a turn or needs your approval, the session hook must be installed: it comes with the live usage reader in Settings › Data source.</p>
      <a class="btn" href="#/settings/data">Open Data source settings</a>
    </div></section>`);
    return;
  }
  const sub = [count(list.length, "session", "sessions"), "in the last 5 hours"].join(" ") + (waiting ? ` · ${waiting} waiting for you` : "") + (stale ? ` · ${asOf(st.source.updated_at, now)}` : "");
  const rows = list
    .map((s) => {
      const c = s.context || {};
      const high = c.pct >= 90;
      const stateTxt = SESSION_WORD[s.state] || s.state;
      const pill = `<span class="pill ${STATE_PILL[s.state] || "pill-outline"}">${s.state === "needs_approval" ? levelIcon("watch", 12) : ""}${esc(stateTxt)}</span>`;
      const ctx = typeof c.pct === "number"
        ? `<div class="ctx-cell">${bullet({ used: c.pct, mark: 90, tone: stale ? "stale" : high ? "will_hit" : "ink", size: "sm", label: `${s.project} context ${percent(c.pct)} full`, valueText: `${percent(c.pct)} full` })}<span class="num ${high ? "tone-will_hit" : ""}">${esc(percent(c.pct))}</span></div>`
        : `<span class="muted">—</span>`;
      const last = s.state === "waiting" || s.state === "needs_approval"
        ? `<span class="t-body-strong">${timeSpan(s.since, "since", now)}</span>`
        : timeSpan(s.since, "ago", now);
      return `<tr class="${s.active ? "" : "is-inactive"}">
        <td class="c-proj"><div class="cell-main">${esc(s.project || "Unknown project")}</div><div class="cell-sub">${esc([prettyModel(s.model), s.cwd].filter(Boolean).join(" · "))}</div></td>
        <td class="c-state">${pill}</td>
        <td class="c-ctx">${ctx}</td>
        <td class="c-tok r num">${esc(tokens(s.tokens_5h))}</td>
        <td class="c-last r num">${last}</td>
      </tr>`;
    })
    .join("");
  patch(root, head(esc(sub)) + `<section class="card table-card${stale ? " is-stale" : ""}">
    <div class="tbl-wrap"><table class="tbl sessions-tbl">
      <caption class="sr-only">Claude Code sessions, waiting first</caption>
      <thead><tr><th scope="col">Project</th><th scope="col">State</th><th scope="col">Context</th><th scope="col" class="r">Tokens (5 h)</th><th scope="col" class="r">Last activity</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
  </section>
  <p class="t-caption muted foot-note">Waiting time counts from the end of the session's last turn. Context over 90% is a good moment to compact.</p>`);
}
