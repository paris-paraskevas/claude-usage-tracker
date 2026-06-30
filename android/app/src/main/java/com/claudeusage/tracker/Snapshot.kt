package com.claudeusage.tracker

import org.json.JSONArray
import org.json.JSONObject

data class Win(val key: String, val label: String, val pct: Double, val resetsAt: Long?, val color: String)
data class Sess(val name: String, val pct: Double, val tokens: Long, val active: Boolean)
data class Msg(val role: String, val text: String, val ts: Long?)
data class Transcript(val name: String, val cwd: String?, val active: Boolean, val messages: List<Msg>)

/** A parsed, UI-ready view of the desktop snapshot. Parsing is defensive — any field
 *  may be missing on an early/stale snapshot. */
data class Snap(
    val ok: Boolean,
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
    val transcript: Transcript?,    // the active one (transcripts.firstOrNull()) — kept for convenience
) {
    companion object {
        fun parse(jsonStr: String): Snap {
            val o = JSONObject(jsonStr)
            val acc = o.optJSONObject("account")
            val verdict = o.optJSONObject("verdict")
            val ctx = o.optJSONObject("context")
            val ui = o.optJSONObject("ui")

            val wins = ArrayList<Win>()
            val wa = o.optJSONArray("windows") ?: JSONArray()
            for (i in 0 until wa.length()) {
                val w = wa.getJSONObject(i)
                val key = w.optString("key")
                if (key != "five_hour" && key != "seven_day") continue
                wins.add(
                    Win(
                        key = key,
                        label = w.optString("label", key),
                        pct = w.optDouble("pct", 0.0),
                        resetsAt = if (w.isNull("resets_at")) null else w.optLong("resets_at"),
                        color = w.optString("color", "#5e9e72"),
                    )
                )
            }

            val sessions = ArrayList<Sess>()
            val sa = o.optJSONArray("sessions") ?: JSONArray()
            for (i in 0 until sa.length()) {
                val s = sa.getJSONObject(i)
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
            ui?.optJSONArray("status_components")?.let { for (i in 0 until it.length()) watch.add(it.getString(i)) }
            val sv = statusView(o.optJSONObject("status"), watch)

            val at = o.optJSONObject("alltime")
            val period = at?.optJSONObject("periods")?.optJSONObject("all")

            // `transcripts` (list, user-pickable) is preferred; fall back to the single
            // `transcript` an older desktop may still send.
            val transcripts = ArrayList<Transcript>()
            o.optJSONArray("transcripts")?.let { arr ->
                for (i in 0 until arr.length()) transcripts.add(parseTranscript(arr.getJSONObject(i)))
            }
            if (transcripts.isEmpty()) o.optJSONObject("transcript")?.let { transcripts.add(parseTranscript(it)) }

            return Snap(
                ok = o.optBoolean("ok", false),
                org = acc?.optString("org").orEmptyAcct(acc),
                email = acc?.optString("email")?.takeIf { it.isNotBlank() && it != "null" } ?: "",
                subscription = o.optString("subscription", ""),
                updatedAt = o.optLong("updated_at", 0),
                verdictText = verdict?.optString("text"),
                verdictColor = verdict?.optString("color"),
                wins = wins,
                ctxPct = ctx?.let { if (it.isNull("used_percentage")) null else it.optDouble("used_percentage") },
                ctxTokens = ctx?.optLong("total_input_tokens")?.takeIf { it > 0 },
                sessions = sessions,
                statusWord = sv?.first,
                statusColor = sv?.second,
                statusDesc = o.optJSONObject("status")?.optString("description")?.takeIf { it.isNotBlank() && it != "null" },
                atSessions = period?.optLong("sessions"),
                atMessages = period?.optLong("messages"),
                atTokens = period?.optLong("tokens"),
                atStreak = at?.optInt("streak_current"),
                atPeak = at?.optString("peak_hour")?.takeIf { it.isNotBlank() && it != "null" },
                favModel = period?.optString("fav_model")?.takeIf { it.isNotBlank() && it != "null" },
                transcripts = transcripts,
                transcript = transcripts.firstOrNull(),
            )
        }

        private fun parseTranscript(tj: JSONObject): Transcript {
            val ma = tj.optJSONArray("messages") ?: JSONArray()
            val ms = ArrayList<Msg>()
            for (i in 0 until ma.length()) {
                val mo = ma.getJSONObject(i)
                ms.add(Msg(
                    role = mo.optString("role"),
                    text = mo.optString("text"),
                    ts = if (mo.isNull("ts")) null else (mo.optDouble("ts") * 1000).toLong(),
                ))
            }
            return Transcript(
                name = tj.optString("name"),
                cwd = tj.optString("cwd").takeIf { it.isNotBlank() && it != "null" },
                active = tj.optBoolean("active", false),
                messages = ms,
            )
        }

        private fun String?.orEmptyAcct(acc: JSONObject?): String {
            if (!this.isNullOrBlank() && this != "null") return this
            val name = acc?.optString("name")
            if (!name.isNullOrBlank() && name != "null") return name
            val email = acc?.optString("email")
            return email?.substringBefore("@") ?: ""
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
                    val c = comps.getJSONObject(i)
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
