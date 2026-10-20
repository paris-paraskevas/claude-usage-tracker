// Small DOM helpers shared by every surface.

import { duration, relIn, ago, when, clock, asOf, esc } from "./format.js";

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
export { esc };

/**
 * Replace a container's HTML only when it changed, keeping keyboard focus on
 * the element with the same [data-key] (or id) and text-field caret position.
 */
export function patch(el, html) {
  if (!el) return false;
  if (el.__html === html) return false;
  const active = document.activeElement;
  let key = null;
  let sel = null;
  if (active && el.contains(active)) {
    key = active.getAttribute("data-key") || (active.id ? "#" + active.id : null);
    if ("selectionStart" in active) {
      try {
        sel = [active.selectionStart, active.selectionEnd];
      } catch {}
    }
  }
  el.innerHTML = html;
  el.__html = html;
  if (key) {
    const next = key.startsWith("#") ? el.querySelector(key) : el.querySelector(`[data-key="${CSS.escape(key)}"]`);
    if (next) {
      next.focus({ preventScroll: true });
      if (sel && "setSelectionRange" in next) {
        try {
          next.setSelectionRange(sel[0], sel[1]);
        } catch {}
      }
    }
  }
  return true;
}

/**
 * Ticking time text. Renders a <span> carrying the epoch and a format:
 *   in    -> "in 1 h 52 m" (seconds under 10 min)
 *   left  -> "1 h 52 m"
 *   ago   -> "4 min ago"
 *   since -> "4 min"
 *   when  -> "16:40" / "Thu 09:00"
 *   asof  -> "as of 14:31"
 */
export function timeText(epoch, fmt, nowS) {
  switch (fmt) {
    case "in":
      return relIn(epoch, nowS, { seconds: true });
    case "left":
      return duration(Math.max(0, epoch - nowS), { seconds: true });
    case "ago":
      return ago(epoch, nowS);
    case "since":
      return nowS - epoch < 60 ? "<1 min" : duration(nowS - epoch, { style: "long" });
    case "when":
      return when(epoch, nowS);
    case "asof":
      return asOf(epoch, nowS);
    case "clock":
      return clock(epoch);
    default:
      return "";
  }
}
export function timeSpan(epoch, fmt, nowS, cls = "") {
  if (typeof epoch !== "number") return `<span class="${cls}">—</span>`;
  return `<span class="${cls}" data-t="${epoch}" data-tf="${fmt}">${esc(timeText(epoch, fmt, nowS))}</span>`;
}

/** Update every ticking span under root; returns seconds to the nearest future countdown. */
export function refreshTimes(root, nowS) {
  let nearest = Infinity;
  for (const el of root.querySelectorAll("[data-t]")) {
    const t = +el.getAttribute("data-t");
    const f = el.getAttribute("data-tf");
    const txt = timeText(t, f, nowS);
    if (el.textContent !== txt) el.textContent = txt;
    if ((f === "in" || f === "left") && t > nowS) nearest = Math.min(nearest, t - nowS);
  }
  return nearest;
}

/** Copy text to the clipboard (with a fallback for older WebView builds). */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch {}
    ta.remove();
    return ok;
  }
}

/** Announce text in the page's polite live region (#live). */
export function announce(text) {
  const live = document.getElementById("live");
  if (!live) return;
  live.textContent = "";
  setTimeout(() => (live.textContent = text), 30);
}

/** Debounce helper. */
export function debounce(fn, ms) {
  let t = null;
  return (...a) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
}
