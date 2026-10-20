// Pure helpers for server.mjs — no SDK imports, so `node --test` can load them without
// installing dependencies (see mcp/test/).
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

export const DEFAULT_PORT = 8787;

// Mirrors the tracker's app_dir(): %LOCALAPPDATA%, else ~/AppData/Local on every OS.
export function portFile(env = process.env, home = homedir()) {
  const appDir = join(env.LOCALAPPDATA || join(home, "AppData", "Local"), "ClaudeUsageTracker");
  return join(appDir, "server_port");
}

function validPort(v) {
  if (typeof v === "number") return Number.isInteger(v) && v > 0 && v < 65536 ? v : null;
  if (typeof v !== "string") return null;
  const s = v.trim();
  // Blank, or the extension host's unsubstituted "${user_config.port}" placeholder, or junk.
  if (!/^\d{1,5}$/.test(s)) return null;
  return validPort(Number(s));
}

// The tracker's loopback port: $CUT_PORT if it's a real port number, else the port file the
// tracker writes on start, else the default. The .mcpb manifest always sets CUT_PORT, possibly
// to "" or a literal "${user_config.port}" when the user leaves the setting blank.
export function trackerPort(env = process.env, readFile = (p) => readFileSync(p, "utf-8")) {
  const fromEnv = validPort(env.CUT_PORT);
  if (fromEnv) return fromEnv;
  try {
    const p = validPort(readFile(portFile(env)));
    if (p) return p;
  } catch { /* fall through to default */ }
  return DEFAULT_PORT;
}

// YYYY-MM in the machine's local timezone (the tracker's ledger months are local, not UTC).
export function localMonth(d = new Date()) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

// The tracker's /api/team/* error codes -> an actionable message (null = no error).
export function teamErrorMessage(d) {
  const code = d && typeof d === "object" ? d.error : null;
  if (!code) return null;
  if (code === "not_signed_in") {
    return "You're not signed in to a team in the tracker. Open the tracker dashboard → Team tab and sign in with your work email.";
  }
  if (code === "bad_month") return "Month must be in YYYY-MM form, e.g. 2026-10.";
  if (code === "supabase unreachable") return "The tracker couldn't reach the team database just now. Try again in a moment.";
  return `Team data unavailable: ${code}`;
}

// Trim the big snapshot down to the numbers worth putting in a chat.
export function usageSummary(d) {
  const win = {};
  for (const w of d.windows || []) win[w.key] = { pct: w.pct, resets_at: w.resets_at, level: w.level };
  return {
    ok: d.ok, updated_at: d.updated_at,
    account: (d.account || {}).org || (d.account || {}).email || d.subscription || null,
    verdict: (d.verdict || {}).text || null,
    five_hour: win.five_hour || null,
    seven_day: win.seven_day || null,
    context: d.context ? { pct: d.context.used_percentage, tokens: d.context.total_input_tokens } : null,
    extra_usage: d.extra && d.extra.enabled
      ? { used: d.extra.used, limit: d.extra.limit, currency: d.extra.currency, pct: d.extra.pct } : null,
  };
}

// The ledger tool's output from the tracker's /api/team/ledger response
// ({month, accounts: {acct: name}, days, finals, computed_spend, month_tokens}).
export function ledgerSummary(d) {
  return { month: d.month, accounts: d.accounts || {}, spend: d.computed_spend || {}, tokens: d.month_tokens || {} };
}
