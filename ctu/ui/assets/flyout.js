// Tray flyout (A-Tray board): verdict, 5-hour + weekly bars, and three
// buttons (Dashboard, Overlay toggle, Mute 1 h). Escape, or any button once
// its action succeeds, posts flyout_close so the host hides the window.

import { start, subscribe, onTick, now } from "./state.js";
import { applyAppearance } from "./theme.js";
import { esc, refreshTimes, timeSpan } from "./dom.js";
import { icon, levelIcon } from "./icons.js";
import { bullet, limitSentence } from "./meter.js";
import { post } from "./actions.js";
import { percent, clock, when, asOf } from "./format.js";
import { bindingLimit, limitById, toneOf, isStale, staleReason, verdict } from "./model.js";

const vEl = document.getElementById("fly-verdict");
const limEl = document.getElementById("fly-limits");
const muteEl = document.getElementById("fly-mute");
const errEl = document.getElementById("fly-err");
const btnOverlay = document.querySelector('[data-act="overlay"]');
const btnMute = document.querySelector('[data-act="mute"]');
let st = null;
let meta = { conn: "connecting" };

for (const el of document.querySelectorAll("[data-icon]")) el.innerHTML = icon(el.getAttribute("data-icon"));

function set(el, html) {
  if (el.__html !== html) {
    el.innerHTML = html;
    el.__html = html;
  }
}

function render() {
  const t = now();
  if (!st) {
    const off = meta.conn === "offline";
    set(vEl, `${levelIcon(off ? "stale" : "gathering", 16)}<div class="fly-v-text"><div class="fly-title">${off ? "Tracker not responding" : "Connecting…"}</div><div class="fly-cap">${off ? "The background app isn't answering." : ""}</div></div>`);
    return;
  }
  const src = st.source || {};
  const d = st.decision || {};
  const bind = bindingLimit(st);
  const stale = isStale(st);
  let tone, title, cap;
  if (stale) {
    tone = "stale";
    title = "Usage data is out of date";
    cap = `${esc(staleReason(st))} · ${esc(asOf(src.updated_at, t))}`;
  } else {
    const v = verdict(st, t);
    tone = bind ? toneOf(bind) : v.tone;
    title = d.title || v.title;
    if (tone === "limited" && bind) {
      const back = d.resume_at || bind.resets_at;
      cap = `Back at ${esc(when(back, t))} · ${timeSpan(back, "in", t)}`;
    } else cap = bind ? `Refill at ${esc(when(bind.resets_at, t))} · updated ${esc(clock(src.updated_at))}` : `Updated ${esc(clock(src.updated_at))}`;
  }
  set(vEl, `${levelIcon(tone, 16)}<div class="fly-v-text"><div class="fly-title" id="fly-title">${esc(title)}</div><div class="fly-cap">${cap}</div></div>`);

  const rows = [limitById(st, "five_hour"), limitById(st, "seven_day")].filter(Boolean).map((L) => {
    const lt = toneOf(L);
    return `<div class="fly-limit">
      <div class="fly-row"><span>${esc(L.id === "five_hour" ? "5-hour" : "Weekly")}</span><span class="num${stale ? " tone-stale" : ""}">${esc(percent(L.used_pct))}</span></div>
      ${bullet({ used: L.used_pct, elapsed: L.elapsed_pct, tone: stale ? "stale" : lt === "gathering" ? "ok" : lt, stale, size: "sm", label: limitSentence(L, t, { stale, asOfText: asOf(src.updated_at, t) }) })}
    </div>`;
  });
  set(limEl, rows.join("") || `<div class="fly-cap">No usage readings yet.</div>`);

  const shown = !!(st.app && st.app.overlay && st.app.overlay.shown);
  btnOverlay.setAttribute("aria-pressed", String(shown));
  btnOverlay.title = shown ? "Hide the overlay" : "Show the overlay";
  const muted = st.mute && st.mute.until && st.mute.until > t;
  btnMute.setAttribute("aria-pressed", String(!!muted));
  btnMute.querySelector(".fbtn-label").textContent = muted ? "Unmute" : "Mute 1 h";
  btnMute.title = muted ? "Turn alerts back on" : "Mute alerts for an hour";
  muteEl.hidden = !muted;
  if (muted) set(muteEl, `${icon("bell-off", { size: 12 })}<span>Alerts muted until ${esc(when(st.mute.until, t))}</span>`);
  refreshTimes(document.body, t);
}

async function close() {
  try {
    await post("flyout_close", {});
  } catch {}
}

document.querySelector(".fly-btns").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act]");
  if (!b) return;
  errEl.hidden = true;
  const a = b.getAttribute("data-act");
  b.disabled = true;
  try {
    if (a === "dashboard") await post("open_dashboard", {});
    else if (a === "overlay") await post("overlay", { show: b.getAttribute("aria-pressed") !== "true" });
    else if (a === "mute") await post("mute", { for: b.getAttribute("aria-pressed") === "true" ? "off" : "hour" });
    await close();
  } catch (err) {
    errEl.textContent = err.message;
    errEl.hidden = false;
  } finally {
    b.disabled = false;
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") close();
});

subscribe((s, m) => {
  st = s;
  meta = m;
  applyAppearance(s);
  render();
});
onTick((t) => {
  render();
  return refreshTimes(document.body, t);
});
start();
render();
