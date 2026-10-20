// Small multi-series line chart for History › Usage over time (0-100 %).
// Pure geometry + SVG string; hover is wired by the view.

import { dateShort, esc } from "./format.js";

const fin = (v) => typeof v === "number" && Number.isFinite(v);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
export const LPAD = { l: 44, r: 12, t: 14, b: 28 };

/** Split samples into runs where the gap between readings is small. */
export function splitRuns(values, maxGap) {
  const runs = [];
  let cur = [];
  let prev = null;
  for (const p of values) {
    if (prev && p[0] - prev[0] > maxGap) {
      if (cur.length) runs.push(cur);
      cur = [];
    }
    cur.push(p);
    prev = p;
  }
  if (cur.length) runs.push(cur);
  return runs;
}

function medianGap(values) {
  const g = [];
  for (let i = 1; i < values.length; i++) g.push(values[i][0] - values[i - 1][0]);
  if (!g.length) return 3600;
  g.sort((a, b) => a - b);
  return g[Math.floor(g.length / 2)];
}

export function lineModel({ series, start, end, width, height, off }) {
  const W = Math.max(240, Math.round(width)), H = Math.max(120, Math.round(height));
  const x0 = LPAD.l, x1 = W - LPAD.r, y0 = LPAD.t, y1 = H - LPAD.b;
  const span = Math.max(1, end - start);
  const X = (t) => x0 + ((clamp(t, start, end) - start) / span) * (x1 - x0);
  const Y = (p) => y1 - (clamp(p, 0, 100) / 100) * (y1 - y0);
  const out = series.map((s) => {
    const vals = s.values.filter((p) => fin(p[0]) && fin(p[1]) && p[0] >= start && p[0] <= end).sort((a, b) => a[0] - b[0]);
    const gap = Math.max(3 * medianGap(vals), 2 * 3600);
    const runs = splitRuns(vals, gap).map((r) => r.map(([t, v]) => [X(t), Y(v)]));
    return { ...s, vals, runs };
  });
  // day-aligned ticks with >= 64 px between labels
  const offFn = off || ((t) => -new Date(t * 1000).getTimezoneOffset());
  let ticks = [];
  for (const step of [86400, 2 * 86400, 7 * 86400, 14 * 86400, 30 * 86400, 61 * 86400, 91 * 86400]) {
    ticks = [];
    const o = offFn(start) * 60;
    let local = Math.ceil((start + o) / 86400) * 86400;
    if (step === 7 * 86400 || step === 14 * 86400) {
      // start weeks on Monday (1970-01-01 was a Thursday)
      const dow = (Math.floor(local / 86400) + 4) % 7;
      local += ((8 - dow) % 7) * 86400;
    }
    for (let g = 0; g < 500 && local - o <= end; g++, local += step) {
      const t = local - offFn(local - o) * 60;
      const x = X(t);
      if (x > x0 + 10 && x < x1 - 20) ticks.push({ t, x });
    }
    let ok = true;
    for (let i = 1; i < ticks.length; i++) if (ticks[i].x - ticks[i - 1].x < 64) ok = false;
    if (ok) break;
  }
  return { W, H, x0, x1, y0, y1, X, Y, start, end, series: out, ticks };
}

export function renderLines(m, { label = "" } = {}) {
  const p = [];
  const f1 = (v) => (Math.round(v * 10) / 10).toString();
  for (const v of [0, 50]) {
    p.push(`<line class="rw-grid${v === 0 ? " rw-base" : ""}" x1="${m.x0}" x2="${m.x1}" y1="${f1(m.Y(v))}" y2="${f1(m.Y(v))}"/>`);
    p.push(`<text class="rw-ylab" x="${m.x0 - 8}" y="${f1(m.Y(v) + 4)}" text-anchor="end">${v}%</text>`);
  }
  p.push(`<line class="rw-limit" x1="${m.x0}" x2="${m.x1}" y1="${m.y0}" y2="${m.y0}"/>`);
  p.push(`<text class="rw-ylab" x="${m.x0 - 8}" y="${m.y0 + 4}" text-anchor="end">100%</text>`);
  for (const tk of m.ticks) {
    p.push(`<line class="rw-xtick" x1="${f1(tk.x)}" x2="${f1(tk.x)}" y1="${m.y1}" y2="${m.y1 + 4}"/>`);
    p.push(`<text class="rw-xlab" x="${f1(tk.x)}" y="${m.y1 + 19}" text-anchor="middle">${esc(dateShort(tk.t))}</text>`);
  }
  for (const s of m.series) {
    if (!s.area) continue;
    for (const run of s.runs) {
      if (run.length < 2) continue;
      const pts = run.map(([x, y]) => f1(x) + "," + f1(y)).join(" ");
      p.push(`<polygon class="lc-area lc-${s.key}" points="${f1(run[0][0])},${m.y1} ${pts} ${f1(run[run.length - 1][0])},${m.y1}"/>`);
    }
  }
  for (const s of m.series) {
    for (const run of s.runs) {
      if (run.length === 1) p.push(`<circle class="lc-dot lc-${s.key}" cx="${f1(run[0][0])}" cy="${f1(run[0][1])}" r="2"/>`);
      else p.push(`<polyline class="lc-line lc-${s.key}" points="${run.map(([x, y]) => f1(x) + "," + f1(y)).join(" ")}"/>`);
    }
  }
  return `<svg class="lc" width="${m.W}" height="${m.H}" viewBox="0 0 ${m.W} ${m.H}" role="img" aria-label="${esc(label)}">${p.join("")}<g class="rw-hover" hidden><line class="rw-cross" y1="${m.y0}" y2="${m.y1}"/></g></svg>`;
}

/** Nearest sample of each series at time t. */
export function linesAt(m, t) {
  return m.series.map((s) => {
    let best = null;
    for (const p of s.vals) if (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
    return { key: s.key, name: s.name, at: best };
  });
}
