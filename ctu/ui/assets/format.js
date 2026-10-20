// Pure formatting helpers. No DOM, no clock: every function takes the values
// it needs (epoch seconds, `now`) plus optional { locale, timeZone }.
// Locale defaults to navigator.language; tests pass both explicitly.

const cache = new Map();
let defaults = { locale: undefined, timeZone: undefined, hour12: undefined };

/** Override defaults (used by the app once, and by tests). */
export function setFormatDefaults(o = {}) {
  defaults = { ...defaults, ...o };
  cache.clear();
}

// A BCP 47 tag Intl accepts ("en-US@posix" or "en_GB" become "en-US" / "en-GB").
export function safeLocale(tag) {
  const t = String(tag || "").split("@")[0].split(".")[0].replace(/_/g, "-");
  if (!t) return null;
  try {
    const c = Intl.getCanonicalLocales(t);
    return c.length ? c[0] : null;
  } catch {
    return null;
  }
}
let navLoc = null;
function navigatorLocale() {
  if (navLoc) return navLoc;
  const cands = typeof navigator !== "undefined" ? [...(navigator.languages || []), navigator.language] : [];
  for (const c of cands) {
    const ok = safeLocale(c);
    if (ok) return (navLoc = ok);
  }
  return (navLoc = "en-GB");
}
function loc(o) {
  if (o && o.locale) return safeLocale(o.locale) || navigatorLocale();
  if (defaults.locale) return safeLocale(defaults.locale) || navigatorLocale();
  return navigatorLocale();
}
function tz(o) {
  return (o && o.timeZone) || defaults.timeZone || undefined;
}
function fmt(kind, locale, options) {
  const key = kind + "|" + locale + "|" + JSON.stringify(options);
  let f = cache.get(key);
  if (!f) {
    const make = (l) =>
      kind === "n" ? new Intl.NumberFormat(l, options) : kind === "d" ? new Intl.DateTimeFormat(l, options) : new Intl.PluralRules(l, options);
    try {
      f = make(locale);
    } catch {
      // unknown time zone or locale: fall back rather than break the whole surface
      if (options && options.timeZone) {
        const rest = { ...options };
        delete rest.timeZone;
        return fmt(kind, locale, rest);
      }
      f = make("en-GB");
    }
    cache.set(key, f);
  }
  return f;
}
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
export const DASH = "—";

/* ---------------- numbers ---------------- */

/** Integer with grouping: 7737 -> "7,737". */
export function integer(n, o) {
  if (!isNum(n)) return DASH;
  return fmt("n", loc(o), { maximumFractionDigits: 0 }).format(Math.round(n));
}

/** Plain number with up to `max` decimals. */
export function number(n, max = 1, o) {
  if (!isNum(n)) return DASH;
  return fmt("n", loc(o), { maximumFractionDigits: max }).format(n);
}

/**
 * Usage percentage, rounded to a whole number. Never shows "100%" for a value
 * under 100 (99.6 -> "99%"), and never "0%" for a small non-zero value ("<1%").
 */
export function percentValue(v) {
  if (!isNum(v)) return null;
  if (v >= 100) return Math.round(v);
  if (v >= 99.5) return 99;
  if (v > 0 && v < 0.5) return 0.5; // signal "<1"
  return Math.max(0, Math.round(v));
}
export function percent(v, o) {
  const r = percentValue(v);
  if (r === null) return DASH;
  const f = fmt("n", loc(o), { style: "percent", maximumFractionDigits: 0 });
  if (r === 0.5) return "<" + f.format(0.01);
  return f.format(r / 100);
}
/** Just the digits of a percentage ("64"), for fixed-width slots. */
export function percentDigits(v) {
  const r = percentValue(v);
  if (r === null) return "--";
  if (r === 0.5) return "<1";
  return String(r);
}

/** Pace in percentage points per hour: 22 -> "22%/h", 0.25 -> "0.25%/h". */
export function rate(v, o) {
  if (!isNum(v)) return DASH;
  const opts = v >= 10 ? { maximumFractionDigits: 0 } : { maximumSignificantDigits: 2 };
  return fmt("n", loc(o), opts).format(v) + "%/h";
}

/** Token counts: 146000 -> "146k", 1840000 -> "1.8M", 950 -> "950". */
export function tokens(n, o) {
  if (!isNum(n)) return DASH;
  const a = Math.abs(n);
  const one = (x) => fmt("n", loc(o), { maximumFractionDigits: x >= 100 ? 0 : 1 }).format(x);
  if (a < 1000) return integer(n, o);
  if (a < 1e6) return one(n / 1e3) + "k";
  if (a < 1e9) return one(n / 1e6) + "M";
  return one(n / 1e9) + "B";
}

/** Money in the account's currency: (12.4, "EUR") -> "€12.40". */
export function currency(v, code, o) {
  if (!isNum(v)) return DASH;
  if (!code) return number(v, 2, o);
  try {
    return fmt("n", loc(o), { style: "currency", currency: code }).format(v);
  } catch {
    return number(v, 2, o) + " " + code;
  }
}

/** Plural-aware count: count(1, "session", "sessions") -> "1 session". */
export function count(n, one, other, o) {
  const rule = fmt("p", loc(o), {}).select(n);
  return integer(n, o) + " " + (rule === "one" ? one : other);
}
export function pluralWord(n, one, other, o) {
  return fmt("p", loc(o), {}).select(n) === "one" ? one : other;
}

/* ---------------- durations ---------------- */

/**
 * Duration from seconds.
 *  style "short" (countdowns): "2 d 18 h", "1 h 52 m", "14 m", "9 m 05 s", "42 s"
 *  style "long"  (sentences):  "2 d 18 h", "1 h 52 min", "14 min", "under a minute"
 * `seconds` lets callers ask for seconds below 10 minutes (ticking countdowns).
 */
export function duration(sec, { style = "short", seconds = false } = {}, o) {
  if (!isNum(sec)) return DASH;
  const n = (x) => integer(x, o);
  let s = Math.max(0, Math.round(sec));
  const minWord = style === "long" ? " min" : " m";
  if (seconds && s < 600) {
    if (s < 60) return n(s) + " s";
    const m = Math.floor(s / 60);
    const r = s % 60;
    return n(m) + " m " + String(r).padStart(2, "0") + " s";
  }
  if (s < 60) return style === "long" ? "under a minute" : "<1 m";
  let mins = Math.round(s / 60);
  if (mins < 60) return n(mins) + minWord;
  if (mins < 24 * 60) {
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    return m ? n(h) + " h " + n(m) + minWord : n(h) + " h";
  }
  let hours = Math.round(s / 3600);
  const d = Math.floor(hours / 24);
  const h = hours % 24;
  return h ? n(d) + " d " + n(h) + " h" : n(d) + " d";
}

/** "in 1 h 52 m" (or "now" when the moment has passed). */
export function relIn(epoch, now, opts = {}, o) {
  if (!isNum(epoch) || !isNum(now)) return DASH;
  const d = epoch - now;
  if (d <= 0) return "now";
  return "in " + duration(d, opts, o);
}

/** "just now", "4 min ago", "2 h ago", "3 d ago". */
export function ago(epoch, now, o) {
  if (!isNum(epoch) || !isNum(now)) return DASH;
  const d = Math.max(0, now - epoch);
  if (d < 45) return "just now";
  return duration(d, { style: "long" }, o) + " ago";
}

/* ---------------- clock & dates ---------------- */

/** Local calendar-day key "YYYY-MM-DD" for an epoch (in the given zone). */
export function dayKey(epoch, o) {
  if (!isNum(epoch)) return "";
  const p = fmt("d", "en-CA", { year: "numeric", month: "2-digit", day: "2-digit", timeZone: tz(o) })
    .formatToParts(new Date(epoch * 1000));
  const g = (t) => p.find((x) => x.type === t)?.value;
  return g("year") + "-" + g("month") + "-" + g("day");
}
function dayDiff(a, b, o) {
  // whole calendar days between the local dates of a and b (b - a)
  const ka = dayKey(a, o), kb = dayKey(b, o);
  const ta = Date.UTC(+ka.slice(0, 4), +ka.slice(5, 7) - 1, +ka.slice(8, 10));
  const tb = Date.UTC(+kb.slice(0, 4), +kb.slice(5, 7) - 1, +kb.slice(8, 10));
  return Math.round((tb - ta) / 86400000);
}

/** Wall-clock time: "16:40" (en-GB) / "4:40 PM" (en-US). */
export function clock(epoch, o) {
  if (!isNum(epoch)) return DASH;
  return fmt("d", loc(o), clockOptions(o)).format(new Date(epoch * 1000));
}
// 24-hour locales get a padded hour ("09:00"); 12-hour ones stay "9:00 AM".
// The host's own clock setting (Windows short-time format, via state.app.clock) wins over the
// locale, so an English UI on a 24-hour system still shows "16:40" like the taskbar clock.
function clockOptions(o) {
  const forced = o && typeof o.hour12 === "boolean" ? o.hour12 : defaults.hour12;
  let h24;
  if (typeof forced === "boolean") h24 = !forced;
  else {
    const l = loc(o);
    const key = "hc|" + l;
    let hc = cache.get(key);
    if (!hc) {
      hc = fmt("d", l, { hour: "numeric" }).resolvedOptions().hourCycle || "h23";
      cache.set(key, hc);
    }
    h24 = hc === "h23" || hc === "h24";
  }
  return { hour: h24 ? "2-digit" : "numeric", minute: "2-digit", hourCycle: h24 ? "h23" : "h12", timeZone: tz(o) };
}

/** Weekday + clock: "Thu 09:00". */
export function weekdayClock(epoch, o) {
  if (!isNum(epoch)) return DASH;
  const wd = fmt("d", loc(o), { weekday: "short", timeZone: tz(o) }).format(new Date(epoch * 1000));
  return wd + " " + clock(epoch, o);
}

/** Short date: "14 Oct" (en-GB) / "Oct 14" (en-US). */
export function dateShort(epoch, o) {
  if (!isNum(epoch)) return DASH;
  return fmt("d", loc(o), { day: "numeric", month: "short", timeZone: tz(o) }).format(new Date(epoch * 1000));
}

/** Weekday name only: "Thu". */
export function weekday(epoch, o) {
  if (!isNum(epoch)) return DASH;
  return fmt("d", loc(o), { weekday: "short", timeZone: tz(o) }).format(new Date(epoch * 1000));
}

/**
 * The clock for a moment near `now`: same day -> "16:40"; within 6 days ->
 * "Thu 09:00"; otherwise "24 Oct, 09:00".
 */
export function when(epoch, now, o) {
  if (!isNum(epoch)) return DASH;
  if (!isNum(now)) return clock(epoch, o);
  const dd = Math.abs(dayDiff(now, epoch, o));
  if (dd === 0) return clock(epoch, o);
  if (dd <= 6) return weekdayClock(epoch, o);
  return dateShort(epoch, o) + ", " + clock(epoch, o);
}

/** "as of 14:31". */
export function asOf(epoch, now, o) {
  return "as of " + when(epoch, now, o);
}

/** Full day label for a "YYYY-MM-DD" key: "Tue 14 Oct 2026". */
export function dayLabel(key, o) {
  if (!key || key.length < 10) return DASH;
  const d = new Date(Date.UTC(+key.slice(0, 4), +key.slice(5, 7) - 1, +key.slice(8, 10), 12));
  return fmt("d", loc(o), { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }).format(d);
}

/** Month label for "YYYY-MM": "October 2026". */
export function monthLabel(ym, o) {
  if (!ym || ym.length < 7) return DASH;
  const d = new Date(Date.UTC(+ym.slice(0, 4), +ym.slice(5, 7) - 1, 15));
  return fmt("d", loc(o), { month: "long", year: "numeric", timeZone: "UTC" }).format(d);
}
/** Month name for an epoch: "October". */
export function monthName(epoch, o) {
  if (!isNum(epoch)) return DASH;
  return fmt("d", loc(o), { month: "long", timeZone: tz(o) }).format(new Date(epoch * 1000));
}
/** Short month for "YYYY-MM-DD" key: "Oct". */
export function monthShortOfKey(key, o) {
  const d = new Date(Date.UTC(+key.slice(0, 4), +key.slice(5, 7) - 1, 15));
  return fmt("d", loc(o), { month: "short", timeZone: "UTC" }).format(d);
}
/** Weekday short of a "YYYY-MM-DD" key. */
export function weekdayOfKey(key, o) {
  const d = new Date(Date.UTC(+key.slice(0, 4), +key.slice(5, 7) - 1, +key.slice(8, 10), 12));
  return fmt("d", loc(o), { weekday: "short", timeZone: "UTC" }).format(d);
}
/** "YYYY-MM" for an epoch in a zone (team ledger months). */
export function monthKey(epoch, o) {
  return dayKey(epoch, o).slice(0, 7);
}
/** Shift a "YYYY-MM" key by n months. */
export function addMonths(ym, n) {
  let y = +ym.slice(0, 4), m = +ym.slice(5, 7) - 1 + n;
  y += Math.floor(m / 12);
  m = ((m % 12) + 12) % 12;
  return y + "-" + String(m + 1).padStart(2, "0");
}

/** Hour of day as a clock label: 10 -> "10:00" / "10 AM". */
export function hourLabel(h, o) {
  if (!isNum(h)) return DASH;
  const d = new Date(Date.UTC(2026, 0, 5, h, 0));
  return fmt("d", loc(o), { hour: "numeric", minute: "2-digit", timeZone: "UTC" }).format(d);
}

/* ---------------- domain words ---------------- */

const LIMIT_NOUN = {
  five_hour: "5-hour limit",
  seven_day: "weekly limit",
  seven_day_opus: "weekly Opus limit",
  seven_day_sonnet: "weekly Sonnet limit",
};
/** Sentence noun for a limit: "5-hour limit", "weekly Opus limit". */
export function limitNoun(limit) {
  if (!limit) return "limit";
  if (LIMIT_NOUN[limit.id]) return LIMIT_NOUN[limit.id];
  const l = String(limit.label || limit.id || "").replace(/·/g, "").replace(/\s+/g, " ").trim();
  return (l ? l.charAt(0).toLowerCase() + l.slice(1) : "usage") + " limit";
}
/** Short label for a limit: "5-hour", "Weekly", "Weekly · Opus". */
export function limitLabel(limit) {
  if (!limit) return "";
  return limit.label || limit.id;
}
/** The trailing window the engine fits its pace over (spec §3). */
export function fitWindow(limitId) {
  return limitId === "five_hour" ? "30 min" : "6 h";
}
/** Compact overlay key: "5h" / "7d" / "Opus" / "Sonnet". */
export function limitKey(limit) {
  if (!limit) return "";
  if (limit.id === "five_hour") return "5h";
  if (limit.id === "seven_day") return "7d";
  if (limit.scope === "opus") return "Opus";
  if (limit.scope === "sonnet") return "Sonnet";
  return limit.label || limit.id;
}

/** "claude-opus-5-5" -> "Opus 5.5"; "claude-sonnet-4-5-20250929" -> "Sonnet 4.5". */
export function prettyModel(id) {
  if (!id) return "";
  const s = String(id).toLowerCase();
  for (const fam of ["opus", "sonnet", "haiku"]) {
    const i = s.indexOf(fam);
    if (i >= 0) {
      const nums = s.slice(i + fam.length).replace(/_/g, "-").split("-").filter((c) => /^\d{1,2}$/.test(c));
      return (fam.charAt(0).toUpperCase() + fam.slice(1) + " " + nums.slice(0, 2).join(".")).trim();
    }
  }
  return String(id).replace(/^claude-/, "");
}

/** Words for each level (always paired with an icon). */
export const LEVEL_WORD = {
  ok: "On pace",
  watch: "Watch pace",
  will_hit: "Will hit limit",
  limited: "Limit reached",
  stale: "Out of date",
  gathering: "Gathering pace",
};

/** Session state words. */
export const SESSION_WORD = {
  working: "Working",
  waiting: "Waiting for you",
  needs_approval: "Needs approval",
  idle: "Idle",
};

/** Escape text for safe insertion into HTML strings. */
export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
