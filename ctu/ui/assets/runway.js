// Runway chart for the binding limit (spec §6, Now view hero).
//
//   x: window start -> reset, y: 0-100 % used, dotted LIMIT line at 100.
//   used line (history + now), dashed even-pace diagonal, dashed projection in
//   the level colour to eta_100 (or to the reset), a shaded band between
//   eta_100 and the reset labelled with its length, a now marker, and a thin
//   line at sustainable_pct_h ("X%/h makes it").
//
// runwayModel() is pure geometry (tested in tests/ui/runway.test.mjs);
// renderRunway() turns a model into an SVG string.

import { clock, weekday, percent, duration, when, rate, esc, limitNoun } from "./format.js";

export const PAD = { l: 44, r: 12, t: 26, b: 30 };
const fin = (v) => typeof v === "number" && Number.isFinite(v);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const CHAR_W = 6.8; // px per character at 12 px, generous so labels never collide
export const textWidth = (s, px = 12) => String(s).length * CHAR_W * (px / 12);

/** Minutes east of UTC for an epoch, in the runtime's zone. */
export function localOffsetMin(t) {
  return -new Date(t * 1000).getTimezoneOffset();
}

/**
 * Axis ticks aligned to local hours (short windows) or local midnights
 * (weekly windows). `off(t)` gives minutes east of UTC so the maths does not
 * depend on the machine's zone.
 */
export function axisTicks(start, end, X, { off = localOffsetMin, minGap = 52, reserveRight = 0, x0 = 0, x1 = Infinity } = {}) {
  const span = end - start;
  const steps = span <= 8 * 3600 ? [3600, 7200, 10800] : span <= 2 * 86400 ? [3 * 3600, 6 * 3600, 12 * 3600] : [86400, 2 * 86400];
  for (const step of steps) {
    const out = [];
    const o0 = off(start) * 60;
    let local = Math.ceil((start + o0) / step) * step;
    for (let guard = 0; guard < 400; guard++, local += step) {
      const approx = local - o0;
      const t = local - off(approx) * 60;
      if (t > end) break;
      if (t <= start) continue;
      const x = X(t);
      if (x < x0 + 14 || x > x1 - reserveRight) continue;
      out.push({ t, x, day: step >= 86400 });
    }
    let ok = true;
    for (let i = 1; i < out.length; i++) if (out[i].x - out[i - 1].x < minGap) ok = false;
    if (ok) return out;
  }
  return [];
}

/**
 * Geometry of the runway chart.
 * @param {object} p
 *  limit: a state limit; history: [[t, used_pct], ...]; now: epoch s;
 *  width/height: px; off: tz offset fn (minutes east of UTC).
 */
export function runwayModel({ limit, history = [], now, width = 800, height = 220, off = localOffsetMin }) {
  const W = Math.max(240, Math.round(width));
  const H = Math.max(140, Math.round(height));
  const windowS = fin(limit.window_s) && limit.window_s > 0 ? limit.window_s : 18000;
  const end = limit.resets_at;
  const start = end - windowS;
  const x0 = PAD.l, x1 = W - PAD.r, y0 = PAD.t, y1 = H - PAD.b;
  const tNow = clamp(now, start, end);
  const X = (t) => x0 + ((clamp(t, start, end) - start) / (end - start)) * (x1 - x0);
  const Y = (p) => y1 - (clamp(p, 0, 100) / 100) * (y1 - y0);
  const used = fin(limit.used_pct) ? limit.used_pct : 0;
  const pace = limit.pace || {};

  const limited = limit.level === "limited" || used >= 100;
  const gathering = !limited && (pace.confidence === "gathering" || !fin(pace.rate_pct_h));
  const etaValid = fin(pace.eta_100) && pace.eta_100 > tNow && pace.eta_100 <= end;
  const mode = limited ? "limited" : gathering ? "gathering" : etaValid ? "eta" : "on_pace";

  // ---- used line: samples in this window up to now, ending at the live value
  let pts = (Array.isArray(history) ? history : [])
    .filter((s) => Array.isArray(s) && fin(s[0]) && fin(s[1]) && s[0] >= start - 1 && s[0] <= tNow + 1)
    .map((s) => [s[0], s[1]])
    .sort((a, b) => a[0] - b[0]);
  if (!pts.length || pts[pts.length - 1][0] < tNow - 1) pts.push([tNow, used]);
  else pts[pts.length - 1] = [tNow, used];
  // keep at most two samples per pixel column (first and last)
  const cols = new Map();
  for (const s of pts) {
    const c = Math.round(X(s[0]));
    const e = cols.get(c);
    if (!e) cols.set(c, [s]);
    else e[1] = s;
  }
  pts = [...cols.values()].flat();
  const usedPts = pts.map(([t, v]) => [X(t), Y(v)]);

  const nowPt = { t: tNow, pct: used, x: X(tNow), y: Y(used) };
  const evenPace = { x1: X(start), y1: Y(0), x2: X(end), y2: Y(100) };

  let projection = null, sustainable = null, dry = null, flat = null, projEnd = null;
  if (mode === "eta") {
    projection = { x1: nowPt.x, y1: nowPt.y, x2: X(pace.eta_100), y2: Y(100), t2: pace.eta_100, pct2: 100 };
    dry = { x: X(pace.eta_100), w: X(end) - X(pace.eta_100), seconds: end - pace.eta_100, t0: pace.eta_100, t1: end };
  } else if (mode === "on_pace") {
    projEnd = fin(pace.projected_at_reset) ? pace.projected_at_reset : used + (pace.rate_pct_h * (end - tNow)) / 3600;
    projection = { x1: nowPt.x, y1: nowPt.y, x2: X(end), y2: Y(Math.min(projEnd, 100)), t2: end, pct2: projEnd };
  } else if (mode === "limited") {
    let hitT = tNow;
    for (const [t, v] of pts) if (v >= 100) { hitT = t; break; }
    flat = { x1: X(hitT), x2: X(end), y: Y(100), t0: hitT };
    dry = { x: X(hitT), w: X(end) - X(hitT), seconds: end - hitT, t0: hitT, t1: end };
  }
  if ((mode === "eta" || mode === "on_pace") && (limit.level === "will_hit" || limit.level === "watch") && fin(pace.sustainable_pct_h)) {
    const endPct = used + (pace.sustainable_pct_h * (end - tNow)) / 3600;
    sustainable = { x1: nowPt.x, y1: nowPt.y, x2: X(end), y2: Y(Math.min(endPct, 100)), rate: pace.sustainable_pct_h };
  }

  const ticks = axisTicks(start, end, X, { off, x0, x1, reserveRight: 96 });

  return {
    mode, level: limit.level, id: limit.id, W, H, x0, x1, y0, y1, start, end, now: tNow,
    used, X, Y, usedPts, nowPt, evenPace, projection, projEnd, sustainable, dry, flat, ticks,
    runwaySec: mode === "eta" ? pace.eta_100 - tNow : mode === "limited" ? 0 : null,
    refillSec: end - tNow,
    rate: fin(pace.rate_pct_h) ? pace.rate_pct_h : null,
    history: pts,
  };
}

/** Value under the pointer: past -> sampled usage; future -> projection and even pace. */
export function valueAt(m, t) {
  const even = (100 * (t - m.start)) / (m.end - m.start);
  if (t <= m.now) {
    const h = m.history;
    let v = h.length ? h[0][1] : m.used;
    for (let i = 0; i < h.length; i++) {
      if (h[i][0] <= t) v = h[i][1];
      else break;
    }
    return { t, past: true, used: v, even };
  }
  let proj = null;
  if (m.mode === "eta" && m.projection) proj = Math.min(100, m.used + ((100 - m.used) * (t - m.now)) / (m.projection.t2 - m.now));
  else if (m.mode === "on_pace" && m.projection) proj = m.used + ((m.projEnd - m.used) * (t - m.now)) / (m.end - m.now);
  else if (m.mode === "limited") proj = 100;
  return { t, past: false, projected: proj, even };
}

/** One-sentence description of the chart for assistive tech. */
export function runwaySentence(m, { asOf = null } = {}) {
  const noun = limitNoun({ id: m.id });
  let s = `${noun.charAt(0).toUpperCase() + noun.slice(1)} window from ${clock(m.start)} to ${clock(m.end)}. `;
  s += `${percent(m.used)} used ${asOf ? asOf.charAt(0).toLowerCase() + asOf.slice(1) : "by " + clock(m.now)}.`;
  if (asOf) return s + " The data is out of date, so there is no forecast.";
  if (m.mode === "eta") s += ` At ${rate(m.rate)} the limit is reached at ${clock(m.projection.t2)}, ${duration(m.dry.seconds, { style: "long" })} before the refill.`;
  else if (m.mode === "on_pace") s += ` At ${rate(m.rate)} about ${percent(Math.min(m.projEnd, 100))} is used by the refill.`;
  else if (m.mode === "limited") s += ` The limit is reached; it refills at ${clock(m.end)}.`;
  else s += " Not enough recent data to forecast yet.";
  if (m.sustainable) s += ` ${rate(m.sustainable.rate)} or less makes it to the refill.`;
  return s;
}

const f1 = (v) => (Math.round(v * 10) / 10).toString();

/** Does a label box touch a line segment [x1, y1, x2, y2]? (sampled every 3 px) */
export function boxHitsSegment(box, sg, pad = 2) {
  const len = Math.hypot(sg[2] - sg[0], sg[3] - sg[1]);
  const n = Math.max(1, Math.ceil(len / 3));
  for (let i = 0; i <= n; i++) {
    const x = sg[0] + ((sg[2] - sg[0]) * i) / n;
    const y = sg[1] + ((sg[3] - sg[1]) * i) / n;
    if (x >= box.l - pad && x <= box.r + pad && y >= box.t - pad && y <= box.b + pad) return true;
  }
  return false;
}

/** Line segments a label must not cross. */
export function obstacles(m, { stale = false, withNowLine = true } = {}) {
  const sg = [];
  const e = m.evenPace;
  sg.push([e.x1, e.y1, e.x2, e.y2]);
  if (!stale) {
    if (m.projection) sg.push([m.projection.x1, m.projection.y1, m.projection.x2, m.projection.y2]);
    if (m.sustainable) sg.push([m.sustainable.x1, m.sustainable.y1, m.sustainable.x2, m.sustainable.y2]);
    if (m.flat) sg.push([m.flat.x1, m.flat.y, m.flat.x2, m.flat.y]);
  }
  for (let i = 1; i < m.usedPts.length; i++) sg.push([m.usedPts[i - 1][0], m.usedPts[i - 1][1], m.usedPts[i][0], m.usedPts[i][1]]);
  if (withNowLine) sg.push([m.nowPt.x, m.y0, m.nowPt.x, m.y1]);
  return sg;
}

/**
 * SVG markup for a model. opts: { stale, asOfText }.
 * A stale chart shows only what was measured: no projection, band or
 * sustainable line, and the now point sits at the reading time.
 * Styling comes from classes (dashboard.css), so themes and forced colours apply.
 */
export function renderRunway(m, { stale = false, asOfText = "" } = {}) {
  const parts = [];
  const tone = m.mode === "limited" ? "limited" : m.level;
  // horizontal grid (solid hairlines) + y labels
  for (const p of [0, 50]) {
    parts.push(`<line class="rw-grid${p === 0 ? " rw-base" : ""}" x1="${m.x0}" x2="${m.x1}" y1="${f1(m.Y(p))}" y2="${f1(m.Y(p))}"/>`);
    parts.push(`<text class="rw-ylab" x="${m.x0 - 8}" y="${f1(m.Y(p) + 4)}" text-anchor="end">${p}%</text>`);
  }
  parts.push(`<line class="rw-limit" x1="${m.x0}" x2="${m.x1}" y1="${m.y0}" y2="${m.y0}"/>`);
  parts.push(`<text class="rw-ylab" x="${m.x0 - 8}" y="${m.y0 + 4}" text-anchor="end">100%</text>`);
  parts.push(`<text class="rw-lab rw-lab-limit" x="${m.x0 + 4}" y="${m.y0 - 7}">Limit</text>`);

  // x ticks
  for (const tk of m.ticks) {
    parts.push(`<line class="rw-xtick" x1="${f1(tk.x)}" x2="${f1(tk.x)}" y1="${m.y1}" y2="${m.y1 + 4}"/>`);
    parts.push(`<text class="rw-xlab" x="${f1(tk.x)}" y="${m.y1 + 19}" text-anchor="middle">${esc(tk.day ? weekday(tk.t) : clock(tk.t))}</text>`);
  }
  parts.push(`<text class="rw-xlab rw-xlab-end" x="${m.x1}" y="${m.y1 + 19}" text-anchor="end">Refill ${esc(when(m.end, m.now))}</text>`);

  const showDry = !stale && m.dry && m.dry.w > 0.5;
  if (showDry) parts.push(`<rect class="rw-dry" x="${f1(m.dry.x)}" y="${m.y0}" width="${f1(m.dry.w)}" height="${m.y1 - m.y0}"/>`);

  const e = m.evenPace;
  parts.push(`<line class="rw-even" x1="${f1(e.x1)}" y1="${f1(e.y1)}" x2="${f1(e.x2)}" y2="${f1(e.y2)}"/>`);

  if (m.sustainable && !stale) {
    const s = m.sustainable;
    parts.push(`<line class="rw-sust" x1="${f1(s.x1)}" y1="${f1(s.y1)}" x2="${f1(s.x2)}" y2="${f1(s.y2)}"/>`);
  }

  parts.push(`<line class="rw-now-line" x1="${f1(m.nowPt.x)}" x2="${f1(m.nowPt.x)}" y1="${m.y0}" y2="${m.y1}"/>`);

  if (m.usedPts.length > 1) parts.push(`<polyline class="rw-used" points="${m.usedPts.map(([x, y]) => f1(x) + "," + f1(y)).join(" ")}"/>`);

  if (m.projection && !stale) {
    const p = m.projection;
    parts.push(`<line class="rw-proj" x1="${f1(p.x1)}" y1="${f1(p.y1)}" x2="${f1(p.x2)}" y2="${f1(p.y2)}"/>`);
    parts.push(`<circle class="rw-proj-end" cx="${f1(p.x2)}" cy="${f1(p.y2)}" r="4"/>`);
  }
  if (m.flat && !stale) parts.push(`<line class="rw-proj rw-flat" x1="${f1(m.flat.x1)}" y1="${f1(m.flat.y)}" x2="${f1(m.flat.x2)}" y2="${f1(m.flat.y)}"/>`);

  parts.push(`<circle class="rw-now" cx="${f1(m.nowPt.x)}" cy="${f1(m.nowPt.y)}" r="4.5"/>`);

  // ---- labels: first candidate position that crosses no line and no other label
  const boxes = [{ l: m.x0, r: m.x0 + 4 + textWidth("Limit"), t: m.y0 - 18, b: m.y0 - 4 }];
  const segsNoNow = obstacles(m, { stale, withNowLine: false });
  const segsAll = obstacles(m, { stale, withNowLine: true });
  const boxOf = (txt, x, y, anchor) => {
    const w = textWidth(txt);
    const l = anchor === "end" ? x - w : anchor === "middle" ? x - w / 2 : x;
    return { l, r: l + w, t: y - 11, b: y + 3 };
  };
  const fits = (box, segs) =>
    box.l >= m.x0 - 2 && box.r <= m.x1 + 2 && box.t >= 0 && box.b <= m.y1 &&
    !boxes.some((b) => !(box.r < b.l - 4 || box.l > b.r + 4 || box.b < b.t - 2 || box.t > b.b + 2)) &&
    !segs.some((sg) => boxHitsSegment(box, sg));
  const place = (txt, cands, cls, segs) => {
    for (const [x, y, anchor] of cands) {
      const box = boxOf(txt, x, y, anchor);
      if (fits(box, segs)) {
        boxes.push(box);
        parts.push(`<text class="rw-lab ${cls}" x="${f1(x)}" y="${f1(y)}" text-anchor="${anchor}">${esc(txt)}</text>`);
        return true;
      }
    }
    return false;
  };

  if (m.mode === "eta" && !stale) {
    const ex = m.projection.x2;
    place("Limit ~" + clock(m.projection.t2), [[ex - 8, m.y0 - 7, "end"], [ex + 8, m.y0 - 7, "start"]], "rw-lab-eta", []);
  }

  const nowTxt = stale
    ? (asOfText || "As of " + clock(m.now)) + " · " + percent(m.used)
    : m.mode === "gathering" ? "Now " + percent(m.used) + " · gathering pace…" : m.mode === "limited" ? "Now" : "Now " + percent(m.used);
  const nx = m.nowPt.x, ny = m.nowPt.y;
  place(nowTxt, [
    [nx + 10, ny + 18, "start"], [nx + 10, ny - 10, "start"], [nx + 10, ny - 26, "start"],
    [nx - 10, ny + 18, "end"], [nx - 10, ny - 10, "end"], [nx - 10, ny - 26, "end"],
    [nx + 10, ny + 34, "start"], [nx - 10, ny + 34, "end"],
  ], "rw-lab-now", segsNoNow);

  if (showDry) {
    const txt = m.mode === "limited" ? "Back at " + clock(m.end) : duration(m.dry.seconds, { style: "long" }) + " at limit";
    place(txt, [[m.x1 - 6, m.y1 - 8, "end"], [m.dry.x + 6, m.y1 - 8, "start"], [m.dry.x - 6, m.y1 - 8, "end"]], "rw-lab-band", segsAll);
  }

  const cls = `rw rw--${m.mode} tone-${tone}${stale ? " is-stale" : ""}`;
  return `<svg class="${cls}" width="${m.W}" height="${m.H}" viewBox="0 0 ${m.W} ${m.H}" role="img" aria-label="${esc(runwaySentence(m, { asOf: stale ? asOfText : null }))}">${parts.join("")}<g class="rw-hover" hidden><line class="rw-cross" y1="${m.y0}" y2="${m.y1}"/><circle class="rw-hdot" r="4"/></g></svg>`;
}
