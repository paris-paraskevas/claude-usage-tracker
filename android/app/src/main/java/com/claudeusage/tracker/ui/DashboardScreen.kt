package com.claudeusage.tracker.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
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
import androidx.compose.material.icons.automirrored.filled.ViewList
import androidx.compose.material.icons.filled.Forum
import androidx.compose.material.icons.filled.Insights
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Speed
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.appwidget.updateAll
import com.claudeusage.tracker.Pairing
import com.claudeusage.tracker.Prefs
import com.claudeusage.tracker.RelayClient
import com.claudeusage.tracker.Msg
import com.claudeusage.tracker.Sess
import com.claudeusage.tracker.Snap
import com.claudeusage.tracker.Win
import com.claudeusage.tracker.widget.UsageWidget
import com.claudeusage.tracker.widget.WidgetData
import com.claudeusage.tracker.widget.updateLockNotification
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private val MONO = FontFamily.Monospace

@Composable
fun DashboardScreen(onUnpair: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()

    var accounts by remember { mutableStateOf(Prefs.all(ctx)) }
    var activeId by remember { mutableStateOf(Prefs.activeId(ctx)) }
    var snap by remember { mutableStateOf<Snap?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    var tab by rememberSaveable { mutableStateOf(0) }
    var ctxSel by rememberSaveable { mutableStateOf<String?>(null) }   // which session drives the Context gauge

    val reload: suspend () -> Unit = reload@{
        val p = Prefs.active(ctx) ?: return@reload          // always the currently-active account
        try {
            val js = RelayClient(p).fetchSnapshot()
            if (js == null) {
                error = null                                // not synced yet — show the waiting state
            } else {
                val parsed = Snap.parse(js)
                snap = parsed; error = null
                // learn this account's label from its org so the switcher reads nicely
                val cur = Prefs.all(ctx).firstOrNull { it.pairing.accountId == p.accountId }
                if (cur?.label.isNullOrBlank() && parsed.org.isNotBlank()) {
                    Prefs.setLabel(ctx, p.accountId, parsed.org); accounts = Prefs.all(ctx)
                }
                WidgetData.saveSnap(ctx, js)                // keep the home-screen widget + lock-screen notif current
                runCatching { UsageWidget().updateAll(ctx) }
                updateLockNotification(ctx)
            }
        } catch (e: Exception) {
            error = e.message ?: "Couldn't reach the relay"
        }
    }

    val scanLauncher = rememberLauncherForActivityResult(ScanContract()) { result ->
        result.contents?.let { Pairing.parse(it.trim()) }?.let { p ->
            Prefs.add(ctx, p); accounts = Prefs.all(ctx); activeId = p.accountId
            snap = null; error = null
            scope.launch { reload() }
            registerFcmToken(ctx)                           // let the new desktop push this phone too
        }
    }
    val addAccount: () -> Unit = {
        scanLauncher.launch(
            ScanOptions().setOrientationLocked(true).setBeepEnabled(false)
                .setPrompt("Scan the pairing QR from another desktop"),
        )
    }
    val switchTo: (String) -> Unit = { id ->
        if (id != activeId) {
            Prefs.setActive(ctx, id); activeId = id; snap = null; error = null
            scope.launch { reload() }
        }
    }
    val removeAccount: (String) -> Unit = { id ->
        Prefs.remove(ctx, id); accounts = Prefs.all(ctx); activeId = Prefs.activeId(ctx)
        if (accounts.isEmpty()) onUnpair() else { snap = null; error = null; scope.launch { reload() } }
    }

    // Poll fast until the first snapshot lands, then settle. 1s ticker drives countdowns.
    LaunchedEffect(Unit) { while (true) { reload(); delay(if (snap == null) 5_000L else 20_000L) } }
    LaunchedEffect(Unit) { while (true) { delay(1_000); now = System.currentTimeMillis() } }

    val s = snap
    if (s == null) {
        Box(
            Modifier.fillMaxSize().background(Bg).windowInsetsPadding(WindowInsets.safeDrawing).padding(24.dp),
            contentAlignment = Alignment.Center,
        ) {
            if (error == null) WaitingForSync()
            else ErrorState(error!!) { scope.launch { reload() } }
        }
        return
    }

    Scaffold(
        containerColor = Bg,
        bottomBar = { BottomNav(tab) { tab = it } },
    ) { inner ->
        when (tab) {
            0 -> OverviewPage(s, now, inner, accounts, activeId, switchTo, addAccount,
                ctxSel, onCtxSel = { ctxSel = it }) { scope.launch { reload() } }
            1 -> SessionsPage(s, inner)
            2 -> ChatPage(s, inner)
            3 -> StatsPage(s, inner)
            else -> SettingsPage(s, Prefs.active(ctx), now, inner, accounts, activeId,
                switchTo, addAccount, removeAccount) { scope.launch { reload() } }
        }
    }
}

// ---- pages ----------------------------------------------------------------

@Composable
private fun OverviewPage(
    s: Snap, now: Long, inner: PaddingValues,
    accounts: List<Prefs.Account>, activeId: String?, onSwitch: (String) -> Unit, onAddAccount: () -> Unit,
    ctxSel: String?, onCtxSel: (String?) -> Unit, onRefresh: () -> Unit,
) {
    PageScroll(inner) {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            AccountSwitcher(s, accounts, activeId, onSwitch, onAddAccount, Modifier.weight(1f))
            s.statusWord?.let { StatusChip(it, hexColor(s.statusColor)) }
            IconButton(onClick = onRefresh) { Icon(Icons.Filled.Refresh, "Refresh", tint = Dim) }
        }

        s.verdictText?.takeIf { it.isNotBlank() }?.let {
            Spacer(Modifier.height(16.dp)); VerdictBanner(it, hexColor(s.verdictColor))
        }

        val five = s.wins.firstOrNull { it.key == "five_hour" }
        val week = s.wins.firstOrNull { it.key == "seven_day" }

        Spacer(Modifier.height(28.dp))
        Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
            GaugeStat(
                label = "5-hour limit",
                pct = five?.pct,
                color = hexColor(five?.color, usageColor(five?.pct ?: 0.0)),
                sub = five?.let { fmtCountdown(it.resetsAt, now) },
                dim = 188.dp, big = true,
            )
        }
        Spacer(Modifier.height(28.dp))
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly) {
            GaugeStat("Weekly", week?.pct, hexColor(week?.color, usageColor(week?.pct ?: 0.0)),
                week?.let { fmtCountdown(it.resetsAt, now) }, 128.dp, false)
            ContextGauge(s, ctxSel, onCtxSel)
        }

        Spacer(Modifier.height(22.dp))
        Text(syncedAgo(s.updatedAt, now), color = Faint, fontSize = 11.sp, fontFamily = MONO,
            modifier = Modifier.fillMaxWidth(), textAlign = TextAlign.Center)
    }
}

/** Header title that doubles as the account switcher: tap to switch desktops or add one. */
@Composable
private fun AccountSwitcher(
    s: Snap, accounts: List<Prefs.Account>, activeId: String?,
    onSwitch: (String) -> Unit, onAddAccount: () -> Unit, modifier: Modifier = Modifier,
) {
    var open by remember { mutableStateOf(false) }
    val name = accounts.firstOrNull { it.pairing.accountId == activeId }?.label?.ifBlank { null }
        ?: s.org.ifBlank { null } ?: "Claude"
    val multi = accounts.size > 1
    Box(modifier) {
        Column(Modifier.clip(RoundedCornerShape(8.dp)).clickable { open = true }) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(name, color = Ink, fontSize = 22.sp, fontWeight = FontWeight.Bold, maxLines = 1)
                Text("  ▾", color = Faint, fontSize = 15.sp)
            }
            Text(
                if (multi) "${s.subscription.ifBlank { "plan" }} · ${accounts.size} accounts"
                else s.subscription.ifBlank { "your plan" } + " plan",
                color = Faint, fontSize = 12.sp, fontFamily = MONO,
            )
        }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            accounts.forEach { acc ->
                val lbl = acc.label?.ifBlank { null } ?: acc.pairing.accountId.take(8)
                DropdownMenuItem(
                    text = { Text(lbl, color = if (acc.pairing.accountId == activeId) Accent else Ink) },
                    onClick = { open = false; onSwitch(acc.pairing.accountId) },
                )
            }
            DropdownMenuItem(
                text = { Text("+ Add account", color = Accent) },
                onClick = { open = false; onAddAccount() },
            )
        }
    }
}

@Composable
private fun SessionsPage(s: Snap, inner: PaddingValues) {
    PageScroll(inner) {
        PageTitle("Sessions", "last 5 hours")
        Spacer(Modifier.height(16.dp))
        if (s.sessions.isEmpty()) {
            Text("No active Claude Code sessions in the last 5 hours.", color = Dim, fontSize = 14.sp)
        } else {
            s.sessions.forEach { SessionCard(it); Spacer(Modifier.height(12.dp)) }
        }
    }
}

@Composable
private fun ChatPage(s: Snap, inner: PaddingValues) {
    PageScroll(inner) {
        val t = s.transcript
        PageTitle("Conversation", t?.name)
        Spacer(Modifier.height(16.dp))
        if (t == null || t.messages.isEmpty()) {
            Text(
                "No conversation mirrored yet.\n\nOn the desktop: Settings → Remote (phone) → turn on " +
                    "\"mirror the active conversation.\" Your latest session then appears here — " +
                    "text only, end-to-end encrypted.",
                color = Dim, fontSize = 14.sp,
            )
        } else {
            t.messages.forEach { MsgBubble(it); Spacer(Modifier.height(10.dp)) }
        }
    }
}

@Composable
private fun MsgBubble(m: Msg) {
    val user = m.role == "user"
    Row(Modifier.fillMaxWidth(), horizontalArrangement = if (user) Arrangement.End else Arrangement.Start) {
        Column(
            Modifier.fillMaxWidth(0.88f).clip(RoundedCornerShape(12.dp))
                .background(if (user) Accent.copy(alpha = 0.16f) else Panel).padding(12.dp),
        ) {
            Text(if (user) "You" else "Claude", color = if (user) Accent else Dim,
                fontSize = 11.sp, fontWeight = FontWeight.SemiBold, fontFamily = MONO)
            Spacer(Modifier.height(4.dp))
            Text(m.text, color = Ink, fontSize = 14.sp)
        }
    }
}

@Composable
private fun StatsPage(s: Snap, inner: PaddingValues) {
    PageScroll(inner) {
        PageTitle("All-time", null)
        Spacer(Modifier.height(20.dp))
        Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
            Text(s.atTokens?.let { Snap.fmtTokens(it) } ?: "—", color = Accent,
                fontSize = 46.sp, fontWeight = FontWeight.Bold, fontFamily = MONO)
            Text("TOTAL TOKENS", color = Faint, fontSize = 12.sp, letterSpacing = 1.5.sp)
        }
        Spacer(Modifier.height(24.dp))
        val tiles = listOf(
            "Sessions" to (s.atSessions?.toString() ?: "—"),
            "Messages" to (s.atMessages?.let { String.format("%,d", it) } ?: "—"),
            "Current streak" to (s.atStreak?.let { "${it}d" } ?: "—"),
            "Peak hour" to (s.atPeak ?: "—"),
            "Favorite model" to (s.favModel ?: "—"),
        )
        tiles.chunked(2).forEach { row ->
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                row.forEach { (l, v) -> StatTile(l, v, Modifier.weight(1f)) }
                if (row.size == 1) Spacer(Modifier.weight(1f))
            }
            Spacer(Modifier.height(12.dp))
        }
    }
}

@Composable
private fun SettingsPage(
    s: Snap, pairing: Pairing?, now: Long, inner: PaddingValues,
    accounts: List<Prefs.Account>, activeId: String?,
    onSwitch: (String) -> Unit, onAddAccount: () -> Unit, onRemove: (String) -> Unit,
    onRefresh: () -> Unit,
) {
    val ctx = LocalContext.current
    var lockOn by remember { mutableStateOf(WidgetData.lockscreen(ctx)) }
    PageScroll(inner) {
        PageTitle("Settings", null)
        Spacer(Modifier.height(16.dp))
        Card {
            SectionLabel("ACCOUNTS")
            Spacer(Modifier.height(4.dp))
            accounts.forEach { acc ->
                val id = acc.pairing.accountId
                val lbl = acc.label?.ifBlank { null } ?: id.take(8)
                Row(
                    Modifier.fillMaxWidth().clip(RoundedCornerShape(8.dp))
                        .clickable { onSwitch(id) }.padding(vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Box(Modifier.size(8.dp).clip(CircleShape)
                        .background(if (id == activeId) hexColor("#5e9e72") else Faint))
                    Text(lbl, color = if (id == activeId) Ink else Dim, fontSize = 14.sp, maxLines = 1,
                        modifier = Modifier.padding(start = 10.dp).weight(1f))
                    TextButton(onClick = { onRemove(id) }) {
                        Text("Remove", color = hexColor("#d4694f"), fontSize = 12.sp)
                    }
                }
            }
            TextButton(onClick = onAddAccount) { Text("+ Add account", color = Accent) }
        }
        Spacer(Modifier.height(14.dp))
        Card {
            SectionLabel("ACCOUNT")
            Spacer(Modifier.height(4.dp))
            KV("Organization", s.org.ifBlank { "—" })
            KV("Email", s.email.ifBlank { "—" })
            KV("Plan", s.subscription.ifBlank { "—" })
        }
        Spacer(Modifier.height(14.dp))
        Card {
            SectionLabel("SYNC")
            Spacer(Modifier.height(4.dp))
            KV("State", if (s.ok) "live" else "stale")
            KV("Last synced", syncedAgo(s.updatedAt, now))
            KV("Relay", pairing?.url?.substringAfter("://")?.substringBefore("/") ?: "—")
            s.statusWord?.let { KV("Anthropic", s.statusDesc ?: it) }
        }
        Spacer(Modifier.height(14.dp))
        Card {
            SectionLabel("LOCK SCREEN")
            Spacer(Modifier.height(8.dp))
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("Show usage on the lock screen", color = Ink, fontSize = 14.sp)
                    Text("Ongoing notification · 5h / weekly / context", color = Faint, fontSize = 12.sp)
                }
                Switch(
                    checked = lockOn,
                    onCheckedChange = {
                        lockOn = it
                        WidgetData.setLockscreen(ctx, it)
                        updateLockNotification(ctx)
                    },
                    colors = SwitchDefaults.colors(checkedTrackColor = Accent, checkedThumbColor = Ink),
                )
            }
        }
        Spacer(Modifier.height(18.dp))
        TextButton(onClick = onRefresh) { Text("Refresh now", color = Accent) }
        Spacer(Modifier.height(18.dp))
        Text(
            "Unofficial · not affiliated with Anthropic. \"Claude\" and \"Claude Code\" " +
                "are trademarks of Anthropic.",
            color = Faint, fontSize = 11.sp, textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth(),
        )
    }
}

// ---- shared layout --------------------------------------------------------

/** A page body: insets for the system bars + bottom nav, scrolls, with consistent padding. */
@Composable
private fun PageScroll(inner: PaddingValues, content: @Composable androidx.compose.foundation.layout.ColumnScope.() -> Unit) {
    Column(
        Modifier.fillMaxSize().padding(inner).verticalScroll(rememberScrollState())
            .padding(horizontal = 20.dp).padding(top = 12.dp, bottom = 28.dp),
        content = content,
    )
}

@Composable
private fun PageTitle(title: String, sub: String?) {
    Text(title, color = Ink, fontSize = 26.sp, fontWeight = FontWeight.Bold)
    if (sub != null) Text(sub, color = Faint, fontSize = 13.sp, fontFamily = MONO)
}

@Composable
private fun Card(content: @Composable () -> Unit) {
    Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(Panel).padding(16.dp)) { content() }
}

// ---- components -----------------------------------------------------------

/** A circular 270° gauge with a big centred percentage; `big` bumps the type for the hero. */
@Composable
private fun GaugeStat(label: String, pct: Double?, color: Color, sub: String?, dim: Dp, big: Boolean) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Box(Modifier.size(dim), contentAlignment = Alignment.Center) {
            Canvas(Modifier.fillMaxSize()) {
                val sw = size.minDimension * 0.09f
                val tl = Offset(sw / 2f, sw / 2f)
                val arc = Size(size.width - sw, size.height - sw)
                drawArc(Panel2, 135f, 270f, false, tl, arc, style = Stroke(sw, cap = StrokeCap.Round))
                val p = (pct ?: 0.0).coerceIn(0.0, 100.0).toFloat()
                if (p > 0f) drawArc(color, 135f, 270f * p / 100f, false, tl, arc, style = Stroke(sw, cap = StrokeCap.Round))
            }
            Row(verticalAlignment = Alignment.Bottom) {
                Text(if (pct != null) "${pct.toInt()}" else "–", color = color,
                    fontSize = if (big) 50.sp else 32.sp, fontWeight = FontWeight.Bold, fontFamily = MONO)
                Text("%", color = Dim, fontSize = if (big) 18.sp else 13.sp, fontFamily = MONO,
                    modifier = Modifier.padding(start = 2.dp, bottom = if (big) 6.dp else 4.dp))
            }
        }
        Spacer(Modifier.height(10.dp))
        Text(label, color = Ink, fontSize = if (big) 15.sp else 13.sp, fontWeight = FontWeight.Medium)
        if (sub != null) Text(sub, color = Faint, fontSize = 12.sp, fontFamily = MONO)
    }
}

/** Context gauge whose source session is user-selectable — tap to pick which Claude Code
 *  session's context window the gauge tracks (defaults to the active one). */
@Composable
private fun ContextGauge(s: Snap, ctxSel: String?, onSel: (String?) -> Unit) {
    var open by remember { mutableStateOf(false) }
    val chosen = ctxSel?.let { sel -> s.sessions.firstOrNull { it.name == sel } }
    val pct = chosen?.pct ?: s.ctxPct
    val sub = chosen?.name ?: (s.ctxTokens?.let { "${Snap.fmtTokens(it)} tok" } ?: "active")
    val pickable = s.sessions.isNotEmpty()
    Box {
        Box(
            Modifier.clip(RoundedCornerShape(12.dp))
                .then(if (pickable) Modifier.clickable { open = true } else Modifier)
        ) {
            GaugeStat(if (pickable) "Context  ▾" else "Context", pct, bandColor(pct ?: 0.0), sub, 128.dp, false)
        }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            DropdownMenuItem(
                text = { Text("Active session", color = if (ctxSel == null) Accent else Ink) },
                onClick = { onSel(null); open = false },
            )
            s.sessions.forEach { sess ->
                DropdownMenuItem(
                    text = {
                        Text("${sess.name} · ${sess.pct.toInt()}%",
                            color = if (ctxSel == sess.name) Accent else Ink)
                    },
                    onClick = { onSel(sess.name); open = false },
                )
            }
        }
    }
}

@Composable
private fun SessionCard(s: Sess) {
    Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(Panel).padding(14.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Box(Modifier.size(8.dp).clip(CircleShape).background(if (s.active) hexColor("#5e9e72") else Faint))
            Text(s.name, color = Ink, fontSize = 16.sp, fontWeight = FontWeight.Medium, maxLines = 1,
                modifier = Modifier.padding(start = 10.dp).weight(1f))
            Text("${s.pct.toInt()}%", color = bandColor(s.pct), fontSize = 22.sp,
                fontWeight = FontWeight.Bold, fontFamily = MONO)
        }
        Spacer(Modifier.height(10.dp))
        Bar(s.pct, bandColor(s.pct), Modifier.fillMaxWidth())
        Spacer(Modifier.height(8.dp))
        Text("${Snap.fmtTokens(s.tokens)} tokens · ${if (s.active) "active" else "idle"}",
            color = Faint, fontSize = 12.sp, fontFamily = MONO)
    }
}

@Composable
private fun StatTile(label: String, value: String, modifier: Modifier = Modifier) {
    Column(modifier.clip(RoundedCornerShape(12.dp)).background(Panel).padding(16.dp)) {
        Text(value, color = Ink, fontSize = 24.sp, fontWeight = FontWeight.Bold, fontFamily = MONO, maxLines = 1)
        Spacer(Modifier.height(4.dp))
        Text(label, color = Faint, fontSize = 12.sp)
    }
}

@Composable
private fun SectionLabel(t: String) =
    Text(t, color = Faint, fontSize = 11.sp, fontFamily = MONO, letterSpacing = 1.sp)

@Composable
private fun KV(k: String, v: String) {
    Row(Modifier.fillMaxWidth().padding(vertical = 7.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(k, color = Dim, fontSize = 13.sp, modifier = Modifier.weight(1f))
        Text(v, color = Ink, fontSize = 13.sp, fontFamily = MONO, textAlign = TextAlign.End)
    }
}

@Composable
private fun Bar(pct: Double, color: Color, modifier: Modifier = Modifier) {
    Box(modifier.height(8.dp).clip(RoundedCornerShape(4.dp)).background(Panel2)) {
        Box(
            Modifier.fillMaxHeight().fillMaxWidth((pct.coerceIn(0.0, 100.0) / 100.0).toFloat())
                .clip(RoundedCornerShape(4.dp)).background(color)
        )
    }
}

@Composable
private fun VerdictBanner(text: String, color: Color) {
    Box(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp))
            .background(color.copy(alpha = 0.14f)).padding(horizontal = 16.dp, vertical = 12.dp)
    ) {
        Text(text.uppercase(), color = color, fontSize = 13.sp, fontWeight = FontWeight.SemiBold, letterSpacing = 0.5.sp)
    }
}

@Composable
private fun StatusChip(word: String, color: Color) {
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(end = 4.dp)) {
        Box(Modifier.size(8.dp).clip(CircleShape).background(color))
        Text(word, color = color, fontSize = 12.sp, fontFamily = MONO, modifier = Modifier.padding(start = 5.dp))
    }
}

@Composable
private fun WaitingForSync() {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Text("Waiting for your desktop", color = Ink, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(10.dp))
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
private fun ErrorState(msg: String, onRetry: () -> Unit) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Text(msg, color = hexColor("#d4694f"), fontSize = 15.sp, textAlign = TextAlign.Center)
        Spacer(Modifier.height(14.dp))
        TextButton(onClick = onRetry) { Text("Try again", color = Accent) }
    }
}

@Composable
private fun BottomNav(selected: Int, onSelect: (Int) -> Unit) {
    NavigationBar(containerColor = Panel, tonalElevation = 0.dp) {
        val items = listOf(
            Triple("Overview", Icons.Filled.Speed, 0),
            Triple("Sessions", Icons.AutoMirrored.Filled.ViewList, 1),
            Triple("Chat", Icons.Filled.Forum, 2),
            Triple("Stats", Icons.Filled.Insights, 3),
            Triple("Settings", Icons.Filled.Settings, 4),
        )
        val colors = NavigationBarItemDefaults.colors(
            selectedIconColor = Accent, selectedTextColor = Accent, indicatorColor = Panel2,
            unselectedIconColor = Dim, unselectedTextColor = Faint,
        )
        items.forEach { (label, icon, idx) ->
            NavigationBarItem(
                selected = selected == idx,
                onClick = { onSelect(idx) },
                icon = { Icon(icon, contentDescription = label) },
                label = { Text(label, fontSize = 11.sp) },
                colors = colors,
            )
        }
    }
}

// ---- helpers --------------------------------------------------------------

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

private fun syncedAgo(updatedAtSec: Long, now: Long): String {
    if (updatedAtSec <= 0) return "—"
    val s = now / 1000 - updatedAtSec
    return when {
        s < 5 -> "synced just now"
        s < 60 -> "synced ${s}s ago"
        s < 3600 -> "synced ${s / 60}m ago"
        s < 86400 -> "synced ${s / 3600}h ago"
        else -> "synced ${s / 86400}d ago"
    }
}
