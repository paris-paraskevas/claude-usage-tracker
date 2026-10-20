package com.claudeusage.tracker

import org.json.JSONArray
import org.json.JSONObject
import java.time.OffsetDateTime

/** How close a limit is to running out (V2-SPEC §3), most severe last. */
enum class Level(val wire: String) {
    OK("ok"), WATCH("watch"), WILL_HIT("will_hit"), LIMITED("limited");

    companion object {
        fun of(s: String?): Level? = entries.firstOrNull { it.wire == s }
    }
}

/** The pace of one limit. Epoch fields are **ms** here (the wire sends `eta_100` in seconds).
 *  [ratePctH] null = not enough samples yet ([confidence] "gathering"). [eta100] is only set
 *  when 100% falls before the refill. */
data class Pace(
    val ratePctH: Double?,
    val sustainablePctH: Double?,
    val projectedAtReset: Double?,
    val eta100: Long?,
    val confidence: String,
)

/** The one decision a surface leads with (V2-SPEC §3): the [binding] limit (a window key), its
 *  [level], and when everything comes back if a limit is reached ([resumeAt], epoch ms).
 *  [title]/[detail] are the desktop's own sentences; null when [fromDesktop] is false, i.e. the
 *  decision was derived on the phone from an older desktop's v1 fields. */
data class Decision(
    val level: Level,
    val binding: String?,
    val title: String?,
    val detail: String?,
    val resumeAt: Long?,
    val fromDesktop: Boolean,
)

/** A usage window. [key] is five_hour / seven_day (the main gauges) or the scoped weekly
 *  windows seven_day_opus / seven_day_sonnet. [resetsAt] is epoch ms. [ratePerHour] /
 *  [etaSeconds] are the v1 burn-rate projection (null = not enough samples; rate 0 = flat usage;
 *  eta relative to the snapshot's `updated_at`). [pace] / [level] / [elapsedPct] come from a v2
 *  desktop, or are derived from the v1 fields when it is older (see [Snap.Companion.derivePace]). */
data class Win(
    val key: String, val label: String, val pct: Double, val resetsAt: Long?, val color: String,
    val ratePerHour: Double?, val etaSeconds: Long?,
    val elapsedPct: Double? = null,
    val pace: Pace? = null,
    val level: Level = Level.OK,
)
data class Sess(val name: String, val pct: Double, val tokens: Long, val active: Boolean)
data class Msg(val role: String, val text: String, val ts: Long?)
data class Transcript(val name: String, val cwd: String?, val sessionId: String?, val active: Boolean, val messages: List<Msg>)
data class ExtraUsage(val used: Double?, val limit: Double?, val currency: String, val pct: Double?)
data class NearLimit(val name: String, val window: String, val pct: Double)

/** One account in the team pool (v0.3: the pool is made of Claude *accounts*, not people). */
data class PoolAccount(
    val name: String, val fhPct: Double?, val sdPct: Double?,
    val fhResetsAt: Long?, val sdResetsAt: Long?,
    val monthSpend: Double?, val monthTokens: Long?, val currency: String,
)
data class TeamOverview(
    /** Single-currency org total, or null when the pool mixes currencies (see [spendByCurrency]). */
    val orgSpend: Double?, val orgCurrency: String?,
    /** Month spend per currency — never summed across currencies. */
    val spendByCurrency: Map<String, Double>,
    val accountCount: Int, val tz: String,
    val near: List<NearLimit>, val accounts: List<PoolAccount>,
    /** "admin" / "member", or null when an older desktop doesn't say. */
    val role: String?,
)

/** Recent usage samples for the sparkline (epoch seconds + percents, oldest first). */
data class History(val t: LongArray, val fiveHour: DoubleArray, val sevenDay: DoubleArray)

/** A parsed, UI-ready view of the desktop snapshot. Parsing is defensive — any field
 *  may be missing on an early/stale snapshot. */
data class Snap(
    val ok: Boolean,
    /** The desktop's own fetch error (e.g. "Login expired"), present when [ok] is false. */
    val error: String?,
    /** "ok" / "expired" / "missing" — the desktop's Claude login state. */
    val tokenState: String?,
    val org: String,
    val email: String,
    val subscription: String,
    val updatedAt: Long,
    val verdictText: String?,
    val verdictColor: String?,
    val wins: List<Win>,
    val ctxPct: Double?,
    val ctxTokens: Long?,
    val sessions: List<Sess>,
    val statusWord: String?,
    val statusColor: String?,
    val statusDesc: String?,
    val atSessions: Long?,
    val atMessages: Long?,
    val atTokens: Long?,
    val atStreak: Int?,
    val atPeak: String?,
    val favModel: String?,
    val transcripts: List<Transcript>,
    /** The desktop has "mirror conversations to the phone" turned on. */
    val mirrorOn: Boolean,
    val extra: ExtraUsage?,
    /** Pool overview — sent to every signed-in team member (admins and members) since v0.3. */
    val teamOverview: TeamOverview?,
    /** `team.device_ok`: false = this pooled Claude account is in use on another device. */
    val deviceOk: Boolean?,
    val history: History?,
    /** The desktop's decision (v2 relay payload), else one derived from the windows. Null only
     *  when the snapshot carries no 5-hour or weekly window. */
    val decision: Decision? = null,
    /** `source.stale` when a v2 desktop sends it (its own reading is old or the fetch failed). */
    val sourceStale: Boolean? = null,
) {
    /** The limit the decision is about (falls back to the fullest main window). */
    val binding: Win? get() =
        decision?.binding?.let { b -> wins.firstOrNull { it.key == b } }
            ?: wins.filter { it.key in MAIN_KEYS }.maxByOrNull { it.pct }
    val fiveHour: Win? get() = wins.firstOrNull { it.key == "five_hour" }
    val sevenDay: Win? get() = wins.firstOrNull { it.key == "seven_day" }
    /** Scoped weekly windows (Opus / Sonnet), shown as small secondary rows. */
    val scoped: List<Win> get() = wins.filter { it.key == "seven_day_opus" || it.key == "seven_day_sonnet" }
    val loginExpired: Boolean get() = tokenState == "expired" || tokenState == "missing"

    companion object {
        private val WINDOW_KEYS = setOf("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")
        /** The limits that bind for every model; scoped weekly ones are informational on the phone. */
        private val MAIN_KEYS = setOf("five_hour", "seven_day")

        private fun JSONObject.optNullableDouble(key: String): Double? =
            if (!has(key) || isNull(key)) null else optDouble(key).let { if (it.isNaN()) null else it }

        private fun JSONObject.optNullableLong(key: String): Long? =
            if (!has(key) || isNull(key)) null else optDouble(key).let { if (it.isNaN()) null else it.toLong() }

        private fun JSONObject.optText(key: String): String? =
            if (!has(key) || isNull(key)) null else optString(key).takeIf { it.isNotBlank() && it != "null" }

        /** A reset time as epoch ms: accepts epoch ms, epoch seconds, or an ISO-8601 string
         *  (the pool rows carry `fh_resets_at` / `sd_resets_at` as ISO text). */
        private fun JSONObject.optInstantMs(key: String): Long? {
            if (!has(key) || isNull(key)) return null
            return when (val v = opt(key)) {
                is Number -> v.toLong().let { if (it in 1..99_999_999_999L) it * 1000 else it }
                is String -> runCatching { OffsetDateTime.parse(v).toInstant().toEpochMilli() }.getOrNull()
                else -> null
            }
        }

        fun parse(jsonStr: String): Snap {
            val o = JSONObject(jsonStr)
            val acc = o.optJSONObject("account")
            val verdict = o.optJSONObject("verdict")
            val ctx = o.optJSONObject("context")
            val ui = o.optJSONObject("ui")

            val updatedAt = o.optLong("updated_at", 0)
            val decisionJson = o.optJSONObject("decision")
            val wins = ArrayList<Win>()
            val wa = o.optJSONArray("windows") ?: JSONArray()
            for (i in 0 until wa.length()) {
                val w = wa.optJSONObject(i) ?: continue
                val key = w.optString("key")
                if (key !in WINDOW_KEYS) continue
                val pct = w.optDouble("pct", 0.0).let { if (it.isNaN()) 0.0 else it }
                val resetsAt = w.optInstantMs("resets_at")
                val rate = w.optNullableDouble("rate_per_hour")
                val eta = w.optNullableLong("eta_seconds")
                // v2 desktops add `pace`; older ones only have rate_per_hour / eta_seconds.
                val pace = w.optJSONObject("pace")?.let { parsePace(it) }
                    ?: derivePace(pct, resetsAt, rate, eta, updatedAt)
                // v1 desktops already send a `level`, but it is the old colour band
                // ("ok"/"warn"/"high"/"crit"/"max"); only trust it next to v2 fields.
                val v2 = w.has("pace") || decisionJson != null
                val level = (if (v2) Level.of(w.optText("level")) else null) ?: deriveLevel(pct, pace)
                wins.add(
                    Win(
                        key = key,
                        label = w.optText("label") ?: key,
                        pct = pct,
                        resetsAt = resetsAt,
                        color = w.optString("color", "#5e9e72"),
                        ratePerHour = rate,
                        etaSeconds = eta,
                        elapsedPct = w.optNullableDouble("elapsed_pct"),
                        pace = pace,
                        level = level,
                    )
                )
            }

            val sessions = ArrayList<Sess>()
            val sa = o.optJSONArray("sessions") ?: JSONArray()
            for (i in 0 until sa.length()) {
                val s = sa.optJSONObject(i) ?: continue
                sessions.add(
                    Sess(
                        name = s.optString("name", "?"),
                        pct = s.optDouble("context_pct", 0.0),
                        tokens = s.optLong("tokens", 0),
                        active = s.optBoolean("active", false),
                    )
                )
            }

            val watch = ArrayList<String>()
            ui?.optJSONArray("status_components")?.let { for (i in 0 until it.length()) watch.add(it.optString(i)) }
            val sv = statusView(o.optJSONObject("status"), watch)

            val at = o.optJSONObject("alltime")
            val period = at?.optJSONObject("periods")?.optJSONObject("all")

            // `transcripts` (list, user-pickable) is preferred; fall back to the single
            // `transcript` an older desktop may still send. Read-only mirror.
            val transcripts = ArrayList<Transcript>()
            o.optJSONArray("transcripts")?.let { arr ->
                for (i in 0 until arr.length()) arr.optJSONObject(i)?.let { transcripts.add(parseTranscript(it)) }
            }
            if (transcripts.isEmpty()) o.optJSONObject("transcript")?.let { transcripts.add(parseTranscript(it)) }

            // Extra-usage € (present only when the account has overage enabled).
            val ex = o.optJSONObject("extra")
            val extra = if (ex != null) ExtraUsage(
                used = ex.optNullableDouble("used"),
                limit = ex.optNullableDouble("limit"),
                currency = ex.optString("currency", "").let { if (it == "null") "" else it },
                pct = ex.optNullableDouble("pct"),
            ) else null

            return Snap(
                ok = o.optBoolean("ok", false),
                error = o.optText("error"),
                tokenState = o.optText("token_state"),
                org = acc?.optString("org").orEmptyAcct(acc),
                email = acc?.optText("email") ?: "",
                subscription = o.optText("subscription") ?: "",
                updatedAt = updatedAt,
                verdictText = verdict?.optText("text"),
                verdictColor = verdict?.optText("color"),
                wins = wins,
                ctxPct = ctx?.optNullableDouble("used_percentage"),
                ctxTokens = ctx?.optLong("total_input_tokens")?.takeIf { it > 0 },
                sessions = sessions,
                statusWord = sv?.first,
                statusColor = sv?.second,
                statusDesc = o.optJSONObject("status")?.optText("description"),
                atSessions = period?.optNullableLong("sessions"),
                atMessages = period?.optNullableLong("messages"),
                atTokens = period?.optNullableLong("tokens"),
                atStreak = at?.optNullableLong("streak_current")?.toInt(),
                atPeak = at?.optText("peak_hour"),
                favModel = period?.optText("fav_model"),
                transcripts = transcripts,
                mirrorOn = ui?.optBoolean("remote_transcript", false) ?: false,
                extra = extra,
                teamOverview = parseTeam(o),
                deviceOk = o.optJSONObject("team")?.let { t ->
                    if (!t.has("device_ok") || t.isNull("device_ok")) null else t.optBoolean("device_ok")
                },
                history = parseHistory(o.optJSONObject("history")),
                decision = decisionJson?.let { parseDecision(it, wins) } ?: deriveDecision(wins),
                sourceStale = o.optJSONObject("source")?.let { s ->
                    if (!s.has("stale") || s.isNull("stale")) null else s.optBoolean("stale")
                },
            )
        }

        private fun parsePace(p: JSONObject) = Pace(
            ratePctH = p.optNullableDouble("rate_pct_h"),
            sustainablePctH = p.optNullableDouble("sustainable_pct_h"),
            projectedAtReset = p.optNullableDouble("projected_at_reset"),
            eta100 = p.optInstantMs("eta_100"),
            confidence = p.optText("confidence") ?: if (p.isNull("rate_pct_h")) "gathering" else "ok",
        )

        /** The desktop's decision; an unknown level falls back to the derived one. */
        private fun parseDecision(d: JSONObject, wins: List<Win>): Decision? {
            val derived = deriveDecision(wins)
            val level = Level.of(d.optText("level")) ?: return derived
            return Decision(
                level = level,
                binding = d.optText("binding") ?: derived?.binding,
                title = d.optText("title"),
                detail = d.optText("detail"),
                resumeAt = d.optInstantMs("resume_at")
                    ?: if (level == Level.LIMITED) derived?.resumeAt else null,
                fromDesktop = true,
            )
        }

        /**
         * A v2-style pace from an older desktop's v1 fields, so the widget reads the same either
         * way. [etaSeconds] is relative to [updatedAtS] (the snapshot's build time). v1 only sets
         * an ETA for burns above ~5%/h, so a slower burn that still reaches 100% before the refill
         * gets its ETA computed here.
         */
        internal fun derivePace(
            pct: Double, resetsAtMs: Long?, ratePerHour: Double?, etaSeconds: Long?, updatedAtS: Long,
        ): Pace {
            val baseMs = if (updatedAtS > 0) updatedAtS * 1000 else System.currentTimeMillis()
            val hoursLeft = resetsAtMs?.let { (it - baseMs) / 3_600_000.0 }?.takeIf { it > 0 }
            val rate = ratePerHour?.coerceAtLeast(0.0)
            val projected = if (rate != null && hoursLeft != null) pct + rate * hoursLeft else null
            val eta = when {
                pct >= 100 -> null
                etaSeconds != null && etaSeconds > 0 -> baseMs + etaSeconds * 1000
                rate != null && rate > 0 && projected != null && projected >= 100 ->
                    baseMs + ((100 - pct) / rate * 3_600_000).toLong()
                else -> null
            }?.takeIf { resetsAtMs == null || it < resetsAtMs }
            return Pace(
                ratePctH = rate,
                sustainablePctH = if (hoursLeft != null && pct < 100) (100 - pct) / hoursLeft else null,
                projectedAtReset = projected,
                eta100 = eta,
                confidence = if (rate == null) "gathering" else "ok",
            )
        }

        /** V2-SPEC §3 level rules (without hysteresis: the phone only sees single snapshots). */
        internal fun deriveLevel(pct: Double, pace: Pace?): Level {
            val projected = pace?.projectedAtReset
            return when {
                pct >= 100 -> Level.LIMITED
                pace?.eta100 != null -> Level.WILL_HIT
                projected != null && projected >= 100 -> Level.WILL_HIT
                projected != null && projected >= 80 -> Level.WATCH
                pace?.ratePctH == null && pct >= 90 -> Level.WATCH
                else -> Level.OK
            }
        }

        /** V2-SPEC §3 binding rule over the 5-hour and weekly limits: highest level, then the
         *  earliest ETA, then the highest use. Resume = the latest refill among reached limits. */
        internal fun deriveDecision(wins: List<Win>): Decision? {
            val main = wins.filter { it.key in MAIN_KEYS }
            if (main.isEmpty()) return null
            val b = main.sortedWith(
                compareByDescending<Win> { it.level.ordinal }
                    .thenBy { it.pace?.eta100 ?: Long.MAX_VALUE }
                    .thenByDescending { it.pct },
            ).first()
            return Decision(
                level = b.level,
                binding = b.key,
                title = null,
                detail = null,
                resumeAt = main.filter { it.level == Level.LIMITED }.mapNotNull { it.resetsAt }.maxOrNull(),
                fromDesktop = false,
            )
        }

        /** The compact pool overview, sent to every signed-in teammate (admins and members) since
         *  v0.3. The viewer's role comes from `team_overview.role`, falling back to the snapshot's
         *  `team.role` (older desktops sent neither and only reached admins). Accepts both
         *  `members`/`member_count` (current) and `accounts`/`account_count`; the spend totals may
         *  sit at the top level (compact) or under `kpis` (merged). */
        private fun parseTeam(o: JSONObject): TeamOverview? {
            val to = o.optJSONObject("team_overview") ?: return null
            val nearArr = to.optJSONArray("near") ?: JSONArray()
            val near = ArrayList<NearLimit>()
            for (i in 0 until nearArr.length()) {
                val n = nearArr.optJSONObject(i) ?: continue
                near.add(NearLimit(n.optText("name") ?: "?", n.optText("window") ?: "", n.optNullableDouble("pct") ?: 0.0))
            }
            val accArr = to.optJSONArray("accounts") ?: to.optJSONArray("members") ?: JSONArray()
            val accounts = ArrayList<PoolAccount>()
            for (i in 0 until accArr.length()) {
                val a = accArr.optJSONObject(i) ?: continue
                // Rows may be flat (compact) or carry the usage under `account` (merged shape).
                val row = a.optJSONObject("account") ?: a
                accounts.add(PoolAccount(
                    name = a.optText("name") ?: "?",
                    fhPct = row.optNullableDouble("fh_pct"),
                    sdPct = row.optNullableDouble("sd_pct"),
                    fhResetsAt = row.optInstantMs("fh_resets_at"),
                    sdResetsAt = row.optInstantMs("sd_resets_at"),
                    monthSpend = a.optNullableDouble("month_spend"),
                    monthTokens = a.optNullableLong("month_tokens"),
                    currency = a.optText("currency") ?: "",
                ))
            }
            val count = to.optNullableLong("account_count")?.toInt()
                ?: to.optNullableLong("member_count")?.toInt()
                ?: accounts.size
            val k = to.optJSONObject("kpis")
            val byCur = LinkedHashMap<String, Double>()
            (to.optJSONObject("org_spend_by_currency") ?: k?.optJSONObject("org_spend_by_currency"))?.let { m ->
                val keys = m.keys()
                while (keys.hasNext()) {
                    val cur = keys.next()
                    m.optNullableDouble(cur)?.let { v -> byCur[cur] = v }
                }
            }
            return TeamOverview(
                orgSpend = to.optNullableDouble("org_spend") ?: k?.optNullableDouble("org_spend"),
                orgCurrency = to.optText("org_currency") ?: k?.optText("org_currency"),
                spendByCurrency = byCur,
                accountCount = count,
                tz = to.optText("tz") ?: "",
                near = near, accounts = accounts,
                role = to.optText("role") ?: o.optJSONObject("team")?.optText("role"),
            )
        }

        private fun parseHistory(h: JSONObject?): History? {
            if (h == null) return null
            val t = h.optJSONArray("t") ?: return null
            val f = h.optJSONArray("five_hour") ?: JSONArray()
            val w = h.optJSONArray("seven_day") ?: JSONArray()
            val n = minOf(t.length(), f.length(), w.length())
            if (n < 2) return null
            return History(
                t = LongArray(n) { t.optLong(it) },
                fiveHour = DoubleArray(n) { f.optDouble(it, 0.0) },
                sevenDay = DoubleArray(n) { w.optDouble(it, 0.0) },
            )
        }

        private fun parseTranscript(tj: JSONObject): Transcript {
            val ma = tj.optJSONArray("messages") ?: JSONArray()
            val ms = ArrayList<Msg>()
            for (i in 0 until ma.length()) {
                val mo = ma.optJSONObject(i) ?: continue
                ms.add(Msg(
                    role = mo.optString("role"),
                    text = mo.optString("text"),
                    ts = mo.optNullableDouble("ts")?.let { (it * 1000).toLong() },
                ))
            }
            return Transcript(
                name = tj.optString("name"),
                cwd = tj.optText("cwd"),
                sessionId = tj.optText("session_id"),
                active = tj.optBoolean("active", false),
                messages = ms,
            )
        }

        private fun String?.orEmptyAcct(acc: JSONObject?): String {
            if (!this.isNullOrBlank() && this != "null") return this
            val name = acc?.optString("name")
            if (!name.isNullOrBlank() && name != "null") return name
            val email = acc?.optString("email")
            return email?.takeIf { it != "null" }?.substringBefore("@") ?: ""
        }

        // Mirrors the dashboard's Ok / Errors / Down mapping.
        private val COMP_RANK = mapOf(
            "operational" to 0, "under_maintenance" to 1, "degraded_performance" to 2,
            "partial_outage" to 3, "major_outage" to 4,
        )
        private val IND_SEV = mapOf("none" to 0, "minor" to 2, "major" to 4, "critical" to 4)

        private fun sevColor(sev: Int) = if (sev <= 0) "#5e9e72" else if (sev >= 4) "#c94f38" else "#cda24e"
        private fun sevWord(sev: Int) = if (sev <= 0) "Ok" else if (sev >= 4) "Down" else "Errors"

        private fun statusView(status: JSONObject?, watch: List<String>): Pair<String, String>? {
            if (status == null) return null
            var sev = 0
            val comps = status.optJSONArray("components")
            if (watch.isNotEmpty() && comps != null) {
                for (i in 0 until comps.length()) {
                    val c = comps.optJSONObject(i) ?: continue
                    if (watch.contains(c.optString("name"))) {
                        sev = maxOf(sev, COMP_RANK[c.optString("status")] ?: 0)
                    }
                }
            } else {
                sev = IND_SEV[status.optString("indicator", "none")] ?: 0
            }
            return sevWord(sev) to sevColor(sev)
        }

        fun fmtTokens(n: Long): String = when {
            n >= 1_000_000_000 -> String.format("%.2fB", n / 1e9)
            n >= 1_000_000 -> String.format("%.1fM", n / 1e6)
            n >= 1_000 -> String.format("%.1fk", n / 1e3)
            else -> n.toString()
        }
    }
}
