// node --test tests/ui
import test from "node:test";
import assert from "node:assert/strict";
import * as f from "../../ctu/ui/assets/format.js";

const GB = { locale: "en-GB", timeZone: "Europe/Athens" };
const US = { locale: "en-US", timeZone: "Europe/Athens" };
const NOW = 1792496880; // Tue 20 Oct 2026 14:48 Athens (UTC+3)
const RESET_5H = 1792503600; // 16:40
const RESET_7D = 1792648800; // Thu 22 Oct 09:00

test("percent rounds, never shows 100% below the limit, marks tiny values", () => {
  assert.equal(f.percent(64, GB), "64%");
  assert.equal(f.percent(63.5, GB), "64%");
  assert.equal(f.percent(99.4, GB), "99%");
  assert.equal(f.percent(99.7, GB), "99%");
  assert.equal(f.percent(100, GB), "100%");
  assert.equal(f.percent(105.1, GB), "105%");
  assert.equal(f.percent(0, GB), "0%");
  assert.equal(f.percent(0.2, GB), "<1%");
  assert.equal(f.percent(null, GB), "—");
  assert.equal(f.percent(NaN, GB), "—");
  assert.equal(f.percent(64, { locale: "fr-FR" }).replace(/\s/g, " "), "64 %");
});

test("percentDigits gives the fixed-slot digits", () => {
  assert.equal(f.percentDigits(64), "64");
  assert.equal(f.percentDigits(4), "4");
  assert.equal(f.percentDigits(99.8), "99");
  assert.equal(f.percentDigits(0.3), "<1");
  assert.equal(f.percentDigits(undefined), "--");
});

test("rate uses whole numbers from 10 %/h and two significant digits below", () => {
  assert.equal(f.rate(22, GB), "22%/h");
  assert.equal(f.rate(19.3, GB), "19%/h");
  assert.equal(f.rate(5.43, GB), "5.4%/h");
  assert.equal(f.rate(0.25, GB), "0.25%/h");
  assert.equal(f.rate(0.9, GB), "0.9%/h");
  assert.equal(f.rate(null, GB), "—");
});

test("duration: short countdown and long sentence styles", () => {
  assert.equal(f.duration(6720, {}, GB), "1 h 52 m");
  assert.equal(f.duration(6720, { style: "long" }, GB), "1 h 52 min");
  assert.equal(f.duration(840, { style: "long" }, GB), "14 min");
  assert.equal(f.duration(840, {}, GB), "14 m");
  assert.equal(f.duration(3600, {}, GB), "1 h");
  assert.equal(f.duration(3629, {}, GB), "1 h"); // rounds to the minute
  assert.equal(f.duration(237600, {}, GB), "2 d 18 h");
  assert.equal(f.duration(2 * 86400, {}, GB), "2 d");
  assert.equal(f.duration(30, {}, GB), "<1 m");
  assert.equal(f.duration(30, { style: "long" }, GB), "under a minute");
  assert.equal(f.duration(-50, {}, GB), "<1 m");
  // seconds only under 10 minutes
  assert.equal(f.duration(545, { seconds: true }, GB), "9 m 05 s");
  assert.equal(f.duration(42, { seconds: true }, GB), "42 s");
  assert.equal(f.duration(600, { seconds: true }, GB), "10 m");
  assert.equal(f.duration(null), "—");
});

test("relIn / ago", () => {
  assert.equal(f.relIn(RESET_5H, NOW, {}, GB), "in 1 h 52 m");
  assert.equal(f.relIn(NOW - 5, NOW, {}, GB), "now");
  assert.equal(f.relIn(NOW + 840, NOW, { seconds: true }, GB), "in 14 m");
  assert.equal(f.relIn(NOW + 125, NOW, { seconds: true }, GB), "in 2 m 05 s");
  assert.equal(f.ago(NOW - 30, NOW, GB), "just now");
  assert.equal(f.ago(NOW - 240, NOW, GB), "4 min ago");
  assert.equal(f.ago(NOW - 2 * 3600, NOW, GB), "2 h ago");
  assert.equal(f.ago(NOW + 100, NOW, GB), "just now"); // clock skew never goes negative
});

test("clock follows the locale's hour cycle and the given zone", () => {
  assert.equal(f.clock(RESET_5H, GB), "16:40");
  assert.equal(f.clock(RESET_5H, US), "4:40 PM");
  assert.equal(f.clock(RESET_7D, GB), "09:00");
  assert.equal(f.clock(RESET_5H, { locale: "en-GB", timeZone: "America/New_York" }), "09:40");
  assert.equal(f.clock(RESET_5H, { locale: "en-GB", timeZone: "Asia/Kolkata" }), "19:10");
  assert.equal(f.weekdayClock(RESET_7D, GB), "Thu 09:00");
  assert.equal(f.clock(undefined, GB), "—");
});

test("when picks clock, weekday or date by distance in calendar days", () => {
  assert.equal(f.when(RESET_5H, NOW, GB), "16:40");
  assert.equal(f.when(RESET_7D, NOW, GB), "Thu 09:00");
  // ten days later is after the 25 Oct DST change: wall clock moves back an hour
  assert.equal(f.when(NOW + 10 * 86400, NOW, GB), "30 Oct, 13:48");
  // 23:30 the same evening vs 00:30 the next day (calendar day, not 24 h)
  const late = NOW + 8 * 3600 + 42 * 60; // 20 Oct 23:30 Athens
  assert.equal(f.when(late, NOW, GB), "23:30");
  assert.equal(f.when(late + 3600, NOW, GB), "Wed 00:30");
  assert.equal(f.asOf(NOW - 17 * 60, NOW, GB), "as of 14:31");
});

test("currency per code, with a fallback for unknown codes", () => {
  assert.equal(f.currency(12.4, "EUR", GB), "€12.40");
  assert.equal(f.currency(12.4, "USD", US), "$12.40");
  assert.equal(f.currency(1234, "JPY", { locale: "en-US" }), "¥1,234");
  assert.equal(f.currency(12.4, "EURO", GB), "12.4 EURO");
  assert.equal(f.currency(12.4, null, GB), "12.4");
  assert.equal(f.currency(undefined, "EUR", GB), "—");
});

test("plural rules come from Intl", () => {
  assert.equal(f.count(1, "session", "sessions", GB), "1 session");
  assert.equal(f.count(0, "session", "sessions", GB), "0 sessions");
  assert.equal(f.count(2, "session", "sessions", GB), "2 sessions");
  assert.equal(f.count(1234, "day", "days", GB), "1,234 days");
});

test("tokens are compact with a lowercase k", () => {
  assert.equal(f.tokens(950, GB), "950");
  assert.equal(f.tokens(146000, GB), "146k");
  assert.equal(f.tokens(18500, GB), "18.5k");
  assert.equal(f.tokens(1840000, GB), "1.8M");
  assert.equal(f.tokens(237300000, GB), "237M");
  assert.equal(f.tokens(2.1e9, GB), "2.1B");
});

test("months: labels, keys in a zone, and arithmetic", () => {
  assert.equal(f.monthLabel("2026-10", GB), "October 2026");
  assert.equal(f.addMonths("2026-01", -1), "2025-12");
  assert.equal(f.addMonths("2026-12", 1), "2027-01");
  assert.equal(f.addMonths("2026-10", -13), "2025-09");
  const t = Date.UTC(2026, 9, 31, 22, 30) / 1000; // 31 Oct 22:30 UTC
  assert.equal(f.monthKey(t, { timeZone: "UTC" }), "2026-10");
  assert.equal(f.monthKey(t, { timeZone: "Europe/Athens" }), "2026-11");
  assert.equal(f.dayKey(NOW, { timeZone: "Europe/Athens" }), "2026-10-20");
  assert.equal(f.monthName(NOW, GB), "October");
});

test("domain words", () => {
  assert.equal(f.prettyModel("claude-opus-5-5"), "Opus 5.5");
  assert.equal(f.prettyModel("claude-sonnet-4-5-20250929"), "Sonnet 4.5");
  assert.equal(f.prettyModel("claude-haiku-4-5"), "Haiku 4.5");
  assert.equal(f.prettyModel("gpt-x"), "gpt-x");
  assert.equal(f.limitNoun({ id: "five_hour" }), "5-hour limit");
  assert.equal(f.limitNoun({ id: "seven_day_opus" }), "weekly Opus limit");
  assert.equal(f.limitNoun({ id: "other", label: "Weekly · Haiku" }), "weekly Haiku limit");
  assert.equal(f.fitWindow("five_hour"), "30 min");
  assert.equal(f.fitWindow("seven_day"), "6 h");
  assert.equal(f.hourLabel(10, GB), "10:00");
  assert.equal(f.esc(`<a href="x">'&'</a>`), "&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;");
});

test("odd locale tags and unknown zones never throw", () => {
  assert.equal(f.safeLocale("en-US@posix"), "en-US");
  assert.equal(f.safeLocale("en_GB.UTF-8"), "en-GB");
  assert.equal(f.safeLocale("!!"), null);
  assert.equal(f.clock(RESET_5H, { locale: "en-GB@posix", timeZone: "Europe/Athens" }), "16:40");
  assert.equal(typeof f.clock(RESET_5H, { locale: "en-GB", timeZone: "Mars/Olympus" }), "string");
  assert.equal(f.percent(64, { locale: "not a locale" }).replace(/\s/g, ""), "64%");
});

test("clock follows the host's 12/24-hour setting over the locale", () => {
  const t = Date.UTC(2026, 9, 20, 16, 40) / 1000;
  assert.equal(f.clock(t, { locale: "en-US", timeZone: "UTC", hour12: false }), "16:40");
  assert.equal(f.clock(t, { locale: "en-GB", timeZone: "UTC", hour12: true }).replace(/\s/g, " ").toLowerCase(), "4:40 pm");
});
