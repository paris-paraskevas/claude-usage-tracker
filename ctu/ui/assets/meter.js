// Bullet bar (spec §6): track, used fill in the level colour, hatched
// projection from used to min(projected, 100), a 2 px even-pace tick at
// elapsed_pct, and role="meter" with a full-sentence label.

import { esc, percent, when, clock, limitNoun } from "./format.js";

const clamp = (v) => Math.max(0, Math.min(100, v));
const fin = (v) => typeof v === "number" && Number.isFinite(v);

/**
 * @param {object} o
 *  used, projected?, elapsed?, mark? (e.g. 90 for "compact at 90%"),
 *  tone: ok | watch | will_hit | limited | neutral | ink | accent | stale,
 *  size: lg (10 px) | md (8 px) | sm (6 px), label: accessible sentence,
 *  valueText?: short value text for assistive tech
 */
export function bullet({ used, projected = null, elapsed = null, mark = null, tone = "neutral", size = "lg", label = "", valueText = null, stale = false }) {
  const u = fin(used) ? clamp(used) : 0;
  const parts = [];
  parts.push(`<span class="meter-fill" style="width:${u.toFixed(2)}%"></span>`);
  if (fin(projected) && projected > u && tone !== "limited" && !stale) {
    const p = clamp(projected);
    parts.push(`<span class="meter-proj" style="left:${u.toFixed(2)}%;width:${(p - u).toFixed(2)}%"></span>`);
  }
  let outside = "";
  if (fin(elapsed) && !stale) outside += `<span class="meter-tick" style="left:${clamp(elapsed).toFixed(2)}%"></span>`;
  if (fin(mark)) outside += `<span class="meter-mark" style="left:${clamp(mark).toFixed(2)}%"></span>`;
  const vt = valueText || `${percent(used)} used`;
  return `<div class="meter meter--${size} lvl-${tone}${stale ? " is-stale" : ""}" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(u)}" aria-valuetext="${esc(vt)}" aria-label="${esc(label)}"><span class="meter-bar">${parts.join("")}</span>${outside}</div>`;
}

const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);

/** Full sentence describing a limit for screen readers. */
export function limitSentence(limit, now, { stale = false, asOfText = "" } = {}) {
  if (!limit) return "";
  const noun = cap(limitNoun(limit));
  const pace = limit.pace || {};
  const reset = limit.resets_at;
  let s;
  if (limit.level === "limited" || limit.used_pct >= 100) {
    s = `${noun} reached. Refills ${when(reset, now)}.`;
  } else {
    s = `${noun}: ${percent(limit.used_pct)} used, refills ${when(reset, now)}.`;
    if (pace.confidence === "gathering" || !fin(pace.rate_pct_h)) s += " Not enough recent data to forecast yet.";
    else if (limit.level === "will_hit" && fin(pace.eta_100)) s += ` At this pace the limit is reached around ${clock(pace.eta_100)}.`;
    else if (fin(pace.projected_at_reset)) s += ` At this pace about ${percent(Math.min(pace.projected_at_reset, 100))} by the refill.`;
    if (fin(limit.elapsed_pct)) s += ` Even pace is ${percent(limit.elapsed_pct)}.`;
  }
  if (stale) s += ` Data ${asOfText || "is out of date"}.`;
  return s;
}
