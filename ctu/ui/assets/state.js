// Live state for every surface.
//  - Server-Sent Events on /api/v2/events (`state` events carry the full state).
//  - If SSE fails, poll GET /api/v2/state every 10 s until SSE recovers.
//  - subscribe(fn) -> fn(state, meta) on every new state and connection change.
//  - onTick(fn): one shared clock for countdowns (30 s, or 1 s while any
//    countdown is under 10 minutes); paused while the document is hidden.

const EVENTS_URL = "/api/v2/events";
const STATE_URL = "/api/v2/state";
const POLL_MS = 10000;
const SSE_RETRY_MS = 30000;

let state = null;
const meta = {
  conn: "connecting", // connecting | live | polling | offline
  lastOkAt: 0, // ms
  error: null,
  everLoaded: false,
};
const listeners = new Set();
const settingsListeners = new Set();
let es = null;
let pollTimer = null;
let sseRetryTimer = null;
let started = false;

export function now() {
  return Date.now() / 1000;
}
const OFFLINE_STALE_S = 15 * 60;
/**
 * The state as the UI should show it. When the tracker itself stops answering
 * it cannot mark its data stale, so after 15 minutes without contact the
 * last state is shown as stale here (source.client_offline = true).
 */
function effective() {
  if (!state) return null;
  const src = state.source || {};
  if (meta.conn === "offline" && !src.stale && now() - (src.updated_at || 0) > OFFLINE_STALE_S) {
    return { ...state, source: { ...src, stale: true, client_offline: true } };
  }
  return state;
}
export function getState() {
  return effective();
}
export function getMeta() {
  return { ...meta };
}

/** Subscribe to state; called immediately when a state is already known. */
export function subscribe(fn) {
  listeners.add(fn);
  if (state || meta.conn !== "connecting") safe(fn, effective(), getMeta());
  return () => listeners.delete(fn);
}
/** Optional `settings` SSE events (engine may emit them after a PATCH). */
export function subscribeSettings(fn) {
  settingsListeners.add(fn);
  return () => settingsListeners.delete(fn);
}

function safe(fn, ...args) {
  try {
    fn(...args);
  } catch (e) {
    console.error(e);
  }
}
let lastEffStale = false;
function notify() {
  const m = getMeta();
  const s = effective();
  lastEffStale = !!(s && s.source && s.source.client_offline);
  for (const fn of listeners) safe(fn, s, m);
}

function accept(next, mode) {
  if (!next || typeof next !== "object" || !("limits" in next)) return;
  const changed =
    !state || next.rev !== state.rev || next.generated_at !== state.generated_at || meta.conn !== mode;
  state = next;
  meta.conn = mode;
  meta.lastOkAt = Date.now();
  meta.error = null;
  meta.everLoaded = true;
  if (changed) notify();
}
function setConn(conn, error) {
  if (meta.conn === conn && meta.error === error) return;
  meta.conn = conn;
  meta.error = error || null;
  notify();
}

/** Start the connection (idempotent). */
export function start() {
  if (started) return;
  started = true;
  openSSE();
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && pollTimer) pollOnce();
  });
}

function openSSE() {
  if (typeof EventSource === "undefined") {
    startPolling();
    return;
  }
  try {
    es = new EventSource(EVENTS_URL);
  } catch {
    startPolling();
    return;
  }
  const onState = (ev) => {
    try {
      accept(JSON.parse(ev.data), "live");
      stopPolling();
    } catch (e) {
      console.error("bad state event", e);
    }
  };
  es.addEventListener("state", onState);
  es.onmessage = onState; // unnamed events, if the engine sends them
  es.addEventListener("settings", (ev) => {
    try {
      const s = JSON.parse(ev.data);
      for (const fn of settingsListeners) safe(fn, s);
    } catch {}
  });
  es.onerror = () => {
    // The browser retries on its own while CONNECTING; meanwhile poll.
    if (es && es.readyState === EventSource.CLOSED) {
      es.close();
      es = null;
      clearTimeout(sseRetryTimer);
      sseRetryTimer = setTimeout(openSSE, SSE_RETRY_MS);
    }
    startPolling();
  };
}

function startPolling() {
  if (pollTimer) return;
  pollOnce();
  pollTimer = setInterval(() => {
    if (!document.hidden) pollOnce();
  }, POLL_MS);
}
function stopPolling() {
  if (!pollTimer) return;
  clearInterval(pollTimer);
  pollTimer = null;
}

let polling = false;
async function pollOnce() {
  if (polling) return;
  polling = true;
  try {
    const res = await fetch(STATE_URL, { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    accept(await res.json(), es && es.readyState === EventSource.OPEN ? "live" : "polling");
  } catch (e) {
    setConn("offline", String(e && e.message ? e.message : e));
    const s = effective();
    if (!!(s && s.source && s.source.client_offline) !== lastEffStale) notify();
  } finally {
    polling = false;
  }
}

/** Fetch the state now (after an action, or the Retry button). */
export async function refetch() {
  await pollOnce();
}

/* ---------------- shared ticker ---------------- */

const tickers = new Set();
let tickTimer = null;

/** fn(nowSeconds) -> return a number of seconds until the nearest countdown
 *  (or nothing). The ticker runs every second while any countdown is < 600 s. */
export function onTick(fn) {
  tickers.add(fn);
  scheduleTick(0);
  return () => tickers.delete(fn);
}

function runTick() {
  tickTimer = null;
  if (typeof document !== "undefined" && document.hidden) return;
  const t = now();
  let nearest = Infinity;
  for (const fn of tickers) {
    try {
      const r = fn(t);
      if (typeof r === "number" && r < nearest) nearest = r;
    } catch (e) {
      console.error(e);
    }
  }
  if (nearest < 600) scheduleTick(1000 - (Date.now() % 1000) + 5);
  else scheduleTick(30000 - (Date.now() % 30000) + 5);
}
function scheduleTick(ms) {
  if (tickTimer) clearTimeout(tickTimer);
  tickTimer = setTimeout(runTick, ms);
}
if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (tickTimer) clearTimeout(tickTimer);
      tickTimer = null;
    } else scheduleTick(0);
  });
}
/** Re-run tick listeners now (after a re-render). */
export function tickNow() {
  scheduleTick(0);
}
