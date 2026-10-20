// Presentation rules shared by the dashboard, overlay and flyout. Pure: the
// engine computes every number and level; this only picks words for them.

import {
  percent, rate, duration, relIn, when, clock, limitNoun, fitWindow, currency, LEVEL_WORD,
} from "./format.js";

const fin = (v) => typeof v === "number" && Number.isFinite(v);
const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
const RANK = { limited: 3, will_hit: 2, watch: 1, ok: 0 };

export function limitById(state, id) {
  return ((state && state.limits) || []).find((l) => l.id === id) || null;
}

/** The limit the decision is about (falls back to the highest-ranked one). */
export function bindingLimit(state) {
  if (!state || !Array.isArray(state.limits) || !state.limits.length) return null;
  const d = state.decision || {};
  return (
    (d.binding && limitById(state, d.binding)) ||
    state.limits.find((l) => l.binding) ||
    [...state.limits].sort((a, b) => (RANK[b.level] || 0) - (RANK[a.level] || 0) || b.used_pct - a.used_pct)[0]
  );
}

export function isGathering(limit) {
  const p = (limit && limit.pace) || {};
  return p.confidence === "gathering" || !fin(p.rate_pct_h);
}

/** Tone for colour/icon/word: the level, or "gathering" for an ok limit with no forecast yet. */
export function toneOf(limit) {
  if (!limit) return "gathering";
  if (limit.level === "limited" || limit.used_pct >= 100) return "limited";
  if (limit.level === "ok" && isGathering(limit)) return "gathering";
  return limit.level || "ok";
}
export function levelWord(tone) {
  return LEVEL_WORD[tone] || LEVEL_WORD.ok;
}

export function isStale(state) {
  return !!(state && state.source && state.source.stale);
}

/** Why the data is stale, in the user's words. */
export function staleReason(state) {
  const src = (state && state.source) || {};
  if (src.client_offline) return "The tracker isn't responding";
  if (src.auth === "expired") return "Your Claude sign-in expired";
  if (src.auth === "missing") return "Not signed in to Claude Code";
  return "Can't reach Claude";
}

/** "22% an hour" (prose form of a rate). */
export function rateWords(v) {
  if (!fin(v)) return "an unknown rate";
  return rate(v).replace("%/h", "% an hour");
}

/** Short note for one limit in the overlay strip. */
export function stripNote(limit, now) {
  if (!limit) return "";
  const p = limit.pace || {};
  const tone = toneOf(limit);
  if (tone === "limited") return "back at " + when(limit.resets_at, now);
  if (tone === "will_hit" && fin(p.eta_100)) return "limit ~" + clock(p.eta_100);
  if (tone === "watch" && fin(p.projected_at_reset)) return "~" + percent(Math.min(p.projected_at_reset, 100)) + " by " + when(limit.resets_at, now);
  return "refills " + when(limit.resets_at, now);
}

/**
 * The dashboard verdict: { tone, title, detail } composed from the binding
 * limit, falling back to the engine's decision.title / detail.
 */
export function verdict(state, now) {
  const d = (state && state.decision) || {};
  const fallback = { tone: d.level || "ok", title: d.title || "", detail: d.detail || "" };
  if (!state) return fallback;
  const src = state.source || {};
  if (src.stale) {
    const at = src.updated_at;
    return {
      tone: "stale",
      title: "Usage data is out of date",
      detail: `${staleReason(state)}. Last reading ${fin(at) ? "at " + when(at, now) + " (" + duration(now - at, { style: "long" }) + " ago)" : "unknown"}; the numbers below are from then.`,
    };
  }
  const L = bindingLimit(state);
  if (!L) return fallback;
  const p = L.pace || {};
  const noun = limitNoun(L);
  const reset = L.resets_at;
  const tone = toneOf(L);

  if (tone === "limited") {
    const back = fin(d.resume_at) ? d.resume_at : reset;
    let detail = `Back at ${when(back, now)}.`;
    const o = state.overage;
    if (o && o.enabled) detail += ` Extra usage is on: ${currency(o.used, o.currency)} of ${currency(o.limit, o.currency)} used this month.`;
    else detail += " Extra usage is off, so Claude waits for the refill.";
    return { tone, title: `${cap(noun)} reached`, detail };
  }
  if (tone === "will_hit" && fin(p.eta_100)) {
    const gap = reset - p.eta_100;
    let detail = `That's ${duration(gap, { style: "long" })} before it refills at ${when(reset, now)}.`;
    if (fin(p.rate_pct_h)) {
      detail += ` You've used ${rateWords(p.rate_pct_h)} over the last ${fitWindow(L.id)}`;
      detail += fin(p.sustainable_pct_h) ? `; at ${rateWords(p.sustainable_pct_h)} you'd make it to the refill.` : ".";
    }
    return { tone, title: `You'll hit the ${noun} around ${clock(p.eta_100)}`, detail };
  }
  if (tone === "watch") {
    if (fin(p.projected_at_reset) && !isGathering(L)) {
      return {
        tone,
        title: `About ${percent(Math.min(p.projected_at_reset, 100))} of the ${noun} by ${when(reset, now)}`,
        detail: `Close to the limit at this pace: ${rateWords(p.rate_pct_h)} over the last ${fitWindow(L.id)}. It refills ${relIn(reset, now)}.`,
      };
    }
    return {
      tone,
      title: `${percent(L.used_pct)} of the ${noun} used`,
      detail: `Not enough recent data to forecast yet. It refills at ${when(reset, now)}, ${relIn(reset, now)}.`,
    };
  }
  if (tone === "gathering") {
    return {
      tone,
      title: "Gathering pace",
      detail: `${cap(noun)} ${percent(L.used_pct)} used, refills at ${when(reset, now)}. A forecast appears after about ${L.id === "five_hour" ? "10 minutes" : "an hour"} of use.`,
    };
  }
  // ok
  let detail = `${cap(noun)} ${percent(L.used_pct)} used`;
  if (fin(p.projected_at_reset)) detail += `, about ${percent(Math.min(p.projected_at_reset, 100))} by the refill at ${when(reset, now)}.`;
  else detail += `, refills at ${when(reset, now)}.`;
  return { tone: "ok", title: d.level === "ok" && d.title ? d.title : "On pace", detail };
}

/* ---------------- sessions ---------------- */

const SESSION_ORDER = { needs_approval: 0, waiting: 1, working: 2, idle: 3 };

/** Waiting / needs-approval first (longest waiting first), then working, then idle. */
export function sortSessions(list) {
  return [...(list || [])].sort((a, b) => {
    const ra = SESSION_ORDER[a.state] ?? 4, rb = SESSION_ORDER[b.state] ?? 4;
    if (ra !== rb) return ra - rb;
    if (ra <= 1) return (a.since || 0) - (b.since || 0);
    if (a.active !== b.active) return a.active ? -1 : 1;
    return (b.since || 0) - (a.since || 0);
  });
}
export function waitingSessions(list) {
  return sortSessions(list).filter((s) => s.state === "waiting" || s.state === "needs_approval");
}
/** The session whose context matters now: working + active first, then most recent active. */
export function activeSession(list) {
  const act = (list || []).filter((s) => s.active);
  if (!act.length) return null;
  return [...act].sort((a, b) => (a.state === "working" ? 0 : 1) - (b.state === "working" ? 0 : 1) || (b.since || 0) - (a.since || 0))[0];
}

/* ---------------- service ---------------- */

export function serviceInfo(service) {
  const s = service || {};
  switch (s.indicator) {
    case "none":
      return { tone: "svc-ok", text: "Claude is operating normally", short: "Claude OK" };
    case "minor":
      return { tone: "svc-minor", text: "Minor incident" + (s.description ? ": " + s.description : ""), short: "Incident" };
    case "major":
    case "critical":
      return { tone: "svc-major", text: (s.indicator === "critical" ? "Critical outage" : "Major outage") + (s.description ? ": " + s.description : ""), short: "Outage" };
    default:
      return { tone: "svc-unknown", text: "Claude status unknown", short: "Status ?" };
  }
}

/* ---------------- team ---------------- */

/** Best headroom first; accounts whose weekly limit is used up go last. */
export function sortPool(pool) {
  return [...(pool || [])].sort((a, b) => {
    const la = a.seven_day_pct >= 100 || a.level === "limited" ? 1 : 0;
    const lb = b.seven_day_pct >= 100 || b.level === "limited" ? 1 : 0;
    if (la !== lb) return la - lb;
    return (a.five_hour_pct ?? 999) - (b.five_hour_pct ?? 999) || (a.seven_day_pct ?? 999) - (b.seven_day_pct ?? 999);
  });
}
