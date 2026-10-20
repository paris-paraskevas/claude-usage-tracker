// History view (spec §7): usage over time (5-hour and weekly), totals,
// streaks, a labelled activity heatmap with a Less -> More legend, models and
// projects. Range 7d / 30d / All. Data: GET /api/v2/alltime?range=…
// (shape documented in tests/ui/fixtures/alltime.json).

import { patch, esc } from "./dom.js";
import { levelIcon } from "./icons.js";
import { getJSON } from "./actions.js";
import { lineModel, renderLines, linesAt } from "./linechart.js";
import {
  tokens, integer, hourLabel, percent, count, dayLabel, monthShortOfKey, weekdayOfKey, dateShort, clock,
} from "./format.js";

export const title = "History";

const RANGES = [
  ["7d", "7 days"],
  ["30d", "30 days"],
  ["all", "All time"],
];
let root = null;
let nowFn = () => Date.now() / 1000;
let range = "30d";
let data = { range: null, status: "idle", body: null, error: "" };
let ro = null;
let chartW = 0;
let lm = null;

try {
  const saved = localStorage.getItem("ctu.history.range");
  if (RANGES.some((r) => r[0] === saved)) range = saved;
} catch {}

export function mount(el, ctx) {
  root = el;
  nowFn = ctx.now;
  root.addEventListener("click", onClick);
  root.addEventListener("pointermove", onHover);
  root.addEventListener("pointerleave", hideHover);
  ro = new ResizeObserver(() => {
    const w = root && root.querySelector(".lc-wrap");
    if (w && Math.abs(w.clientWidth - chartW) > 1) render();
  });
  ro.observe(root);
  load(range);
}
export function unmount() {
  ro.disconnect();
  root.removeEventListener("click", onClick);
  root.removeEventListener("pointermove", onHover);
  root.removeEventListener("pointerleave", hideHover);
  root = null;
}
export function update() {
  // History is a lazy topic; refresh it at most every 10 minutes while open.
  if (data.status === "ok" && Date.now() - (data.at || 0) > 600000) load(range, true);
}

async function load(r, quiet = false) {
  range = r;
  data = { ...data, range: r, status: quiet && data.body ? "ok" : "loading", refreshing: true };
  render();
  try {
    const body = await getJSON("/api/v2/alltime?range=" + encodeURIComponent(r));
    if (range !== r) return;
    data = { range: r, status: "ok", body, error: "", at: Date.now() };
  } catch (e) {
    if (range !== r) return;
    data = { range: r, status: "error", body: data.body, error: e.message };
  }
  render();
}

function onClick(e) {
  const b = e.target.closest("[data-range]");
  if (b) {
    const r = b.getAttribute("data-range");
    try {
      localStorage.setItem("ctu.history.range", r);
    } catch {}
    load(r);
    return;
  }
  if (e.target.closest("[data-act=retry]")) load(range);
}

/* ---------------- render ---------------- */

function head() {
  return `<header class="page-head">
    <h1 class="t-title">History</h1>
    <div class="seg" role="group" aria-label="Range">${RANGES.map(([k, l]) => `<button type="button" data-range="${k}" data-key="r-${k}" aria-pressed="${k === range}">${l}</button>`).join("")}</div>
  </header>`;
}

function render() {
  if (!root) return;
  const d = data.body;
  if (data.status === "loading" && !d) {
    patch(root, head() + `<div aria-busy="true" class="hist-loading"><span class="sr-only">Loading history…</span><div class="skel" style="height:260px"></div><div class="grid-4"><div class="skel" style="height:72px"></div><div class="skel" style="height:72px"></div><div class="skel" style="height:72px"></div><div class="skel" style="height:72px"></div></div><div class="skel" style="height:200px"></div></div>`);
    return;
  }
  if (data.status === "error" && (!d || d.ready === false)) {
    patch(root, head() + `<section class="card"><div class="empty">${levelIcon("error", 24)}<div class="empty-title">Couldn't load your history</div><p class="empty-body">${esc(data.error)}</p><button type="button" class="btn btn-accent" data-act="retry">Try again</button></div></section>`);
    return;
  }
  if (d && d.ready === false) {
    patch(root, head() + `<section class="card"><div class="empty">${levelIcon("gathering", 24)}<div class="empty-title">Reading your Claude Code history…</div><p class="empty-body">The first time, the tracker goes through every past session on this PC. It takes a minute or two; this page fills in when it's done.</p><button type="button" class="btn" data-act="retry">Check again</button></div></section>`);
    return;
  }
  const busy = data.status === "loading" ? ' aria-busy="true"' : "";
  const err = data.status === "error" ? `<div class="callout tone-watch" role="status">${levelIcon("watch", 16)}<div class="callout-body">Couldn't refresh: ${esc(data.error)}. Showing the last loaded numbers.</div><button type="button" class="btn btn-sm" data-act="retry">Try again</button></div>` : "";
  patch(root, head() + err + `<div class="hist${data.status === "loading" ? " is-refetching" : ""}"${busy}>${usageHTML(d)}${statsHTML(d)}${heatmapHTML(d)}<div class="grid-2">${modelsHTML(d)}${projectsHTML(d)}</div></div>`);
  const wrap = root.querySelector(".lc-wrap");
  if (wrap && Math.abs(wrap.clientWidth - chartW) > 1) {
    chartW = wrap.clientWidth;
    render();
  }
}

function rangeStart(d) {
  const now = d.generated_at || nowFn();
  if (range === "7d") return now - 7 * 86400;
  if (range === "30d") return now - 30 * 86400;
  const u = d.usage || {};
  const first = (u.t && u.t[0]) || d.first_ts || now - 30 * 86400;
  return Math.min(first, now - 86400);
}

function usageHTML(d) {
  const u = d.usage || { t: [], five_hour: [], seven_day: [] };
  const t = u.t || [];
  const end = d.generated_at || nowFn();
  const start = rangeStart(d);
  const s5 = t.map((x, i) => [x, (u.five_hour || [])[i]]);
  const s7 = t.map((x, i) => [x, (u.seven_day || [])[i]]);
  const head = `<div class="card-head"><h2 id="usage-h">Usage over time</h2><div class="lc-legend"><span class="key"><span class="key-line lc-key-5h"></span>5-hour</span><span class="key"><span class="key-line lc-key-7d"></span>Weekly</span></div></div>`;
  if (!t.length) {
    return `<section class="card" aria-labelledby="usage-h">${head}<div class="empty"><div class="empty-title">No readings in this range</div><p class="empty-body">The tracker records your 5-hour and weekly levels while it runs. Readings from before v2 may be missing.</p></div></section>`;
  }
  const width = chartW || Math.max(320, (root.clientWidth || 900) - 56 - 38);
  lm = lineModel({
    series: [
      { key: "5h", name: "5-hour", values: s5, area: true },
      { key: "7d", name: "Weekly", values: s7 },
    ],
    start,
    end,
    width,
    height: 220,
  });
  const peak5 = Math.max(0, ...s5.map((p) => p[1]).filter((v) => typeof v === "number"));
  const label = `5-hour and weekly usage from ${dateShort(start)} to ${dateShort(end)}. The 5-hour limit peaked at ${percent(peak5)}.`;
  return `<section class="card" aria-labelledby="usage-h">${head}<div class="lc-wrap">${renderLines(lm, { label })}<div class="rw-tip" hidden></div></div></section>`;
}

function tile(label, value, sub = "") {
  return `<div class="stat"><div class="stat-val">${esc(value)}</div><div class="stat-lab">${esc(label)}</div>${sub ? `<div class="stat-sub">${esc(sub)}</div>` : ""}</div>`;
}

function statsHTML(d) {
  const rl = RANGES.find((r) => r[0] === range)[1].toLowerCase();
  const days = (n) => count(n || 0, "day", "days");
  const tiles = [
    tile("Tokens", tokens(d.tokens || 0), rl === "all time" ? "all time" : "last " + rl),
    tile("Messages", integer(d.messages || 0)),
    tile("Sessions", integer(d.sessions || 0)),
    tile("Active days", integer(d.active_days || 0)),
    tile("Current streak", days(d.streak_current)),
    tile("Longest streak", days(d.streak_longest)),
    tile("Busiest hour", typeof d.peak_hour === "number" ? hourLabel(d.peak_hour) : d.peak_hour || "—"),
    tile("Most used model", d.fav_model || "—"),
  ];
  const cmp = d.compare && d.compare.x ? `<p class="t-caption muted stats-foot">That's about ${esc(integer(d.compare.x))}× the tokens in ${esc(d.compare.name)}.</p>` : "";
  return `<section aria-label="Totals" class="stats-sec"><div class="grid-4 stats">${tiles.join("")}</div>${cmp}</section>`;
}

function heatmapHTML(d) {
  const hm = d.heatmap || { days: [] };
  const days = hm.days || [];
  if (!days.length) return "";
  const weeks = Math.ceil(days.length / 7);
  // month labels: first column whose first day starts a new month
  const months = [];
  let lastM = "";
  for (let w = 0; w < weeks; w++) {
    const day = days[w * 7];
    if (!day) continue;
    const m = day.d.slice(0, 7);
    // label the column that contains the 1st..7th of a month
    const dom = +day.d.slice(8, 10);
    if (m !== lastM && (dom <= 7 || w === 0)) {
      const prev = months[months.length - 1];
      if (!(w === 0 && dom > 14) && !(prev && w - prev.w < 3)) months.push({ w, label: monthShortOfKey(day.d) });
      lastM = m;
    } else if (m !== lastM) lastM = m;
  }
  const cells = days
    .map((x) => `<span class="hm-cell l${x.lvl | 0}" title="${esc(dayLabel(x.d))} · ${x.tok ? esc(tokens(x.tok)) + " tokens" : "no activity"}"></span>`)
    .join("");
  const active = days.filter((x) => x.tok > 0).length;
  const busiest = days.reduce((a, b) => (b.tok > (a ? a.tok : 0) ? b : a), null);
  const label = `Activity over the last ${weeks} weeks: ${active} active days.${busiest ? " Busiest day " + dayLabel(busiest.d) + " with " + tokens(busiest.tok) + " tokens." : ""}`;
  const wd = (i) => weekdayOfKey(days[i] ? days[i].d : "2026-10-18");
  return `<section class="card heat-card" aria-labelledby="heat-h">
    <div class="card-head"><h2 id="heat-h">Activity</h2><span class="card-sub">${esc(count(active, "active day", "active days"))} in the last ${weeks} weeks</span></div>
    <div class="hm" style="--weeks:${weeks}" role="img" aria-label="${esc(label)}">
      <div class="hm-months" aria-hidden="true">${months.map((m) => `<span style="grid-column:${m.w + 1} / span 3">${esc(m.label)}</span>`).join("")}</div>
      <div class="hm-days" aria-hidden="true"><span></span><span>${esc(wd(1))}</span><span></span><span>${esc(wd(3))}</span><span></span><span>${esc(wd(5))}</span><span></span></div>
      <div class="hm-grid" aria-hidden="true">${cells}</div>
    </div>
    <div class="hm-legend" aria-hidden="true"><span>Less</span><span class="hm-cell l0"></span><span class="hm-cell l1"></span><span class="hm-cell l2"></span><span class="hm-cell l3"></span><span class="hm-cell l4"></span><span>More</span></div>
  </section>`;
}

function barsHTML(items, total) {
  if (!items.length) return `<div class="empty"><p class="empty-body">Nothing in this range.</p></div>`;
  const max = Math.max(...items.map((x) => x.tokens || 0), 1);
  return `<ol class="hbars" role="list">${items
    .map((x) => `<li class="hbar"><span class="hbar-name" title="${esc(x.name)}">${esc(x.name)}</span><span class="hbar-track" aria-hidden="true"><span class="hbar-fill" style="width:${((100 * (x.tokens || 0)) / max).toFixed(1)}%"></span></span><span class="hbar-val num">${esc(tokens(x.tokens))}<span class="muted"> · ${esc(percent(x.share))}</span></span></li>`)
    .join("")}</ol>`;
}

function modelsHTML(d) {
  const models = d.models || [];
  return `<section class="card" aria-labelledby="models-h"><div class="card-head"><h2 id="models-h">Models</h2><span class="card-sub">Share of tokens</span></div>${barsHTML(models)}</section>`;
}
function projectsHTML(d) {
  const p = d.projects || [];
  const n = d.project_count || p.length;
  const scope = d.projects_scope === "all" && range !== "all" ? " · all time" : "";
  return `<section class="card" aria-labelledby="proj-h"><div class="card-head"><h2 id="proj-h">Projects</h2><span class="card-sub">${n > p.length ? `Top ${p.length} of ${n}` : esc(count(n, "project", "projects"))}${scope}</span></div>${barsHTML(p)}</section>`;
}

/* ---------------- hover ---------------- */

let hovering = false;
function onHover(e) {
  const svg = e.target.closest && e.target.closest(".lc-wrap svg");
  if (!svg || !lm) return hideHover();
  const r = svg.getBoundingClientRect();
  const x = e.clientX - r.left;
  if (x < lm.x0 || x > lm.x1) return hideHover();
  hovering = true;
  const t = lm.start + ((x - lm.x0) / (lm.x1 - lm.x0)) * (lm.end - lm.start);
  const vals = linesAt(lm, t);
  const g = svg.querySelector(".rw-hover");
  const line = g.querySelector(".rw-cross");
  line.setAttribute("x1", x);
  line.setAttribute("x2", x);
  g.removeAttribute("hidden");
  const tip = svg.parentElement.querySelector(".rw-tip");
  tip.textContent = "";
  const near = vals.find((v) => v.at);
  const h = document.createElement("div");
  h.className = "tip-head";
  h.textContent = near ? dateShort(near.at[0]) + " " + clock(near.at[0]) : "";
  tip.appendChild(h);
  for (const v of vals) {
    if (!v.at) continue;
    const row = document.createElement("div");
    row.className = "tip-row";
    const k = document.createElement("span");
    k.className = "tip-key lc-key-" + v.key;
    const b = document.createElement("b");
    b.textContent = percent(v.at[1]);
    const s = document.createElement("span");
    s.textContent = v.name;
    row.append(k, b, s);
    tip.appendChild(row);
  }
  tip.hidden = false;
  const tw = tip.offsetWidth;
  let left = x + 12;
  if (left + tw > r.width - 4) left = x - 12 - tw;
  tip.style.left = Math.max(0, left) + "px";
  tip.style.top = lm.y0 + "px";
}
function hideHover() {
  if (!hovering || !root) return;
  hovering = false;
  const g = root.querySelector(".lc-wrap .rw-hover");
  if (g) g.setAttribute("hidden", "");
  const tip = root.querySelector(".lc-wrap .rw-tip");
  if (tip) tip.hidden = true;
}
