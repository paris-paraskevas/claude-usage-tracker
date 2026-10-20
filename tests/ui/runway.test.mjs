// node --test tests/ui
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { setFormatDefaults } from "../../ctu/ui/assets/format.js";
import { runwayModel, renderRunway, axisTicks, valueAt, boxHitsSegment, PAD } from "../../ctu/ui/assets/runway.js";

setFormatDefaults({ locale: "en-GB", timeZone: "Europe/Athens" });

const fx = (n) => JSON.parse(readFileSync(new URL(`./fixtures/${n}`, import.meta.url), "utf8"));
const STATE = fx("state-will_hit.json");
const HIST = fx("history-five_hour.json");
const NOW = STATE.generated_at; // 14:48
const five = (s = STATE) => structuredClone(s.limits.find((l) => l.id === "five_hour"));
const ATHENS = () => 180; // minutes east of UTC (EEST)
const W = 800, H = 200;
const close = (a, b, eps = 0.01) => assert.ok(Math.abs(a - b) <= eps, `${a} ≈ ${b}`);

test("eta before the reset: projection to the limit, band from eta to reset", () => {
  const L = five();
  const m = runwayModel({ limit: L, history: HIST, now: NOW, width: W, height: H, off: ATHENS });
  assert.equal(m.mode, "eta");
  assert.equal(m.start, L.resets_at - 18000);
  assert.equal(m.end, L.resets_at);
  // x maps the window linearly onto the plot
  close(m.X(m.start), PAD.l);
  close(m.X(m.end), W - PAD.r);
  close(m.Y(0), H - PAD.b);
  close(m.Y(100), PAD.t);
  // now point at 62.7 % of the window, 64 % used
  close(m.nowPt.x, PAD.l + ((NOW - m.start) / 18000) * (W - PAD.r - PAD.l));
  close(m.nowPt.y, m.Y(64));
  // projection ends on the limit line at eta_100
  close(m.projection.x2, m.X(L.pace.eta_100));
  close(m.projection.y2, m.y0);
  // the band is the 14 minutes between eta and refill
  assert.equal(m.dry.seconds, 14 * 60);
  close(m.dry.x + m.dry.w, m.x1);
  // readouts: runway 1 h 38 m vs refill 1 h 52 m
  assert.equal(m.runwaySec, 98 * 60);
  assert.equal(m.refillSec, 112 * 60);
  // even pace diagonal corner to corner
  assert.deepEqual([m.evenPace.x1, m.evenPace.y1, m.evenPace.x2, m.evenPace.y2], [m.x0, m.y1, m.x1, m.y0]);
  // sustainable line (will_hit) reaches ~100 % at the refill
  assert.ok(m.sustainable);
  close(m.sustainable.x2, m.x1);
  assert.ok(Math.abs(m.sustainable.y2 - m.y0) < 1.5);
  // the used line ends at the live value, not the last sample
  const last = m.usedPts[m.usedPts.length - 1];
  close(last[0], m.nowPt.x);
  close(last[1], m.nowPt.y);
});

test("no eta (on pace): projection ends at the reset at projected_at_reset", () => {
  const L = structuredClone(fx("state-ok.json").limits[0]);
  const m = runwayModel({ limit: L, history: [], now: NOW, width: W, height: H, off: ATHENS });
  assert.equal(m.mode, "on_pace");
  assert.equal(m.dry, null);
  assert.equal(m.runwaySec, null);
  assert.equal(m.sustainable, null); // only shown for watch / will_hit
  close(m.projection.x2, m.x1);
  close(m.projection.y2, m.Y(49.2));
  assert.equal(m.projEnd, 49.2);
});

test("an eta at or after the reset is not a runway: treated as on pace, clamped at 100", () => {
  const L = five();
  L.pace.eta_100 = L.resets_at + 600;
  L.pace.projected_at_reset = 104;
  const m = runwayModel({ limit: L, history: HIST, now: NOW, width: W, height: H, off: ATHENS });
  assert.equal(m.mode, "on_pace");
  close(m.projection.y2, m.y0);
});

test("projection without projected_at_reset falls back to rate × hours", () => {
  const L = five();
  L.level = "ok";
  L.pace = { rate_pct_h: 6, sustainable_pct_h: 20, projected_at_reset: null, eta_100: null, confidence: "ok" };
  const m = runwayModel({ limit: L, history: [], now: NOW, width: W, height: H, off: ATHENS });
  close(m.projEnd, 64 + 6 * (6720 / 3600), 1e-9);
});

test("limited: flat at 100 from the first sample at the limit, countdown to refill", () => {
  const s = fx("state-limited.json");
  const L = structuredClone(s.limits[0]);
  const start = L.resets_at - L.window_s;
  const hist = [[start + 600, 20], [start + 9000, 80], [NOW - 1020, 100], [NOW - 300, 100]];
  const m = runwayModel({ limit: L, history: hist, now: NOW, width: W, height: H, off: ATHENS });
  assert.equal(m.mode, "limited");
  assert.equal(m.runwaySec, 0);
  assert.equal(m.refillSec, 14 * 60);
  close(m.flat.x1, m.X(NOW - 1020));
  close(m.flat.y, m.y0);
  assert.equal(m.dry.t0, NOW - 1020);
  assert.equal(m.projection, null);
  assert.equal(m.sustainable, null);
});

test("gathering: no projection, no band, no sustainable line", () => {
  const L = structuredClone(fx("state-gathering.json").limits[0]);
  const m = runwayModel({ limit: L, history: [[L.resets_at - 18000 + 60, 0], [NOW - 60, 3.8]], now: NOW, width: W, height: H, off: ATHENS });
  assert.equal(m.mode, "gathering");
  assert.equal(m.projection, null);
  assert.equal(m.dry, null);
  assert.equal(m.sustainable, null);
  assert.equal(m.runwaySec, null);
  assert.equal(m.usedPts.length, 3);
});

test("samples outside the window are ignored; geometry does not depend on the zone", () => {
  const L = five();
  const noisy = [[L.resets_at - 40000, 90], ...HIST, [NOW + 3600, 99]];
  const a = runwayModel({ limit: L, history: noisy, now: NOW, width: W, height: H, off: () => 180 });
  const b = runwayModel({ limit: L, history: HIST, now: NOW, width: W, height: H, off: () => -420 });
  assert.deepEqual(a.usedPts, b.usedPts);
  assert.deepEqual(a.projection, b.projection);
  assert.deepEqual(a.dry, b.dry);
  assert.deepEqual(a.nowPt, b.nowPt);
});

test("hour ticks land on local hours in any offset, including half-hour zones", () => {
  const L = five();
  for (const off of [180, 0, -300, 330, 345]) {
    const m = runwayModel({ limit: L, history: [], now: NOW, width: 1000, height: H, off: () => off });
    assert.ok(m.ticks.length >= 3, `ticks for ${off}`);
    for (const t of m.ticks) {
      assert.equal((t.t + off * 60) % 3600, 0, `offset ${off}: ${t.t}`);
      assert.ok(t.t > m.start && t.t < m.end);
      assert.ok(t.x <= m.x1 - 96, "keeps clear of the refill label");
    }
  }
});

test("weekly ticks land on local midnights and keep 52 px apart", () => {
  const L = structuredClone(STATE.limits.find((l) => l.id === "seven_day"));
  const start = L.resets_at - L.window_s, end = L.resets_at;
  const X = (t) => 44 + ((t - start) / (end - start)) * 700;
  const ticks = axisTicks(start, end, X, { off: () => 180, x0: 44, x1: 744, reserveRight: 96 });
  assert.ok(ticks.length >= 4);
  for (const t of ticks) assert.equal((t.t + 10800) % 86400, 0);
  for (let i = 1; i < ticks.length; i++) assert.ok(ticks[i].x - ticks[i - 1].x >= 52);
  // narrow chart: steps widen instead of overlapping
  const Xn = (t) => 44 + ((t - start) / (end - start)) * 200;
  const tn = axisTicks(start, end, Xn, { off: () => 180, x0: 44, x1: 244, reserveRight: 96 });
  for (let i = 1; i < tn.length; i++) assert.ok(tn[i].x - tn[i - 1].x >= 52);
});

test("valueAt reads samples in the past and the projection in the future", () => {
  const L = five();
  const m = runwayModel({ limit: L, history: HIST, now: NOW, width: W, height: H, off: ATHENS });
  const past = valueAt(m, m.start + 3600);
  assert.equal(past.past, true);
  assert.ok(past.used >= 0 && past.used <= 64);
  const fut = valueAt(m, (NOW + L.pace.eta_100) / 2);
  assert.equal(fut.past, false);
  close(fut.projected, 82, 0.01);
  close(valueAt(m, L.pace.eta_100).projected, 100);
  close(valueAt(m, m.end).even, 100);
});

test("label collision helper", () => {
  const box = { l: 10, r: 50, t: 10, b: 20 };
  assert.equal(boxHitsSegment(box, [0, 0, 60, 30]), true);
  assert.equal(boxHitsSegment(box, [0, 40, 60, 40]), false);
  assert.equal(boxHitsSegment(box, [30, 0, 30, 5]), false);
  assert.equal(boxHitsSegment(box, [30, 0, 30, 9], 2), true);
});

test("render: labels and stale rendering", () => {
  const L = five();
  const m = runwayModel({ limit: L, history: HIST, now: NOW, width: W, height: 220, off: ATHENS });
  const svg = renderRunway(m);
  assert.match(svg, /^<svg class="rw rw--eta tone-will_hit"/);
  assert.match(svg, /Limit ~16:26/);
  assert.match(svg, /14 min at limit/);
  assert.match(svg, /Refill 16:40/);
  assert.match(svg, /Now 64%/);
  assert.match(svg, /role="img" aria-label="5-hour limit window from 11:40 to 16:40\. 64% used by 14:48\./);
  // stale: what was measured only
  const ms = runwayModel({ limit: L, history: HIST, now: NOW - 1020, width: W, height: 220, off: ATHENS });
  const stale = renderRunway(ms, { stale: true, asOfText: "As of 14:31" });
  assert.match(stale, /is-stale/);
  assert.doesNotMatch(stale, /class="rw-proj"/);
  assert.doesNotMatch(stale, /at limit/);
  assert.doesNotMatch(stale, /class="rw-sust"/);
  assert.match(stale, /As of 14:31 · /);
  assert.match(stale, /no forecast/);
});
