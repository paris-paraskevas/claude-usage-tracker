import { setFormatDefaults } from "./format.js";
// Theme + accent. The OS theme drives prefers-color-scheme; state.app.theme
// ("light" | "dark" | "system") or ?theme= can force one via [data-theme].
// state.app.accent ("#RRGGBB" or { light, dark }) becomes --accent, adjusted
// until button text and accent-coloured text clear 4.5:1 in the current theme.

const root = document.documentElement;
let lastKey = "";
let current = { accent: null, theme: null };
const mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

function hexToRgb(hex) {
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(String(hex || "").trim());
  if (!m) return null;
  let h = m[1];
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}
const toHex = (rgb) => "#" + rgb.map((v) => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, "0")).join("");
function lum([r, g, b]) {
  const f = (v) => {
    v /= 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}
export function contrast(a, b) {
  const la = lum(a), lb = lum(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}
const mix = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);

function isDark() {
  const forced = root.getAttribute("data-theme");
  if (forced === "dark") return true;
  if (forced === "light") return false;
  return !!(mq && mq.matches);
}

/** Pick accent shades that satisfy contrast for the current theme. */
function accentFor(raw, dark) {
  let base = hexToRgb(typeof raw === "object" && raw ? (dark ? raw.dark : raw.light) : raw);
  if (!base) return null;
  const card = dark ? [43, 43, 43] : [255, 255, 255];
  const toward = dark ? [255, 255, 255] : [0, 0, 0];
  const onAccent = dark ? [0, 0, 0] : [255, 255, 255];
  let a = base;
  for (let i = 0; i < 20 && (contrast(a, onAccent) < 4.5 || contrast(a, card) < 4.5); i++) a = mix(a, toward, 0.08);
  return {
    accent: toHex(a),
    text: toHex(a),
    hover: toHex(mix(a, card, 0.1)),
    pressed: toHex(mix(a, card, 0.2)),
    soft: toHex(mix(card, a, dark ? 0.22 : 0.12)),
    on: toHex(onAccent),
  };
}

function apply() {
  const dark = isDark();
  const key = JSON.stringify([current.accent, dark]);
  if (key === lastKey) return;
  lastKey = key;
  const a = current.accent ? accentFor(current.accent, dark) : null;
  const props = { "--accent": "accent", "--accent-text": "text", "--accent-hover": "hover", "--accent-pressed": "pressed", "--accent-soft": "soft", "--on-accent": "on" };
  for (const [prop, k] of Object.entries(props)) {
    if (a) root.style.setProperty(prop, a[k]);
    else root.style.removeProperty(prop);
  }
}

/** Apply state.app.theme / state.app.accent. Safe to call on every state. */
export function applyAppearance(state) {
  const app = (state && state.app) || {};
  const q = new URLSearchParams(location.search);
  const theme = q.get("theme") || app.theme || null;
  if (theme === "light" || theme === "dark") root.setAttribute("data-theme", theme);
  else root.removeAttribute("data-theme");
  if (app.clock === "24h" || app.clock === "12h") setFormatDefaults({ hour12: app.clock === "12h" });
  current.accent = q.get("accent") ? "#" + q.get("accent").replace(/^#/, "") : app.accent || null;
  lastKey = lastKey + "!"; // force re-evaluation when data-theme changed
  apply();
}

if (mq && mq.addEventListener) mq.addEventListener("change", () => { lastKey = ""; apply(); });
