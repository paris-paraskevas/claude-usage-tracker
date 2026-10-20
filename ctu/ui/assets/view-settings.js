// Settings (spec §7, behind the gear): Alerts, Overlay, Phone, Team, Data
// source, About. GET /api/v2/settings once; every change PATCHes just the
// changed group and the server's validated response becomes the new truth.
// Shape: tests/ui/fixtures/settings.json.

import { patch, esc, timeSpan, announce } from "./dom.js";
import { icon, levelIcon } from "./icons.js";
import { act, getSettings, patchSettings, toast } from "./actions.js";
import { refetch } from "./state.js";
import { when } from "./format.js";

export const title = "Settings";

const ALERTS = [
  ["will_hit", "Before you hit a limit", "When your pace will use up a limit before it refills."],
  ["limited", "When a limit is reached", "Includes when extra usage takes over."],
  ["refilled", "When a limit refills", "After a limit you hit resets."],
  ["context_high", "When a session's context is almost full", "At 90%, so you can compact first."],
  ["overage_high", "When extra usage nears its cap", "At 80% of your monthly cap."],
  ["session_waiting", "When a session is waiting for you", "After it finishes a turn and you've been away for a minute."],
  ["needs_approval", "When a session needs approval", "When Claude Code asks permission to continue."],
  ["incident", "During a Claude incident", "When Claude's status page reports a problem."],
];
const FIELDS = {
  "5h": ["5-hour limit", "Used % and when it runs out or refills"],
  "7d": ["Weekly limit", "Used %"],
  ctx: ["Active session context", "How full the current session is"],
  waiting: ["Sessions waiting", "Count of sessions waiting for you"],
  status: ["Claude status", "Shown only during an incident"],
};
const COMPONENTS = ["claude.ai", "Claude Code", "Claude API (api.anthropic.com)", "Claude Console (platform.claude.com)"];
const REFRESH = [
  [30, "Every 30 seconds"],
  [60, "Every minute"],
  [120, "Every 2 minutes"],
  [300, "Every 5 minutes"],
  [600, "Every 10 minutes"],
];
const GROUPS = [
  ["alerts", "Alerts"],
  ["overlay", "Overlay & tray"],
  ["phone", "Phone"],
  ["team", "Team"],
  ["data", "Data source"],
  ["about", "About"],
];

let root = null;
let st = null;
let nowFn = () => Date.now() / 1000;
let S = null; // settings
let load = { status: "idle", error: "" };
let saved = {}; // group -> timestamp of last save
let pair = null; // { code, url, qr_svg, expires_at }
let updateMsg = "";
let draft = {}; // text drafts
let sub = null;
let dragging = false;

export function mount(el, ctx) {
  root = el;
  nowFn = ctx.now;
  sub = ctx.sub;
  root.addEventListener("change", onChange);
  root.addEventListener("input", onInput);
  root.addEventListener("click", onClick);
  root.addEventListener("pointerdown", startDrag);
  window.addEventListener("pointerup", endDrag);
  root.addEventListener("focusout", flushDeferred);
  fetchSettings();
}
export function unmount() {
  root.removeEventListener("change", onChange);
  root.removeEventListener("input", onInput);
  root.removeEventListener("click", onClick);
  root.removeEventListener("pointerdown", startDrag);
  root.removeEventListener("focusout", flushDeferred);
  window.removeEventListener("pointerup", endDrag);
  root = null;
  pair = null;
  updateMsg = "";
}
let deferred = false;
const editing = () => {
  const a = document.activeElement;
  return !!(root && a && root.contains(a) && (a.tagName === "SELECT" || (a.tagName === "INPUT" && !["checkbox", "radio", "range", "button"].includes(a.type))));
};
export function update(s) {
  st = s;
  if (dragging || editing()) deferred = true; // never swap a field out from under the user
  else render();
}
function flushDeferred() {
  if (!deferred) return;
  setTimeout(() => {
    if (!editing() && !dragging) {
      deferred = false;
      render();
    }
  }, 0);
}
export function route(s) {
  sub = s;
  scrollToSub();
}
function startDrag(e) {
  if (e.target.type === "range") dragging = true;
}
function endDrag() {
  dragging = false;
  flushDeferred();
}

async function fetchSettings() {
  load = { status: "loading", error: "" };
  render();
  try {
    S = await getSettings();
    load = { status: "ok", error: "" };
  } catch (e) {
    load = { status: "error", error: e.message };
  }
  render();
  scrollToSub();
}
function scrollToSub() {
  if (!root || !sub) return;
  const el = root.querySelector("#set-" + CSS.escape(sub));
  if (el) {
    el.scrollIntoView({ block: "start" });
    const h = el.querySelector("h2");
    if (h) {
      h.setAttribute("tabindex", "-1");
      h.focus({ preventScroll: true });
    }
  }
}

/* ---------------- render helpers ---------------- */

const sw = (id, key, on, label, disabled = false) =>
  `<label class="switch" for="${id}"><span class="switch-text" aria-hidden="true">${on ? "On" : "Off"}</span><input type="checkbox" role="switch" id="${id}" data-key="${id}" data-set="${key}"${on ? " checked" : ""}${disabled ? " disabled" : ""} aria-label="${esc(label)}"></label>`;

function row(title, desc, control, { id = "", ic = "" } = {}) {
  return `<div class="set-row"${id ? ` id="${id}"` : ""}>${ic ? `<span class="set-ic">${icon(ic)}</span>` : ""}<div class="set-text"><div class="set-title">${title}</div>${desc ? `<div class="set-desc">${desc}</div>` : ""}</div><div class="set-ctl">${control}</div></div>`;
}
function group(id, name, rows, extra = "") {
  const s = saved[id] && Date.now() - saved[id] < 2500 ? `<span class="saved-note">${icon("check", { size: 12 })}Saved</span>` : "";
  return `<section class="set-group" id="set-${id}" aria-labelledby="set-${id}-h"><div class="set-group-head"><h2 id="set-${id}-h">${name}</h2><span class="saved-slot" aria-live="polite">${s}</span></div>${rows}${extra}</section>`;
}

function render() {
  if (!root) return;
  const head = `<header class="page-head"><h1 class="t-title">Settings</h1></header>`;
  if (load.status === "loading" || load.status === "idle") {
    patch(root, head + `<div aria-busy="true"><span class="sr-only">Loading settings…</span><div class="skel" style="height:28px;width:160px;margin-bottom:8px"></div><div class="skel" style="height:320px"></div></div>`);
    return;
  }
  if (load.status === "error") {
    patch(root, head + `<section class="card"><div class="empty">${levelIcon("error", 24)}<div class="empty-title">Couldn't load settings</div><p class="empty-body">${esc(load.error)}</p><button type="button" class="btn btn-accent" data-act="reload">Try again</button></div></section>`);
    return;
  }
  const toc = `<nav class="set-toc" aria-label="Settings sections">${GROUPS.map(([k, l]) => `<a href="#/settings/${k}" class="set-toc-link">${l}</a>`).join("")}</nav>`;
  patch(root, head + toc + [alertsHTML(), overlayHTML(), phoneHTML(), teamHTML(), dataHTML(), aboutHTML()].join(""));
}

function alertsHTML() {
  const a = S.alerts || {};
  const q = S.quiet_hours || {};
  const comps = Array.from(new Set([...(S.status_components_available || COMPONENTS), ...(S.status_components || [])]));
  const chosen = new Set(S.status_components || []);
  const rows = `<div class="set-card">${ALERTS.map(([k, t, d]) => row(esc(t), esc(d), sw("al-" + k, "alerts." + k, !!a[k], t))).join("")}</div>
  <div class="set-card">
    ${row("Quiet hours", "No alerts between these times. They still show in the dashboard.", sw("qh-on", "quiet_hours.enabled", !!q.enabled, "Quiet hours"), { ic: "bell-off" })}
    <div class="set-row set-sub${q.enabled ? "" : " is-disabled"}"><div class="set-text"><div class="set-title">From</div></div><div class="set-ctl times">
      <label class="sr-only" for="qh-start">Quiet hours start</label><input class="input input-time" type="time" step="900" id="qh-start" data-key="qh-start" data-set="quiet_hours.start" value="${esc(q.start || "22:00")}"${q.enabled ? "" : " disabled"}>
      <span class="muted">to</span>
      <label class="sr-only" for="qh-end">Quiet hours end</label><input class="input input-time" type="time" step="900" id="qh-end" data-key="qh-end" data-set="quiet_hours.end" value="${esc(q.end || "08:00")}"${q.enabled ? "" : " disabled"}>
    </div></div>
  </div>
  <div class="set-card">
    <div class="set-row set-row-col"><div class="set-text"><div class="set-title" id="comp-h">Claude status: alert about</div><div class="set-desc">Incidents that affect only other parts of Claude stay quiet.</div></div>
    <div class="checks" role="group" aria-labelledby="comp-h">${comps.map((c, i) => `<label class="check"><input type="checkbox" data-key="comp-${i}" data-comp="${esc(c)}"${chosen.has(c) ? " checked" : ""}>${esc(c)}</label>`).join("")}</div></div>
  </div>`;
  return group("alerts", "Alerts", rows);
}

function overlayHTML() {
  const o = S.overlay || {};
  const fields = o.fields || [];
  const shown = !!(st && st.app && st.app.overlay && st.app.overlay.shown);
  const order = [...fields.filter((f) => FIELDS[f]), ...Object.keys(FIELDS).filter((f) => !fields.includes(f))];
  const fl = order
    .map((f) => {
      const on = fields.includes(f);
      const i = fields.indexOf(f);
      return `<li class="field-item${on ? "" : " is-off"}">
        <label class="check"><input type="checkbox" data-key="of-${f}" data-field="${f}"${on ? " checked" : ""}><span><span class="set-title">${esc(FIELDS[f][0])}</span><span class="set-desc">${esc(FIELDS[f][1])}</span></span></label>
        <span class="field-move">
          <button type="button" class="btn btn-subtle btn-icon btn-sm" data-move="${f}" data-dir="-1" data-key="up-${f}" aria-label="Move ${esc(FIELDS[f][0])} up (further left in the strip)"${on && i > 0 ? "" : " disabled"}>${icon("arrow-up", { size: 14 })}</button>
          <button type="button" class="btn btn-subtle btn-icon btn-sm" data-move="${f}" data-dir="1" data-key="dn-${f}" aria-label="Move ${esc(FIELDS[f][0])} down (further right in the strip)"${on && i < fields.length - 1 ? "" : " disabled"}>${icon("arrow-down", { size: 14 })}</button>
        </span>
      </li>`;
    })
    .join("");
  const op = Math.max(30, Math.min(100, o.opacity ?? 90));
  const rows = `<div class="set-card">
    ${row("Show overlay", "A one-line strip, always on top, that never takes focus. Hover or click it for details.", sw("ov-shown", "@overlay.show", shown, "Show overlay"), { ic: "overlay" })}
    <div class="set-row set-row-col"><div class="set-text"><div class="set-title" id="of-h">Fields, left to right</div><div class="set-desc">Hidden fields still show in the expanded panel.</div></div><ol class="field-list" aria-labelledby="of-h">${fl}</ol></div>
    ${row("Let clicks pass through", "Clicks reach the window underneath. Turn it off from the tray icon menu.", sw("ov-ct", "overlay.click_through", !!o.click_through, "Let clicks pass through"))}
    ${row("Opacity at rest", "Full opacity while you hover.", `<span class="range-row"><input type="range" class="range" min="30" max="100" step="5" id="ov-op" data-key="ov-op" data-set="overlay.opacity" value="${op}" aria-label="Opacity at rest" aria-valuetext="${op}%"><output for="ov-op" class="num range-out" id="ov-op-out">${op}%</output></span>`)}
    ${row("Hide during full-screen apps and presentations", "", sw("ov-fs", "overlay.auto_hide_fullscreen", !!o.auto_hide_fullscreen, "Hide during full-screen apps and presentations"))}
    ${row("Tray icon", "Bars: 5-hour on the left, weekly on the right. Number: the % of the limit closest to running out.", `<select class="input" id="ov-tray" data-key="ov-tray" data-set="overlay.tray_icon" aria-label="Tray icon">${[["bars", "Two bars"], ["number", "Number"]].map(([v, l]) => `<option value="${v}"${(o.tray_icon || "bars") === v ? " selected" : ""}>${l}</option>`).join("")}</select>`)}
  </div>`;
  return group("overlay", "Overlay & tray", rows);
}

function phoneHTML() {
  const r = S.remote || {};
  const rm = (st && st.app && st.app.remote) || {};
  const now = nowFn();
  const status = rm.paired
    ? `<span class="chip tone-ok">${levelIcon("ok", 12)}Paired</span><span class="muted t-caption">${rm.last_sync ? "Last sync " + timeSpan(rm.last_sync, "ago", now) : ""}</span>`
    : `<span class="chip tone-stale">${levelIcon("gathering", 12)}Not paired</span>`;
  const pairBox = pair
    ? `<div class="pair-box">
        ${pair.qr_svg ? `<img class="pair-qr" alt="Pairing QR code" width="160" height="160" src="data:image/svg+xml;charset=utf-8,${encodeURIComponent(pair.qr_svg)}">` : ""}
        <div class="pair-text"><div class="set-title">Scan with the Claude Usage app</div>
        <div class="set-desc">Open the app on your phone, tap Pair, and scan this code.${pair.code ? " Or type the code:" : ""}</div>
        ${pair.code ? `<div class="pair-code num">${esc(pair.code)}</div>` : ""}
        ${pair.expires_at ? `<div class="set-desc">Expires ${esc(when(pair.expires_at, now))}.</div>` : ""}
        <button type="button" class="btn btn-sm" data-act="pair-done" data-key="pair-done">Done</button></div>
      </div>`
    : "";
  const rows = `<div class="set-card">
    ${row("Phone", status, rm.paired
      ? `<span class="btn-row"><button type="button" class="btn" data-act="remote_pair" data-key="rp">Pair another phone</button><button type="button" class="btn" data-act="remote_rotate" data-key="rr">Rotate key</button><button type="button" class="btn" data-act="remote_unpair" data-key="ru">Unpair</button></span>`
      : `<button type="button" class="btn btn-accent" data-act="remote_pair" data-key="rp">${icon("phone")}Pair phone</button>`, { ic: "phone" })}
    ${pairBox}
    ${row("Mirror to your phone", "Sends your limits, verdict and waiting sessions to the phone app and widget.", sw("rm-on", "remote.enabled", !!r.enabled, "Mirror to your phone"))}
    ${row("Show conversation text on the phone", "For sessions waiting on you, sends their last messages so you can read them away from the desk.", sw("rm-tx", "remote.transcript", !!r.transcript, "Show conversation text on the phone", !r.enabled))}
    <div class="set-row"><div class="set-text"><label class="set-title" for="rm-url">Relay address</label><div class="set-desc">Change only if you run your own relay.</div></div><div class="set-ctl"><input class="input input-url" type="url" id="rm-url" data-key="rm-url" data-set="remote.relay_url" spellcheck="false" value="${esc(draft["remote.relay_url"] ?? r.relay_url ?? "")}"></div></div>
  </div>`;
  return group("phone", "Phone", rows);
}

function teamHTML() {
  const tzv = (S.team && S.team.tz) || "";
  let zones = [];
  try {
    zones = Intl.supportedValuesOf("timeZone");
  } catch {
    zones = [tzv || "UTC"];
  }
  if (tzv && !zones.includes(tzv)) zones.unshift(tzv);
  const sel = `<select class="input" id="tm-tz" data-key="tm-tz" data-set="team.tz" aria-label="Team time zone">${zones.map((z) => `<option value="${esc(z)}"${z === tzv ? " selected" : ""}>${esc(z.replace(/_/g, " "))}</option>`).join("")}</select>`;
  return group("team", "Team", `<div class="set-card">${row("Team time zone", "Ledger months start and end on this clock, the same for everyone in the pool.", sel, { ic: "team" })}</div>`);
}

function dataHTML() {
  const d = S.data || {};
  const src = (st && st.source) || {};
  const now = nowFn();
  const ref = d.refresh_s || 60;
  const opts = REFRESH.some((r) => r[0] === ref) ? REFRESH : [[ref, `Every ${ref} seconds`], ...REFRESH];
  const reading = src.updated_at ? `Last reading ${timeSpan(src.updated_at, "ago", now)} from ${src.kind === "statusline" ? "the statusline" : "the usage API"}.` : "No reading yet.";
  const rows = `<div class="set-card">
    ${row("Live usage reader (statusline)", `Claude Code's statusline reports your limits after every turn, with no extra requests. Installing it also adds the session hook, which tells the tracker when a session is waiting for you or needs approval.`,
      d.statusline_installed
        ? `<span class="chip tone-ok">${levelIcon("ok", 12)}Installed</span>`
        : `<button type="button" class="btn btn-accent" data-act="install_statusline" data-key="isl">Install</button>`, { ic: "plug" })}
    ${row("Check usage", "How often to ask Claude for your limits when the statusline is quiet.", `<select class="input" id="dt-ref" data-key="dt-ref" data-set="data.refresh_s" aria-label="Check usage">${opts.map(([v, l]) => `<option value="${v}"${v === ref ? " selected" : ""}>${l}</option>`).join("")}</select>`)}
    <div class="set-row"><div class="set-text"><div class="set-title">Current source</div><div class="set-desc">${reading}${src.error ? " " + esc(src.error) : ""}</div></div><div class="set-ctl"><button type="button" class="btn" data-act="refresh" data-key="d-ref">${icon("refresh")}Refresh now</button></div></div>
  </div>`;
  return group("data", "Data source", rows);
}

function aboutHTML() {
  const ab = S.about || {};
  const app = (st && st.app) || {};
  const ver = ab.version || app.version || "—";
  const upd = app.update && app.update.available ? app.update : null;
  const updCtl = upd
    ? `<button type="button" class="btn btn-accent" data-act="update" data-key="upd">Update to ${esc(upd.version)}</button>`
    : `<button type="button" class="btn" data-act="check_update" data-key="chk">Check for updates</button>`;
  const rows = `<div class="set-card">
    ${row(`Claude Usage Tracker ${esc(ver)}`, upd ? `Version ${esc(upd.version)} is ready to install.` : updateMsg ? `<span role="status">${esc(updateMsg)}</span>` : "Updates come from GitHub releases.", updCtl, { ic: "info" })}
    ${row("Check for updates automatically", "Once a day. Nothing installs without your click.", sw("ab-uc", "about.update_check", !!ab.update_check, "Check for updates automatically"))}
    ${row("Logs", "Useful when reporting a problem.", `<button type="button" class="btn" data-act="open_logs" data-key="logs">${icon("folder")}Open log folder</button>`)}
  </div>`;
  return group("about", "About", rows);
}

/* ---------------- saving ---------------- */

function setPath(obj, path, val) {
  const [g, k] = path.split(".");
  return { [g]: { ...(obj[g] || {}), [k]: val } };
}

async function save(path, value, groupId) {
  const [g, k] = path.split(".");
  const before = S;
  S = { ...S, ...setPath(S, path, value) };
  render();
  try {
    const res = await patchSettings({ [g]: { [k]: value } });
    if (res && typeof res === "object" && res[g]) S = { ...S, ...res };
    saved[groupId] = Date.now();
    render();
    setTimeout(render, 2600);
  } catch (e) {
    S = before;
    render();
    toast({ title: "Couldn't save that setting", detail: e.message, kind: "error" });
  }
}

const groupOf = (path) => ({ alerts: "alerts", quiet_hours: "alerts", status_components: "alerts", overlay: "overlay", remote: "phone", team: "team", data: "data", about: "about" }[path.split(".")[0]]);

function onInput(e) {
  const t = e.target;
  if (t.id === "ov-op") {
    const out = root.querySelector("#ov-op-out");
    if (out) out.textContent = t.value + "%";
    t.setAttribute("aria-valuetext", t.value + "%");
  }
  if (t.getAttribute("data-set") === "remote.relay_url") draft["remote.relay_url"] = t.value;
}

async function onChange(e) {
  const t = e.target;
  const set = t.getAttribute("data-set");
  if (t.hasAttribute("data-comp")) {
    const chosen = Array.from(root.querySelectorAll("[data-comp]")).filter((x) => x.checked).map((x) => x.getAttribute("data-comp"));
    const before = S;
    S = { ...S, status_components: chosen };
    try {
      const res = await patchSettings({ status_components: chosen });
      if (res && res.status_components) S = { ...S, ...res };
      saved.alerts = Date.now();
    } catch (err) {
      S = before;
      toast({ title: "Couldn't save that setting", detail: err.message, kind: "error" });
    }
    render();
    return;
  }
  if (t.hasAttribute("data-field")) {
    const f = t.getAttribute("data-field");
    const cur = (S.overlay && S.overlay.fields) || [];
    const next = t.checked ? [...cur.filter((x) => x !== f), f] : cur.filter((x) => x !== f);
    return save("overlay.fields", next, "overlay");
  }
  if (!set) return;
  if (set === "@overlay.show") {
    await act("overlay", { show: t.checked }, { button: t });
    return refetch();
  }
  let v;
  if (t.type === "checkbox") v = t.checked;
  else if (t.type === "range" || set === "data.refresh_s") v = Number(t.value);
  else v = t.value;
  if (set === "remote.relay_url") {
    v = String(v).trim();
    if (v && !/^https:\/\/[^\s]+$/i.test(v)) {
      t.setAttribute("aria-invalid", "true");
      toast({ title: "Use an https:// address", detail: "The relay address must start with https://.", kind: "error" });
      return;
    }
    t.removeAttribute("aria-invalid");
    delete draft["remote.relay_url"];
  }
  if (t.type === "range") dragging = false;
  return save(set, v, groupOf(set));
}

async function onClick(e) {
  const mv = e.target.closest("[data-move]");
  if (mv) {
    const f = mv.getAttribute("data-move");
    const dir = +mv.getAttribute("data-dir");
    const cur = [...((S.overlay && S.overlay.fields) || [])];
    const i = cur.indexOf(f);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= cur.length) return;
    [cur[i], cur[j]] = [cur[j], cur[i]];
    await save("overlay.fields", cur, "overlay");
    announce(`${FIELDS[f][0]} moved to position ${j + 1}`);
    const again = root && root.querySelector(`[data-key="${dir < 0 ? "up" : "dn"}-${f}"]`);
    if (again && !again.disabled) again.focus();
    else if (root) root.querySelector(`[data-key="of-${f}"]`)?.focus();
    return;
  }
  const b = e.target.closest("[data-act]");
  if (!b) return;
  const a = b.getAttribute("data-act");
  if (a === "reload") return fetchSettings();
  if (a === "pair-done") {
    pair = null;
    return render();
  }
  if (a === "remote_pair") {
    const res = await act("remote_pair", {}, { button: b });
    if (res) {
      pair = res.pair || res;
      render();
    }
    return refetch();
  }
  if (a === "remote_unpair") {
    await act("remote_unpair", {}, { button: b, success: "Phone unpaired" });
    pair = null;
    return refetch();
  }
  if (a === "remote_rotate") {
    await act("remote_rotate", {}, { button: b, success: "New key set. Pair your phone again." });
    return refetch();
  }
  if (a === "check_update") {
    const res = await act("check_update", {}, { button: b });
    if (res) {
      const u = res.update || (res.available ? res : null);
      updateMsg = u && u.available ? `Version ${u.version} is available.` : res.message || "You're on the latest version.";
      render();
      refetch();
    }
    return;
  }
  if (a === "install_statusline") {
    const res = await act("install_statusline", {}, { button: b, success: "Statusline installed. It starts with your next Claude Code turn." });
    if (res) fetchSettings();
    return;
  }
  if (a === "update") return act("update", {}, { button: b, success: "Downloading the update…" });
  if (a === "refresh") return act("refresh", {}, { button: b });
  if (a === "open_logs") return act("open_logs", {}, { button: b });
}
