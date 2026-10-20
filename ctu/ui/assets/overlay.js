// Overlay (spec §8): one-line strip at rest, expands in place to a 340 px
// panel on click (hover only highlights it); pin keeps it open. After every size change the
// rendered size is POSTed to /api/v2/actions/overlay_resize {w, h, expanded}
// (debounced) so the host can resize its frameless window. Drag via
// pywebview's `pywebview-drag-region` class (strip, and the panel header).

import { start, subscribe, subscribeSettings, onTick, now } from "./state.js";
import { applyAppearance } from "./theme.js";
import { esc, refreshTimes, timeSpan, announce, debounce } from "./dom.js";
import { icon, levelIcon } from "./icons.js";
import { bullet, limitSentence } from "./meter.js";
import { act, post, getSettings } from "./actions.js";
import { percentDigits, percent, clock, when, asOf } from "./format.js";
import {
  bindingLimit, limitById, toneOf, stripNote, isStale, staleReason, waitingSessions, activeSession, serviceInfo, verdict,
} from "./model.js";

const ov = document.getElementById("ov");
const strip = document.getElementById("strip");
const panel = document.getElementById("panel");

let st = null;
let meta = { conn: "connecting" };
let settings = { overlay: { fields: ["5h", "7d", "ctx", "waiting", "status"], opacity: 90, click_through: false } };
let expanded = false;
let pinned = false;
let pinPending = null; // optimistic pin until the state confirms
let closeTimer = null;
let openedBy = null;
let lastTitle = "";

/* ---------------- strip ---------------- */

const lowerFirst = (s) => (s ? s.charAt(0).toLowerCase() + s.slice(1) : s);

function sep() {
  return `<span class="sep" aria-hidden="true"></span>`;
}
function pctSlot(v) {
  return `<b class="v"><span class="d">${esc(percentDigits(v))}</span>%</b>`;
}

function stripHTML(t) {
  if (!st) {
    const txt = meta.conn === "offline" ? "Claude usage · tracker not responding" : "Claude usage · connecting…";
    return { cls: meta.conn === "offline" ? "strip--stale" : "strip--loading", html: `${meta.conn === "offline" ? levelIcon("stale", 12) : ""}<span>${txt}</span>`, label: txt };
  }
  const fields = (settings.overlay && settings.overlay.fields) || ["5h", "7d", "ctx", "waiting", "status"];
  const five = limitById(st, "five_hour");
  const week = limitById(st, "seven_day");
  const bind = bindingLimit(st);
  const stale = isStale(st);
  const waiting = waitingSessions(st.sessions);
  const act = activeSession(st.sessions);
  const svc = serviceInfo(st.service);
  const parts = [];
  const label = [];

  // Limited: the countdown replaces the percentage
  if (!stale && bind && toneOf(bind) === "limited") {
    const back = (st.decision && st.decision.resume_at) || bind.resets_at;
    const key = bind.id === "five_hour" ? "5h" : bind.id === "seven_day" ? "Weekly" : bind.label;
    let html = `${levelIcon("limited", 12, "on-solid")}<span><b>${esc(key)} limit reached</b> · back at ${esc(when(back, t))} (${timeSpan(back, "in", t)})</span>`;
    if (fields.includes("waiting") && waiting.length) html += `<span class="pill pill-onsolid">${waiting.length} waiting</span>`;
    label.push(`${key} limit reached, back at ${when(back, t)}`);
    if (waiting.length) label.push(`${waiting.length} waiting`);
    return { cls: "strip--limited", html, label: label.join("; ") };
  }

  const tone = stale ? "stale" : bind ? toneOf(bind) : "ok";
  parts.push(levelIcon(tone, 12));
  const field = (f) => {
    if (f === "5h" && five) {
      const ft = toneOf(five);
      const note = stale ? "" : `<span class="note tone-${ft === "gathering" ? "ok-quiet" : ft}">${esc(stripNote(five, t))}</span>`;
      label.push(`5-hour ${percent(five.used_pct)}${stale ? "" : ", " + stripNote(five, t).replace("~", "about ")}`);
      return `<span class="f"><span class="k">5h</span> ${pctSlot(five.used_pct)}${note ? " " + note : ""}</span>`;
    }
    if (f === "7d" && week) {
      const wt = toneOf(week);
      const risky = !stale && ["will_hit", "watch"].includes(wt);
      label.push(`weekly ${percent(week.used_pct)}${risky ? ", " + stripNote(week, t) : ""}`);
      return `<span class="f"><span class="k">7d</span> ${pctSlot(week.used_pct)}${risky ? ` <span class="note tone-${wt}">${esc(stripNote(week, t))}</span>` : ""}</span>`;
    }
    if (f === "ctx" && act && act.context && !stale) {
      const high = act.context.pct >= 90;
      label.push(`context ${percent(act.context.pct)}`);
      return `<span class="f${high ? " tone-will_hit" : ""}">${high ? levelIcon("will_hit", 12) : ""}<span class="k">ctx</span> ${pctSlot(act.context.pct)}</span>`;
    }
    if (f === "waiting" && waiting.length) {
      label.push(`${waiting.length} ${waiting.length === 1 ? "session" : "sessions"} waiting`);
      return `<span class="pill pill-accent">${waiting.length} waiting</span>`;
    }
    if (f === "status" && st.service && st.service.indicator && !["none", "unknown"].includes(st.service.indicator)) {
      label.push(svc.text);
      return `<span class="f tone-svc">${levelIcon(svc.tone, 12)}<span>${esc(svc.short)}</span></span>`;
    }
    return "";
  };
  const shown = fields.map(field).filter(Boolean);
  // separators between text fields (not before the pill)
  shown.forEach((h, i) => {
    if (i > 0 && !h.startsWith('<span class="pill')) parts.push(sep());
    parts.push(h);
  });
  if (stale) {
    const at = st.source && st.source.updated_at;
    parts.push(sep());
    parts.push(`<span class="f">${esc(asOf(at, t))} · ${esc(lowerFirst(staleReason(st)))}</span>`);
    label.push(`data ${asOf(at, t)}`);
  }
  if (settings.overlay && settings.overlay.click_through) parts.push(`<span class="lock" title="Clicks pass through">${icon("lock", { size: 12 })}</span>`);
  return { cls: "strip--" + tone, html: parts.join(""), label: label.join("; ") };
}

/* ---------------- panel ---------------- */

function limitRow(L, t, stale) {
  const tone = toneOf(L);
  const p = L.pace || {};
  const name = L.id === "five_hour" ? "5-hour" : L.label || "Weekly";
  return `<div class="p-limit">
    <div class="p-row"><span><b>${esc(name)}</b> ${esc(percent(L.used_pct))}</span><span class="muted">${tone === "limited" ? "back " + esc(when(L.resets_at, t)) : "refills " + esc(when(L.resets_at, t))}</span></div>
    ${bullet({ used: L.used_pct, projected: p.projected_at_reset, elapsed: L.elapsed_pct, tone: stale ? "stale" : tone === "gathering" ? "ok" : tone, stale, size: "md", label: limitSentence(L, t, { stale, asOfText: asOf((st.source || {}).updated_at, t) }) })}
  </div>`;
}

function panelHTML(t) {
  const src = (st && st.source) || {};
  const pinIc = pinned ? "pinned" : "pin";
  const head = `<div class="p-head pywebview-drag-region">
    <span class="p-upd">${st ? (isStale(st) ? `${levelIcon("stale", 12)}${esc(asOf(src.updated_at, t))}` : "Updated " + esc(clock(src.updated_at))) : "Connecting…"}</span>
    <button type="button" class="ib" data-act="pin" aria-pressed="${pinned}" aria-label="${pinned ? "Unpin" : "Pin open"}" title="${pinned ? "Unpin" : "Pin open"}">${icon(pinIc, { size: 14 })}</button>
    <button type="button" class="ib" data-act="collapse" aria-label="Collapse" title="Collapse">${icon("chevron-up", { size: 14 })}</button>
  </div>`;
  if (!st) {
    return head + `<div class="p-verdict">${levelIcon("gathering", 18)}<div><div class="p-title">${meta.conn === "offline" ? "Tracker not responding" : "Connecting to the tracker…"}</div><div class="p-detail">${meta.conn === "offline" ? "The background app isn't answering." : ""}</div></div></div>`;
  }
  const stale = isStale(st);
  const d = st.decision || {};
  const bind = bindingLimit(st);
  let tone, titleTxt, detail;
  if (stale) {
    tone = "stale";
    titleTxt = "Usage data is out of date";
    detail = `${staleReason(st)} · ${asOf(src.updated_at, t)}`;
  } else {
    const v = verdict(st, t);
    tone = bind ? toneOf(bind) : v.tone;
    titleTxt = d.title || v.title;
    detail = d.detail || v.detail;
  }
  if (tone === "limited" && bind) {
    const back = d.resume_at || bind.resets_at;
    detail = `Back at ${when(back, t)} · ` + "{{IN}}";
    detail = esc(detail).replace("{{IN}}", timeSpan(back, "in", t));
  } else detail = esc(detail);
  const verdictH = `<div class="p-verdict">${levelIcon(tone === "gathering" ? "gathering" : tone, 18)}<div><div class="p-title">${esc(titleTxt)}</div>${detail ? `<div class="p-detail">${detail}</div>` : ""}</div></div>`;

  const lims = [limitById(st, "five_hour"), limitById(st, "seven_day")].filter(Boolean);
  if (bind && !lims.includes(bind)) lims.push(bind);
  const limitsH = lims.map((L) => limitRow(L, t, stale)).join("");

  const rows = [];
  const a = activeSession(st.sessions);
  const w = waitingSessions(st.sessions);
  if (a && a.context && !w.includes(a)) rows.push(`<div class="p-row"><span class="p-proj">${esc(a.project)}</span><span class="${a.context.pct >= 90 ? "tone-will_hit" : ""}">ctx ${esc(percent(a.context.pct))}</span></div>`);
  for (const s of w.slice(0, 3)) rows.push(`<div class="p-row"><span class="p-proj">${esc(s.project)}</span><span class="p-wait">${s.state === "needs_approval" ? "needs approval" : "waiting"} ${timeSpan(s.since, "since", t)}</span></div>`);
  if (w.length > 3) rows.push(`<div class="p-row muted">+${w.length - 3} more waiting</div>`);
  const sessH = rows.length ? `<div class="p-sess">${rows.join("")}</div>` : "";

  const btns = [`<button type="button" class="btn btn-accent p-main" data-act="dashboard">Open dashboard</button>`];
  const sug = d.suggestion;
  if (stale) btns.push(`<button type="button" class="btn" data-act="refresh">Refresh</button>`);
  else if (sug && sug.email) btns.push(`<button type="button" class="btn" data-act="switch" data-email="${esc(sug.email)}" aria-description="Opens Claude sign-in so you can log in as that account" title="Opens Claude sign-in so you can log in as ${esc(sug.email)}">Switch account</button>`);
  else if (["will_hit", "watch", "limited"].includes(tone) && !(st.mute && st.mute.until > t)) btns.push(`<button type="button" class="btn" data-act="mute">Mute until refill</button>`);
  return head + verdictH + `<div class="p-limits">${limitsH}</div>` + sessH + `<div class="p-btns">${btns.join("")}</div><div class="p-err" role="alert" hidden></div>`;
}

/* ---------------- render + resize ---------------- */

function render() {
  const t = now();
  const s = stripHTML(t);
  strip.className = "strip pywebview-drag-region " + s.cls;
  const inner = `<span class="strip-in">${s.html}</span>`;
  if (strip.__html !== inner) {
    strip.innerHTML = inner;
    strip.__html = inner;
  }
  strip.setAttribute("aria-label", (s.label || "Claude usage") + ". Show details");
  strip.setAttribute("aria-expanded", String(expanded));
  ov.classList.toggle("is-expanded", expanded);
  panel.classList.toggle("is-stale", !!(st && isStale(st)));
  strip.hidden = expanded;
  panel.hidden = !expanded;
  if (expanded) {
    const html = panelHTML(t);
    if (panel.__html !== html) {
      const focused = document.activeElement && panel.contains(document.activeElement) ? document.activeElement.getAttribute("data-act") : null;
      panel.innerHTML = html;
      panel.__html = html;
      if (focused) panel.querySelector(`[data-act="${focused}"]`)?.focus();
    }
  }
  const op = Math.max(30, Math.min(100, Number((settings.overlay || {}).opacity) || 90));
  ov.style.setProperty("--ov-opacity", String(op / 100));
  refreshTimes(document.body, t);
  // announce verdict changes politely
  const title = st && st.decision ? st.decision.title : "";
  if (lastTitle && title && title !== lastTitle) announce(title);
  lastTitle = title;
  scheduleResize();
}

// Re-send while the window still doesn't match (a request can arrive before the host
// window exists, or be clamped), up to a few tries per size.
let lastSize = "";
let tries = 0;
const scheduleResize = debounce(() => {
  const el = expanded ? panel : strip;
  const r = el.getBoundingClientRect();
  const w = Math.ceil(r.width), h = Math.ceil(r.height);
  if (!w || !h) return;
  const key = w + "x" + h;
  const fits = Math.abs(window.innerWidth - w) <= 1 && Math.abs(window.innerHeight - h) <= 1;
  if (key !== lastSize) tries = 0;
  if ((key === lastSize && fits) || tries >= 5) return;
  lastSize = key;
  tries += 1;
  post("overlay_resize", { w, h, expanded }).catch(() => {});
  setTimeout(scheduleResize, 800);
}, 60);
new ResizeObserver(scheduleResize).observe(ov);
window.addEventListener("resize", scheduleResize);

function setExpanded(on, reason = null) {
  clearTimeout(closeTimer);
  if (expanded === on) return;
  expanded = on;
  openedBy = on ? reason : null;
  render();
  if (on && reason === "key") panel.querySelector("[data-act=pin]")?.focus();
  if (!on && reason === "key") strip.focus();
}

/* ---------------- interaction ---------------- */

// Hover only brightens the strip; a click opens the panel. Leaving an open, unpinned panel
// folds it back after a moment so it doesn't sit over your work.
ov.addEventListener("mouseenter", () => {
  clearTimeout(closeTimer);
  ov.classList.add("is-hover");
});
ov.addEventListener("mouseleave", () => {
  ov.classList.remove("is-hover");
  if (expanded && !pinned) closeTimer = setTimeout(() => setExpanded(false), 1200);
});

// click (but not the end of a drag) expands immediately
let downAt = null;
strip.addEventListener("pointerdown", (e) => (downAt = [e.screenX, e.screenY]));
strip.addEventListener("click", (e) => {
  if (downAt && e.detail > 0 && Math.hypot(e.screenX - downAt[0], e.screenY - downAt[1]) > 4) return;
  setExpanded(true, e.detail === 0 ? "key" : "click");
});

panel.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act]");
  if (!b) return;
  const a = b.getAttribute("data-act");
  const err = panel.querySelector(".p-err");
  const onError = (x) => {
    if (err) {
      err.textContent = x.message;
      err.hidden = false;
    }
  };
  if (a === "collapse") {
    if (pinned) togglePin(false);
    setExpanded(false, e.detail === 0 ? "key" : null);
  } else if (a === "pin") togglePin(!pinned);
  else if (a === "dashboard") await act("open_dashboard", {}, { button: b, onError });
  else if (a === "switch") await act("login", { email: b.getAttribute("data-email") }, { button: b, onError });
  else if (a === "mute") await act("mute", { for: "refill" }, { button: b, onError });
  else if (a === "refresh") await act("refresh", {}, { button: b, onError });
});

async function togglePin(on) {
  pinned = on;
  pinPending = on;
  render();
  const r = await act("overlay", { pin: on }, { onError: () => {} });
  if (!r) {
    pinned = !on;
    pinPending = null;
    render();
  }
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && expanded) {
    if (pinned) togglePin(false);
    setExpanded(false, "key");
  }
});

/* ---------------- data ---------------- */

async function loadSettings() {
  try {
    const s = await getSettings();
    if (s && s.overlay) settings = s;
    render();
  } catch {}
}

subscribe((s, m) => {
  st = s;
  meta = m;
  applyAppearance(s);
  const serverPin = !!(s && s.app && s.app.overlay && s.app.overlay.pinned);
  if (pinPending === null || pinPending === serverPin) {
    pinPending = null;
    if (serverPin !== pinned) {
      pinned = serverPin;
      if (pinned) expanded = true;
    }
  }
  render();
});
subscribeSettings((s) => {
  if (s && s.overlay) {
    settings = s;
    render();
  }
});
onTick((t) => {
  render();
  return refreshTimes(document.body, t);
});
setInterval(loadSettings, 60000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) loadSettings();
});

// dev/screenshot hook: ?expanded=1 opens the panel
if (new URLSearchParams(location.search).get("expanded") === "1") expanded = true;
loadSettings();
start();
render();
