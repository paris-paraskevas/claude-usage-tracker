package com.claudeusage.tracker.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.claudeusage.tracker.Prefs
import com.claudeusage.tracker.RelayClient
import com.claudeusage.tracker.Snap
import com.claudeusage.tracker.Win
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

@Composable
fun DashboardScreen(onUnpair: () -> Unit) {
    val ctx = LocalContext.current
    val pairing = remember { Prefs.load(ctx) }
    val scope = rememberCoroutineScope()

    var snap by remember { mutableStateOf<Snap?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(true) }
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }

    val reload: suspend () -> Unit = reload@{
        val p = pairing ?: return@reload
        loading = true
        try {
            val js = RelayClient(p).fetchSnapshot()
            if (js == null) {
                error = null            // not synced yet — show the waiting state, not an error
            } else {
                snap = Snap.parse(js); error = null
            }
        } catch (e: Exception) {
            error = e.message ?: "Couldn't reach the relay"
        } finally {
            loading = false
        }
    }

    // Poll fast until the first snapshot lands (so it appears the moment the desktop pushes),
    // then settle to a calm cadence.
    LaunchedEffect(Unit) { while (true) { reload(); delay(if (snap == null) 5_000L else 20_000L) } }
    LaunchedEffect(Unit) { while (true) { delay(1_000); now = System.currentTimeMillis() } }

    Column(
        // Bg fills edge-to-edge and stays fixed; the safeDrawing inset is applied inside the
        // scroll so content clears the status/nav bars yet still scrolls under the (transparent)
        // status bar for a native Android-15 feel.
        Modifier.fillMaxSize().background(Bg).verticalScroll(rememberScrollState())
            .windowInsetsPadding(WindowInsets.safeDrawing).padding(16.dp)
    ) {
        // header
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Column(Modifier.weight(1f)) {
                Text("Usage Tracker for Claude", color = Ink, fontSize = 17.sp, fontWeight = FontWeight.SemiBold, maxLines = 1)
                Text(snap?.org?.ifBlank { "—" } ?: "connecting…", color = Faint, fontSize = 12.sp, fontFamily = FontFamily.Monospace)
            }
            snap?.statusWord?.let { StatusChip(it, hexColor(snap?.statusColor)) }
            IconButton(onClick = { scope.launch { reload() } }) {
                Icon(Icons.Filled.Refresh, contentDescription = "Refresh", tint = Dim)
            }
        }

        snap?.let { s ->
            s.verdictText?.takeIf { it.isNotBlank() }?.let { Spacer(Modifier.height(10.dp)); VerdictPill(it, hexColor(s.verdictColor)) }

            Spacer(Modifier.height(14.dp))
            Card {
                s.wins.forEach { w -> UsageRow(w, now); Spacer(Modifier.height(14.dp)) }
                ContextRow(s.ctxPct, s.ctxTokens)
            }

            if (s.sessions.isNotEmpty()) {
                Spacer(Modifier.height(14.dp))
                Card {
                    SectionTitle("SESSIONS · LAST 5H")
                    s.sessions.forEach { Spacer(Modifier.height(10.dp)); SessionRow(it.name, it.pct, it.active) }
                }
            }

            Spacer(Modifier.height(14.dp))
            Card {
                SectionTitle("ALL-TIME")
                Spacer(Modifier.height(6.dp))
                StatRow("Total tokens", s.atTokens?.let { Snap.fmtTokens(it) } ?: "—")
                StatRow("Sessions", s.atSessions?.toString() ?: "—")
                StatRow("Messages", s.atMessages?.let { String.format("%,d", it) } ?: "—")
                StatRow("Current streak", s.atStreak?.let { "${it}d" } ?: "—")
                StatRow("Peak hour", s.atPeak ?: "—")
                StatRow("Favorite model", s.favModel ?: "—")
            }

            Spacer(Modifier.height(8.dp))
            Text(if (s.ok) "live" else "stale", color = Faint, fontSize = 11.sp, fontFamily = FontFamily.Monospace)
        }

        if (snap == null) {
            if (error == null) {
                WaitingForSync()
            } else {
                Spacer(Modifier.height(48.dp))
                Text(error!!, color = hexColor("#d4694f"), fontSize = 14.sp,
                    textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth())
                Spacer(Modifier.height(12.dp))
                TextButton(onClick = { scope.launch { reload() } },
                    modifier = Modifier.align(Alignment.CenterHorizontally)) { Text("Try again", color = Accent) }
            }
        } else if (error != null) {
            Spacer(Modifier.height(6.dp))
            Text(error!!, color = hexColor("#cda24e"), fontSize = 11.sp, fontFamily = FontFamily.Monospace)
        }

        Spacer(Modifier.height(20.dp))
        TextButton(onClick = onUnpair) { Text("Unpair this device", color = Faint) }
    }
}

@Composable
private fun Card(content: @Composable () -> Unit) {
    Column(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(Panel).padding(16.dp)
    ) { content() }
}

/** Friendly first-sync state: the account exists but the desktop hasn't pushed a snapshot
 *  yet (it can take a few seconds). An indeterminate bar signals "working", not "stuck". */
@Composable
private fun WaitingForSync() {
    Column(
        Modifier.fillMaxWidth().padding(top = 64.dp, start = 8.dp, end = 8.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("Waiting for your desktop", color = Ink, fontSize = 17.sp, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(8.dp))
        Text(
            "Syncing for the first time. Keep the desktop app open with Remote (phone) " +
                "enabled — this usually takes a few seconds.",
            color = Dim, fontSize = 13.sp, textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(24.dp))
        LinearProgressIndicator(
            modifier = Modifier.fillMaxWidth(0.62f).height(5.dp).clip(RoundedCornerShape(3.dp)),
            color = Accent, trackColor = Panel2,
        )
    }
}

@Composable
private fun SectionTitle(t: String) =
    Text(t, color = Faint, fontSize = 11.sp, fontFamily = FontFamily.Monospace, letterSpacing = 1.sp)

@Composable
private fun UsageRow(w: Win, now: Long) {
    Column {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Text(w.label, color = Dim, fontSize = 12.sp, fontFamily = FontFamily.Monospace, modifier = Modifier.width(64.dp))
            Bar(w.pct, hexColor(w.color), Modifier.weight(1f))
            Text("${w.pct.toInt()}%", color = hexColor(w.color), fontSize = 16.sp,
                fontFamily = FontFamily.Monospace, fontWeight = FontWeight.SemiBold,
                modifier = Modifier.padding(start = 10.dp).width(52.dp))
        }
        Text(fmtCountdown(w.resetsAt, now), color = Faint, fontSize = 11.sp,
            fontFamily = FontFamily.Monospace, modifier = Modifier.padding(start = 64.dp, top = 3.dp))
    }
}

@Composable
private fun ContextRow(pct: Double?, tokens: Long?) {
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
        Text("Ctx", color = Dim, fontSize = 12.sp, fontFamily = FontFamily.Monospace, modifier = Modifier.width(64.dp))
        Bar(pct ?: 0.0, bandColor(pct ?: 0.0), Modifier.weight(1f))
        Text(if (pct != null) "${pct.toInt()}%" else "–", color = bandColor(pct ?: 0.0), fontSize = 16.sp,
            fontFamily = FontFamily.Monospace, fontWeight = FontWeight.SemiBold,
            modifier = Modifier.padding(start = 10.dp).width(52.dp))
    }
}

@Composable
private fun SessionRow(name: String, pct: Double, active: Boolean) {
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
        Box(Modifier.size(7.dp).clip(CircleShape).background(if (active) hexColor("#5e9e72") else Faint))
        Text(name, color = Ink, fontSize = 13.sp, modifier = Modifier.padding(start = 10.dp).width(120.dp), maxLines = 1)
        Bar(pct, bandColor(pct), Modifier.weight(1f))
        Text("${pct.toInt()}%", color = Dim, fontSize = 12.sp, fontFamily = FontFamily.Monospace,
            modifier = Modifier.padding(start = 10.dp).width(44.dp))
    }
}

@Composable
private fun StatRow(k: String, v: String) {
    Row(Modifier.fillMaxWidth().padding(vertical = 6.dp)) {
        Text(k, color = Dim, fontSize = 13.sp, modifier = Modifier.weight(1f))
        Text(v, color = Ink, fontSize = 14.sp, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Medium)
    }
}

@Composable
private fun Bar(pct: Double, color: Color, modifier: Modifier = Modifier) {
    Box(modifier.height(8.dp).clip(RoundedCornerShape(4.dp)).background(Panel2)) {
        Box(
            Modifier.fillMaxHeight()
                .fillMaxWidth((pct.coerceIn(0.0, 100.0) / 100.0).toFloat())
                .clip(RoundedCornerShape(4.dp))
                .background(color)
        )
    }
}

@Composable
private fun VerdictPill(text: String, color: Color) {
    Box(Modifier.clip(RoundedCornerShape(6.dp)).background(color.copy(alpha = 0.14f)).padding(horizontal = 10.dp, vertical = 6.dp)) {
        Text(text.uppercase(), color = color, fontSize = 11.sp, fontWeight = FontWeight.SemiBold, letterSpacing = 0.5.sp)
    }
}

@Composable
private fun StatusChip(word: String, color: Color) {
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(end = 4.dp)) {
        Box(Modifier.size(7.dp).clip(CircleShape).background(color))
        Text(word, color = color, fontSize = 12.sp, fontFamily = FontFamily.Monospace, modifier = Modifier.padding(start = 5.dp))
    }
}

private fun bandColor(p: Double): Color = when {
    p >= 80 -> hexColor("#d4694f")
    p >= 60 -> hexColor("#cda24e")
    else -> hexColor("#5e9e72")
}

private fun fmtCountdown(resetsAt: Long?, now: Long): String {
    if (resetsAt == null) return "—"
    var s = (resetsAt - now) / 1000
    if (s < 0) s = 0
    val d = s / 86400; val h = (s % 86400) / 3600; val m = (s % 3600) / 60
    return when {
        d > 0 -> "resets in ${d}d ${h}h"
        h > 0 -> "resets in ${h}h ${m}m"
        else -> "resets in ${m}m"
    }
}
