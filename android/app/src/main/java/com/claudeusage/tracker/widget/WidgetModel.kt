package com.claudeusage.tracker.widget

import android.content.Context
import com.claudeusage.tracker.Level
import com.claudeusage.tracker.Prefs
import com.claudeusage.tracker.Snap
import com.claudeusage.tracker.Win
import com.claudeusage.tracker.ui.STALE_AFTER_S
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import kotlin.math.roundToInt

/** What the widget is showing overall. Every phase has words on screen, never colour alone. */
internal enum class Phase { NOT_PAIRED, LOADING, WAITING, LIVE, STALE, PROBLEM }

/** The icon + colour pair next to a status line. */
internal enum class Mark { OK, WATCH, WILL_HIT, LIMITED, OFFLINE, PROBLEM, INFO, PAIR, SYNC }

/** One limit as the widget draws it. All strings are final display text; times are absolute
 *  (widgets don't tick), and nothing future-tense survives a refill or stale data. */
internal data class MeterUi(
    val key: String,
    /** "5-hour" / "Weekly" / "Weekly · Opus". */
    val label: String,
    /** Short label for the 2×1 hero: "5-hour" / "Weekly" / "Opus" / "Sonnet". */
    val shortLabel: String,
    /** "64%", or "–" once the window has refilled and no new data has arrived. */
    val pctText: String,
    val progress: Float,
    val level: Level,
    /** Values are shown dimmed: stale data, a desktop problem, or the window already refilled. */
    val dim: Boolean,
    /** "Refills 16:40" / "Refills Thu 09:00" / "Refilled 16:40". */
    val refill: String?,
    /** Narrow pace text: "Limit ~16:26" / "~92% by 16:40" / "On pace" / "back 16:40". */
    val paceShort: String?,
    /** Wide pace text (4×2): "Limit ~16:26 at this pace" / "About 92% by 16:40" / … */
    val paceLong: String?,
    val mark: Mark,
    /** One merged TalkBack sentence for the whole meter group. */
    val spoken: String,
)

/** Everything one widget instance shows; [fingerprint] decides whether a redraw is needed. */
internal data class WidgetUi(
    val phase: Phase,
    val accountId: String?,
    /** 2×2 title: the account label when more than one account is paired, else "Claude". */
    val title: String,
    /** 4×2 title: "Claude" or "Claude · <account>". */
    val titleWide: String,
    /** 2×2 header time: "14:48" (live) / "as of 14:31" (stale or a desktop problem). */
    val updated: String?,
    /** 4×2 header time: "Updated 14:48" / "as of 14:31". */
    val updatedWide: String?,
    /** TalkBack text for the header ("Claude, updated 14:48"). */
    val headerSpoken: String,
    val five: MeterUi?,
    val week: MeterUi?,
    /** The binding limit — the 2×1 hero. */
    val hero: MeterUi?,
    /** 2×1 line under the hero bar: "Limit ~16:26" / "Refills 16:40" / "Offline · 14:31". */
    val heroLine: String?,
    val heroMark: Mark,
    /** TalkBack text for the whole 2×1. */
    val smallSpoken: String,
    /** 2×2 bottom line: the binding limit's pace, "back 16:40", or why the data is old. */
    val bottom: String?,
    val bottomMark: Mark,
    /** Why the numbers are old ("Desktop offline" / "Desktop signed out of Claude"); null when live. */
    val status: String?,
    val statusMark: Mark,
    /** A limit is reached and its refill is still ahead: solid "limited" surface. */
    val limited: Boolean,
    /** "5-hour limit reached". */
    val limitedTitle: String?,
    /** "back 16:40". */
    val backAt: String?,
    /** Non-data phases (not paired / loading / waiting): what to show instead of meters. */
    val message: String?,
    val messageDetail: String?,
    val messageMark: Mark,
) {
    fun fingerprint(): String = toString()
}

internal object WidgetModel {

    /** The account a widget shows: its configured account if still paired, else the app's
     *  active (primary) account. */
    fun resolveAccount(ctx: Context, chosen: String?): Prefs.Account? {
        val all = Prefs.all(ctx)
        return all.firstOrNull { it.pairing.accountId == chosen }
            ?: Prefs.activeId(ctx)?.let { id -> all.firstOrNull { it.pairing.accountId == id } }
    }

    /** Build the UI for one widget instance from the on-device cache (no network). */
    fun load(ctx: Context, chosenAccount: String?, now: Long): WidgetUi {
        val all = Prefs.all(ctx)
        val acct = resolveAccount(ctx, chosenAccount)
            ?: return build(null, paired = false, waiting = false, accountId = null, label = null, now = now)
        val id = acct.pairing.accountId
        val snap = WidgetData.loadSnap(ctx, id)?.let { runCatching { Snap.parse(it) }.getOrNull() }
        val label = if (all.size > 1) (acct.label ?: snap?.org?.ifBlank { null } ?: "Account") else null
        return build(snap, paired = true, waiting = WidgetData.isEmpty(ctx, id), accountId = id, label = label, now = now)
    }

    fun build(snap: Snap?, paired: Boolean, waiting: Boolean, accountId: String?, label: String?, now: Long): WidgetUi {
        val title = label ?: "Claude"
        val titleWide = if (label != null) "Claude · $label" else "Claude"
        if (!paired) {
            return idle(Phase.NOT_PAIRED, null, "Claude", "Claude", "Pair your desktop",
                "Open the app and scan the QR code on your computer", Mark.PAIR)
        }
        if (snap == null) {
            return if (waiting) {
                idle(Phase.WAITING, accountId, title, titleWide, "Waiting for your desktop",
                    "Open Usage Tracker on your computer", Mark.SYNC)
            } else {
                idle(Phase.LOADING, accountId, title, titleWide, "Loading usage…", null, Mark.SYNC)
            }
        }

        val problem = when {
            snap.loginExpired -> "Desktop signed out of Claude"
            !snap.ok -> "Desktop can't read usage"
            else -> null
        }
        val ageS = if (snap.updatedAt > 0) now / 1000 - snap.updatedAt else Long.MAX_VALUE
        val stale = ageS > STALE_AFTER_S || snap.sourceStale == true
        val phase = when {
            problem != null -> Phase.PROBLEM
            stale -> Phase.STALE
            else -> Phase.LIVE
        }
        val dimAll = phase != Phase.LIVE
        val asOf = if (snap.updatedAt > 0) fmtClock(snap.updatedAt * 1000, now) else null
        val asOfSpoken = if (snap.updatedAt > 0) fmtClock(snap.updatedAt * 1000, now, spoken = true) else "unknown"
        val status = when (phase) {
            Phase.PROBLEM -> problem
            Phase.STALE -> "Desktop offline"
            else -> null
        }
        val statusMark = if (phase == Phase.PROBLEM) Mark.PROBLEM else Mark.OFFLINE

        val five = snap.fiveHour?.let { meter(it, dimAll, now) }
        val week = snap.sevenDay?.let { meter(it, dimAll, now) }
        val bindingWin = snap.binding
        val hero = when (bindingWin?.key) {
            null -> null
            "five_hour" -> five
            "seven_day" -> week
            else -> meter(bindingWin, dimAll, now)
        }

        // Limited: a limit is at 100% and its refill is still ahead. True even when the desktop
        // has since gone quiet — a reached limit can't un-reach before its reset.
        val resumeAt = snap.decision?.resumeAt ?: bindingWin?.resetsAt
        val limited = snap.decision?.level == Level.LIMITED && (resumeAt == null || resumeAt > now)
        val backAt = resumeAt?.takeIf { limited && it > now }?.let { "back ${fmtClock(it, now)}" }
        val limitedTitle = if (limited) "${bindingWin?.label ?: "Usage"} limit reached" else null

        val (heroLine, heroMark) = when {
            phase == Phase.PROBLEM -> (if (snap.loginExpired) "Signed out" else "Desktop error") to Mark.PROBLEM
            phase == Phase.STALE -> "Offline · ${asOf ?: "–"}" to Mark.OFFLINE
            hero == null -> "No usage yet" to Mark.INFO
            hero.level == Level.WILL_HIT || hero.level == Level.WATCH -> hero.paceShort to hero.mark
            else -> (hero.refill ?: hero.paceShort) to hero.mark
        }
        val (bottom, bottomMark) = when {
            limited -> (backAt ?: "Limit reached") to Mark.LIMITED
            status != null -> status to statusMark
            hero == null -> "No usage yet" to Mark.INFO
            else -> hero.paceShort to hero.mark
        }
        val staleSpoken = status?.let { "$it, last update $asOfSpoken. " } ?: ""
        val smallSpoken = when {
            limited -> "$limitedTitle. " + (resumeAt?.let { "Back at ${fmtClock(it, now, spoken = true)}." } ?: "") +
                if (status != null) " $status." else ""
            hero != null -> staleSpoken + hero.spoken
            else -> staleSpoken + "No usage yet."
        }.trim()

        return WidgetUi(
            phase = phase, accountId = accountId, title = title, titleWide = titleWide,
            updated = asOf?.let { if (phase == Phase.LIVE) it else "as of $it" },
            updatedWide = asOf?.let { if (phase == Phase.LIVE) "Updated $it" else "as of $it" },
            headerSpoken = titleWide + ", " + (status?.let { "$it, last update $asOfSpoken" } ?: "updated $asOfSpoken"),
            five = five, week = week, hero = hero,
            heroLine = heroLine, heroMark = heroMark, smallSpoken = smallSpoken,
            bottom = bottom, bottomMark = bottomMark,
            status = status, statusMark = statusMark,
            limited = limited, limitedTitle = limitedTitle, backAt = backAt,
            message = null, messageDetail = null, messageMark = Mark.INFO,
        )
    }

    private fun idle(
        phase: Phase, accountId: String?, title: String, titleWide: String,
        message: String, detail: String?, mark: Mark,
    ) = WidgetUi(
        phase = phase, accountId = accountId, title = title, titleWide = titleWide,
        updated = null, updatedWide = null, headerSpoken = titleWide,
        five = null, week = null, hero = null,
        heroLine = message, heroMark = mark,
        smallSpoken = listOfNotNull(message, detail).joinToString(". "),
        bottom = null, bottomMark = mark, status = null, statusMark = mark,
        limited = false, limitedTitle = null, backAt = null,
        message = message, messageDetail = detail, messageMark = mark,
    )

    private fun shortLabel(key: String, label: String) = when (key) {
        "five_hour" -> "5-hour"
        "seven_day" -> "Weekly"
        "seven_day_opus" -> "Opus"
        "seven_day_sonnet" -> "Sonnet"
        else -> label
    }

    /** "64%": rounded, but never "100%" before the limit is actually reached. */
    private fun pctText(pct: Double): String {
        val r = pct.roundToInt()
        return "${if (pct < 100 && r >= 100) 99 else r.coerceAtLeast(0)}%"
    }

    fun meter(w: Win, dimAll: Boolean, now: Long): MeterUi {
        val reset = w.resetsAt
        val refilled = reset != null && reset <= now
        val dim = dimAll || refilled
        val t = { ms: Long -> fmtClock(ms, now) }
        val spokenT = { ms: Long -> fmtClock(ms, now, spoken = true) }
        val level = if (refilled) Level.OK else w.level
        val refill = when {
            reset == null -> null
            refilled -> "Refilled ${t(reset)}"
            else -> "Refills ${t(reset)}"
        }
        val eta = w.pace?.eta100?.takeIf { it > now }
        val projected = w.pace?.projectedAtReset?.roundToInt()
        val gathering = w.pace == null || w.pace.ratePctH == null

        var paceShort: String?
        var paceLong: String?
        var paceSpoken: String
        val mark: Mark
        when {
            refilled -> {
                paceShort = "Refilled"; paceLong = "Refilled · waiting for desktop"
                paceSpoken = "Refilled at ${spokenT(reset!!)}, waiting for new data"; mark = Mark.INFO
            }
            level == Level.LIMITED -> {
                paceShort = reset?.let { "back ${t(it)}" } ?: "Limit reached"
                paceLong = reset?.let { "Limit reached · back ${t(it)}" } ?: "Limit reached"
                paceSpoken = "Limit reached" + (reset?.let { ", back at ${spokenT(it)}" } ?: "")
                mark = Mark.LIMITED
            }
            level == Level.WILL_HIT -> {
                paceShort = eta?.let { "Limit ~${t(it)}" } ?: "Will hit limit"
                paceLong = eta?.let { "Limit ~${t(it)} at this pace" } ?: "Will hit the limit at this pace"
                paceSpoken = eta?.let { "Will hit the limit around ${spokenT(it)} at this pace" }
                    ?: "Will hit the limit at this pace"
                mark = Mark.WILL_HIT
            }
            level == Level.WATCH -> {
                val by = reset?.let { t(it) }
                paceShort = if (projected != null && by != null) "~$projected% by $by" else "Near limit"
                paceLong = if (projected != null && by != null) "About $projected% by $by" else "Close to the limit"
                paceSpoken = if (projected != null && reset != null) {
                    "Watch pace, about $projected percent by ${spokenT(reset)}"
                } else {
                    "Watch pace, close to the limit"
                }
                mark = Mark.WATCH
            }
            gathering -> {
                paceShort = "Gathering pace"; paceLong = "Gathering pace…"
                paceSpoken = "Measuring pace"; mark = Mark.OK
            }
            else -> {
                paceShort = "On pace"; paceLong = "On pace"; paceSpoken = "On pace"; mark = Mark.OK
            }
        }
        // Stale or problem data: no future-tense claims, only facts (used %, the refill time, and
        // a reached limit, which can't un-reach before its reset).
        val factsOnly = dimAll && !refilled && level != Level.LIMITED
        if (factsOnly) {
            paceShort = null; paceLong = null; paceSpoken = ""
        }
        val pctText = if (refilled) "–" else pctText(w.pct)
        val spoken = buildString {
            append(w.label).append(" limit, ")
            append(if (refilled) "refilled" else "${w.pct.roundToInt()} percent used")
            if (paceSpoken.isNotEmpty()) append(". ").append(paceSpoken)
            if (reset != null && !refilled) append(". Refills ").append(spokenT(reset))
            if (dimAll) append(". Last known value")
            append('.')
        }
        return MeterUi(
            key = w.key, label = w.label, shortLabel = shortLabel(w.key, w.label),
            pctText = pctText,
            progress = if (refilled) 0f else (w.pct / 100.0).coerceIn(0.0, 1.0).toFloat(),
            level = level, dim = dim, refill = refill,
            paceShort = paceShort, paceLong = paceLong, mark = if (factsOnly) Mark.OFFLINE else mark,
            spoken = spoken,
        )
    }
}

/** "14:30" today, else "Tue 09:00" — absolute, because widgets/notifications don't tick.
 *  [spoken] uses the full weekday ("Tuesday 09:00") for TalkBack. */
internal fun fmtClock(epochMs: Long, now: Long = System.currentTimeMillis(), spoken: Boolean = false): String {
    val today = Calendar.getInstance().apply { timeInMillis = now }
    val then = Calendar.getInstance().apply { timeInMillis = epochMs }
    val sameDay = today.get(Calendar.YEAR) == then.get(Calendar.YEAR) &&
        today.get(Calendar.DAY_OF_YEAR) == then.get(Calendar.DAY_OF_YEAR)
    val locale = Locale.getDefault()
    // "j" = the locale's preferred hour cycle (12h/24h).
    val skeleton = when {
        sameDay -> "jmm"
        spoken -> "EEEEjmm"
        else -> "EEEjmm"
    }
    val pattern = android.text.format.DateFormat.getBestDateTimePattern(locale, skeleton)
    return SimpleDateFormat(pattern, locale).format(Date(epochMs))
}
