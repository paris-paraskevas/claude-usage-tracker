package com.claudeusage.tracker.ui

import com.claudeusage.tracker.Win
import java.util.Locale

/** A snapshot older than this reads as stale (amber + "stale" text). The desktop pushes every
 *  ~10 s and the app polls every 20 s, so ~60 s is the slowest normal cadence; 2× that without a
 *  new snapshot means the desktop is asleep, offline, or closed. */
const val STALE_AFTER_S = 120L

/** Compact duration: "2d 3h", "1h 10m", "12m", "<1m". */
fun fmtDuration(secs: Long): String {
    val s = secs.coerceAtLeast(0)
    val d = s / 86400; val h = (s % 86400) / 3600; val m = (s % 3600) / 60
    return when {
        d > 0 -> "${d}d ${h}h"
        h > 0 -> "${h}h ${m}m"
        m > 0 -> "${m}m"
        else -> "<1m"
    }
}

/** Spoken duration for TalkBack: "1 hour 10 minutes". */
fun spokenDuration(secs: Long): String {
    val s = secs.coerceAtLeast(0)
    val d = s / 86400; val h = (s % 86400) / 3600; val m = (s % 3600) / 60
    fun unit(n: Long, w: String) = "$n $w" + if (n == 1L) "" else "s"
    return when {
        d > 0 -> unit(d, "day") + if (h > 0) " " + unit(h, "hour") else ""
        h > 0 -> unit(h, "hour") + if (m > 0) " " + unit(m, "minute") else ""
        m > 0 -> unit(m, "minute")
        else -> "less than a minute"
    }
}

fun fmtCountdown(resetsAtMs: Long?, now: Long): String =
    if (resetsAtMs == null) "—" else "resets in " + fmtDuration((resetsAtMs - now) / 1000)

/** "5-hour window, 64 percent used, resets in 1 hour 10 minutes" */
fun spokenWindow(name: String, pct: Double?, resetsAtMs: Long?, now: Long): String = buildString {
    append(name).append(", ")
    append(if (pct != null) "${pct.toInt()} percent used" else "no data")
    if (resetsAtMs != null) append(", resets in ").append(spokenDuration((resetsAtMs - now) / 1000))
}

/** Seconds since the snapshot was produced (Long.MAX_VALUE if unknown). */
fun ageSeconds(updatedAtSec: Long, now: Long): Long =
    if (updatedAtSec <= 0) Long.MAX_VALUE else (now / 1000 - updatedAtSec).coerceAtLeast(0)

fun syncedAgo(updatedAtSec: Long, now: Long): String {
    if (updatedAtSec <= 0) return "not synced yet"
    val s = ageSeconds(updatedAtSec, now)
    return if (s < 5) "synced just now" else "synced ${ago(s)}"
}

/** "12s ago", "3 min ago", "2h ago", "4d ago". */
fun ago(s: Long): String = when {
    s < 60 -> "${s}s ago"
    s < 3600 -> "${s / 60} min ago"
    s < 86400 -> "${s / 3600}h ago"
    else -> "${s / 86400}d ago"
}

/** Burn-rate line mirroring the desktop dashboard: "≈7.2%/h · hits 100% in ~1h 10m".
 *  null rate = not enough samples (no line); rate ≤ 0 (flat usage) = "steady". */
fun burnLine(w: Win?): String? {
    if (w == null) return null
    val r = w.ratePerHour ?: return null
    val eta = w.etaSeconds
    val rate = String.format(Locale.US, "%.1f", r)
    return when {
        r > 0 && eta != null && eta <= 0 -> "≈$rate%/h · at the limit"
        r > 0 && eta != null -> "≈$rate%/h · hits 100% in ~${fmtDuration(eta)}"
        r > 0.1 -> "≈$rate%/h · resets before the limit"
        else -> "steady · no recent burn"
    }
}
