package com.claudeusage.tracker.ui

import android.Manifest
import android.content.Intent
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.consumeWindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ViewList
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.Forum
import androidx.compose.material.icons.filled.Groups
import androidx.compose.material.icons.filled.Insights
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Speed
import androidx.compose.material.icons.filled.Sync
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
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
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.claudeusage.tracker.ExtraUsage
import com.claudeusage.tracker.Notifications
import com.claudeusage.tracker.Pairing
import com.claudeusage.tracker.PoolAccount
import com.claudeusage.tracker.Prefs
import com.claudeusage.tracker.Snap
import com.claudeusage.tracker.TeamOverview
import com.claudeusage.tracker.Transcript
import com.claudeusage.tracker.widget.WidgetData
import com.claudeusage.tracker.widget.updateLockNotification
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import kotlinx.coroutines.delay

/** Pending destructive action awaiting confirmation: "remove:<accountId>" or "unpair". */
private const val CONFIRM_UNPAIR = "unpair"
private const val CONFIRM_REMOVE = "remove:"

@Composable
fun DashboardScreen(
    onUnpaired: () -> Unit,
    onPairedNew: () -> Unit,
    vm: DashboardViewModel = viewModel(),
) {
    val ctx = LocalContext.current
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    var tab by rememberSaveable { mutableStateOf("home") }
    var ctxSel by rememberSaveable { mutableStateOf<String?>(null) }   // which session drives the Context gauge
    var chatSel by rememberSaveable { mutableStateOf<String?>(null) }  // which mirrored conversation is shown
    var confirm by rememberSaveable { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) { vm.syncFromPrefs() }

    val scanLauncher = rememberLauncherForActivityResult(ScanContract()) { result ->
        result.contents?.let { Pairing.parse(it.trim()) }?.let { p ->
            vm.added(p)
            onPairedNew()
        }
    }
    val addAccount: () -> Unit = {
        scanLauncher.launch(
            ScanOptions().setOrientationLocked(false)   // portrait pinned by PortraitCaptureActivity (manifest)
                .setCaptureActivity(PortraitCaptureActivity::class.java)
                .setBeepEnabled(false)
                .setPrompt("Scan the pairing QR from another desktop"),
        )
    }
    val askRemove: (String) -> Unit = { id -> confirm = CONFIRM_REMOVE + id }

    // Poll fast until the first live snapshot lands, then settle; the 1s ticker drives countdowns.
    // Both are gated to STARTED so they pause when the app is backgrounded (no battery/network
    // drain off-screen — the WorkManager job keeps the widget fresh meanwhile).
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    LaunchedEffect(Unit) {
        lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) {
            while (true) {
                vm.reload()
                delay(if (vm.snap == null || vm.fromCache) 5_000L else 20_000L)
            }
        }
    }
    LaunchedEffect(Unit) {
        lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) {
            while (true) { now = System.currentTimeMillis(); delay(1_000) }
        }
    }

    ConfirmDialogs(
        pending = confirm, accounts = vm.accounts,
        onDismiss = { confirm = null },
        onRemove = { id -> confirm = null; if (vm.remove(id)) onUnpaired() },
        onUnpairAll = { confirm = null; vm.unpairAll(); onUnpaired() },
    )

    val s = vm.snap
    if (s == null) {
        Box(
            Modifier.fillMaxSize().background(Bg).windowInsetsPadding(WindowInsets.safeDrawing).padding(24.dp),
            contentAlignment = Alignment.Center,
        ) {
            val err = vm.error
            if (err == null) WaitingForSync()
            else ErrorState(err, onRetry = { vm.refresh() },
                onRemove = vm.activeId?.let { id -> { askRemove(id) } })
        }
        return
    }

    val hasTeam = s.teamOverview != null
    val hasConversation = s.transcripts.isNotEmpty() || s.mirrorOn
    val tabs = buildList {
        add("home")
        add(if (hasTeam) "team" else "sessions")
        if (hasConversation) add("conversation")
        add("history")
        add("settings")
    }
    val current = if (tab in tabs) tab else "home"

    Scaffold(
        containerColor = Bg,
        bottomBar = { BottomNav(tabs, current) { tab = it } },
    ) { inner ->
        Column(Modifier.fillMaxSize().padding(inner).consumeWindowInsets(inner)) {
            StatusBanners(s, vm.error, now, onRetry = { vm.refresh() })
            Box(Modifier.weight(1f)) {
                when (current) {
                    "home" -> HomePage(s, now, vm.accounts, vm.activeId, vm::switchTo, addAccount,
                        ctxSel, onCtxSel = { ctxSel = it }, onRefresh = { vm.refresh() })
                    "team" -> TeamPage(s, now)
                    "sessions" -> SessionsPage(s)
                    "conversation" -> ConversationPage(s, chatSel, onChatSel = { chatSel = it })
                    "history" -> HistoryPage(s)
                    else -> SettingsPage(s, Prefs.active(ctx), now, vm.lastOkAt, vm.accounts, vm.activeId,
                        addAccount, askRemove, onUnpairAll = { confirm = CONFIRM_UNPAIR }, onRefresh = { vm.refresh() })
                }
            }
        }
    }
}

// ---- banners + dialogs ----------------------------------------------------

/** Non-blocking problems shown above every page while data is on screen: our own refresh
 *  failing (with Retry), and the desktop reporting it can't read usage (login expired, etc.). */
@Composable
private fun StatusBanners(s: Snap, error: String?, now: Long, onRetry: () -> Unit) {
    val desktopProblem: Pair<String, Boolean>? = when {
        s.loginExpired ->
            "Your desktop's Claude login has expired — run any Claude Code command on the desktop to refresh it." to true
        !s.ok && s.error != null && s.error.contains("waiting for Claude Code", ignoreCase = true) -> null
        !s.ok && s.error != null -> "Desktop couldn't read usage: ${s.error}" to false
        else -> null
    }
    if (error == null && desktopProblem == null) return
    Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp).padding(top = 8.dp)) {
        if (error != null) {
            Banner(
                text = (if (s.updatedAt > 0) "Couldn't refresh — showing data from ${ago(ageSeconds(s.updatedAt, now))}. "
                else "Couldn't refresh — showing the last data. ") + error,
                color = Warn, action = "Retry", onAction = onRetry,
            )
        }
        desktopProblem?.let { (msg, severe) ->
            if (error != null) Spacer(Modifier.height(6.dp))
            Banner(text = msg, color = if (severe) Danger else Warn, action = null, onAction = null)
        }
    }
}

@Composable
private fun Banner(text: String, color: Color, action: String?, onAction: (() -> Unit)?) {
    Row(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp)).background(color.copy(alpha = 0.14f))
            .padding(start = 12.dp, end = 4.dp, top = 6.dp, bottom = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(Icons.Filled.Warning, contentDescription = null, tint = color, modifier = Modifier.size(18.dp))
        Text(text, color = Ink, style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.weight(1f).padding(horizontal = 10.dp, vertical = 6.dp))
        if (action != null && onAction != null) {
            TextButton(onClick = onAction, modifier = Modifier.heightIn(min = 48.dp)) { Text(action, color = Accent) }
        }
    }
}

@Composable
private fun ConfirmDialogs(
    pending: String?, accounts: List<Prefs.Account>,
    onDismiss: () -> Unit, onRemove: (String) -> Unit, onUnpairAll: () -> Unit,
) {
    if (pending == null) return
    when {
        pending == CONFIRM_UNPAIR -> AlertDialog(
            onDismissRequest = onDismiss,
            title = { Text("Unpair this phone?") },
            text = {
                Text("All ${accounts.size} desktops are removed from this phone and it stops receiving " +
                    "their alerts. You can pair again any time by scanning a desktop's QR.")
            },
            confirmButton = { TextButton(onClick = onUnpairAll) { Text("Unpair", color = Danger) } },
            dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
        )
        pending.startsWith(CONFIRM_REMOVE) -> {
            val id = pending.removePrefix(CONFIRM_REMOVE)
            val acc = accounts.firstOrNull { it.pairing.accountId == id }
            if (acc == null) {                       // already gone (e.g. removed elsewhere)
                LaunchedEffect(pending) { onDismiss() }
                return
            }
            val name = acc.label?.ifBlank { null } ?: id.take(8)
            val last = accounts.size <= 1
            AlertDialog(
                onDismissRequest = onDismiss,
                title = { Text(if (last) "Unpair “$name”?" else "Remove “$name”?") },
                text = {
                    Text(
                        (if (last) "This is the only paired desktop, so this phone will be unpaired. "
                        else "This phone stops showing this desktop's usage. ") +
                            "Its alerts stop too. You can pair again by scanning its QR."
                    )
                },
                confirmButton = {
                    TextButton(onClick = { onRemove(id) }) { Text(if (last) "Unpair" else "Remove", color = Danger) }
                },
                dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
            )
        }
    }
}

// ---- pages ----------------------------------------------------------------

@Composable
private fun HomePage(
    s: Snap, now: Long,
    accounts: List<Prefs.Account>, activeId: String?, onSwitch: (String) -> Unit, onAddAccount: () -> Unit,
    ctxSel: String?, onCtxSel: (String?) -> Unit, onRefresh: () -> Unit,
) {
    PageScroll {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            AccountSwitcher(s, accounts, activeId, onSwitch, onAddAccount, Modifier.weight(1f))
            s.statusWord?.let { StatusChip(it, hexColor(s.statusColor, Dim)) }
            IconButton(onClick = onRefresh) { Icon(Icons.Filled.Refresh, "Refresh", tint = Dim) }
        }
        SyncLine(s.updatedAt, now)
        if (s.deviceOk == false) {
            Text("This pooled account is also in use on another device.", color = Warn,
                style = MaterialTheme.typography.labelMedium, modifier = Modifier.padding(top = 2.dp))
        }

        BoxWithConstraints(Modifier.fillMaxWidth()) {
            val wide = maxWidth >= 600.dp
            if (wide) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(24.dp)) {
                    Column(Modifier.weight(1f)) {
                        Spacer(Modifier.height(20.dp))
                        Gauges(s, now, ctxSel, onCtxSel, maxWidth / 2)
                    }
                    Column(Modifier.weight(1f)) {
                        Spacer(Modifier.height(16.dp))
                        HomeDetails(s, now)
                    }
                }
            } else {
                Column(Modifier.fillMaxWidth()) {
                    s.verdictText?.let { Spacer(Modifier.height(16.dp)); VerdictBanner(it, hexColor(s.verdictColor, Dim)) }
                    Spacer(Modifier.height(24.dp))
                    Gauges(s, now, ctxSel, onCtxSel, maxWidth)
                    HomeDetails(s, now, showVerdict = false)
                }
            }
        }
    }
}

/** Hero 5h gauge + weekly/context pair (stacked when they wouldn't fit side by side, e.g. at a
 *  large font scale). */
@Composable
private fun Gauges(s: Snap, now: Long, ctxSel: String?, onCtxSel: (String?) -> Unit, avail: androidx.compose.ui.unit.Dp) {
    val five = s.fiveHour
    val week = s.sevenDay
    Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
        GaugeStat(
            label = "5-hour limit", pct = five?.pct,
            color = hexColor(five?.color, usageColor(five?.pct ?: 0.0)),
            sub = five?.let { fmtCountdown(it.resetsAt, now) },
            detail = burnLine(five),
            dim = gaugeSize(188.dp, avail), big = true,
            spoken = spokenWindow("5-hour window", five?.pct, five?.resetsAt, now) +
                (burnLine(five)?.let { ". Burn rate $it" } ?: ""),
        )
    }
    Spacer(Modifier.height(24.dp))
    val small = gaugeSize(128.dp)
    BoxWithConstraints(Modifier.fillMaxWidth()) {
        val weekly: @Composable () -> Unit = {
            GaugeStat("Weekly", week?.pct, hexColor(week?.color, usageColor(week?.pct ?: 0.0)),
                week?.let { fmtCountdown(it.resetsAt, now) }, burnLine(week), small, false,
                spoken = spokenWindow("Weekly window", week?.pct, week?.resetsAt, now))
        }
        val context: @Composable () -> Unit = { ContextGauge(s, ctxSel, onCtxSel, small) }
        if (maxWidth >= small * 2 + 24.dp) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly) { weekly(); context() }
        } else {
            Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
                weekly(); Spacer(Modifier.height(20.dp)); context()
            }
        }
    }
}

@Composable
private fun HomeDetails(s: Snap, now: Long, showVerdict: Boolean = true) {
    if (showVerdict) s.verdictText?.let { VerdictBanner(it, hexColor(s.verdictColor, Dim)); Spacer(Modifier.height(12.dp)) }
    val scoped = s.scoped
    if (scoped.isNotEmpty()) {
        Spacer(Modifier.height(16.dp))
        Card {
            SectionLabel("WEEKLY · BY MODEL")
            Spacer(Modifier.height(4.dp))
            scoped.forEach { w ->
                MiniWindowRow(w.label, w.pct, w.resetsAt?.let { fmtCountdown(it, now) })
            }
        }
    }
    s.extra?.let { Spacer(Modifier.height(16.dp)); ExtraCard(it) }
    if (s.sessions.isNotEmpty()) {
        Spacer(Modifier.height(20.dp))
        SectionLabel("SESSIONS · LAST 5H")
        Spacer(Modifier.height(10.dp))
        s.sessions.take(2).forEach { SessionCard(it); Spacer(Modifier.height(10.dp)) }
    }
}

/** "synced 12s ago" — turns amber with a warning icon and the word "stale" once the desktop
 *  hasn't pushed for [STALE_AFTER_S]. */
@Composable
private fun SyncLine(updatedAt: Long, now: Long) {
    val stale = ageSeconds(updatedAt, now) > STALE_AFTER_S
    val color = if (stale) Warn else Faint
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 2.dp)) {
        Icon(if (stale) Icons.Filled.Warning else Icons.Filled.Sync, contentDescription = null,
            tint = color, modifier = Modifier.size(13.dp))
        Text(
            syncedAgo(updatedAt, now) + if (stale) " · stale — is the desktop app running?" else "",
            color = color, style = MaterialTheme.typography.labelMedium, modifier = Modifier.padding(start = 4.dp),
        )
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
        Column(
            Modifier.clip(RoundedCornerShape(8.dp))
                .clickable(onClickLabel = "Switch or add account", role = Role.Button) { open = true }
                .padding(vertical = 2.dp)
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(name, color = Ink, style = MaterialTheme.typography.headlineSmall, maxLines = 1,
                    modifier = Modifier.weight(1f, fill = false))
                Icon(Icons.Filled.ArrowDropDown, contentDescription = null, tint = Faint)
            }
            Text(
                if (multi) "${s.subscription.ifBlank { "plan" }} · ${accounts.size} accounts"
                else s.subscription.ifBlank { "your" } + " plan",
                color = Faint, style = MaterialTheme.typography.labelMedium,
            )
        }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            accounts.forEach { acc ->
                val id = acc.pairing.accountId
                val lbl = acc.label?.ifBlank { null } ?: id.take(8)
                val isActive = id == activeId
                DropdownMenuItem(
                    text = { Text(if (isActive) "$lbl  (current)" else lbl, color = if (isActive) Accent else Ink) },
                    onClick = { open = false; onSwitch(id) },
                )
            }
            DropdownMenuItem(
                text = { Text("+ Add account", color = Accent) },
                onClick = { open = false; onAddAccount() },
            )
        }
    }
}

/** Context gauge whose source session is user-selectable — tap to pick which Claude Code
 *  session's context window the gauge tracks (defaults to the active one). */
@Composable
private fun ContextGauge(s: Snap, ctxSel: String?, onSel: (String?) -> Unit, dim: androidx.compose.ui.unit.Dp) {
    var open by remember { mutableStateOf(false) }
    val chosen = ctxSel?.let { sel -> s.sessions.firstOrNull { it.name == sel } }
    val pct = chosen?.pct ?: s.ctxPct
    val sub = chosen?.name ?: (s.ctxTokens?.let { "${Snap.fmtTokens(it)} tok" } ?: "active session")
    val pickable = s.sessions.isNotEmpty()
    val spoken = "Context window, " + (if (pct != null) "${pct.toInt()} percent full" else "no data") +
        ", " + (chosen?.name?.let { "session $it" } ?: "active session")
    Box {
        Box(
            Modifier.clip(RoundedCornerShape(12.dp))
                .then(if (pickable) Modifier.clickable(onClickLabel = "Choose session", role = Role.Button) { open = true } else Modifier)
        ) {
            GaugeStat("Context", pct, bandColor(pct ?: 0.0), sub, null, dim, false, spoken = spoken,
                dropdown = pickable)
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
private fun SessionsPage(s: Snap) {
    PageScroll {
        PageTitle("Sessions", "last 5 hours")
        Spacer(Modifier.height(16.dp))
        if (s.sessions.isEmpty()) {
            Text("No active Claude Code sessions in the last 5 hours.", color = Dim, style = MaterialTheme.typography.bodyMedium)
        } else {
            s.sessions.forEach { SessionCard(it); Spacer(Modifier.height(12.dp)) }
        }
    }
}

/** Team pool overview (v0.3): every signed-in teammate — admin or member — sees the pooled
 *  Claude accounts, from the compact `team_overview` embedded in the E2EE snapshot. */
@Composable
private fun TeamPage(s: Snap, now: Long) {
    val ov = s.teamOverview
    PageScroll {
        val sub = ov?.let {
            listOfNotNull(
                "${it.accountCount} account" + if (it.accountCount == 1) "" else "s",
                it.tz.ifBlank { null },
                it.role?.let { r -> "you're ${if (r == "admin") "an admin" else "a $r"}" },
            ).joinToString(" · ")
        }
        PageTitle("Team pool", sub)
        Spacer(Modifier.height(16.dp))
        if (ov == null) {
            Text("Sign in to your team on the desktop (Team tab) to see the account pool here.",
                color = Dim, style = MaterialTheme.typography.bodyMedium)
            return@PageScroll
        }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            StatTile("Spend this month", orgSpendText(ov), Modifier.weight(1f))
            StatTile("Near limits", ov.near.size.toString(), Modifier.weight(1f))
        }
        if (ov.near.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Filled.Warning, contentDescription = null, tint = Danger, modifier = Modifier.size(14.dp))
                Text(" Near limit: " + ov.near.joinToString(" · ") { "${it.name} ${it.window} ${it.pct.toInt()}%" },
                    color = Danger, style = MaterialTheme.typography.labelMedium)
            }
        }
        Spacer(Modifier.height(16.dp))
        SectionLabel("ACCOUNTS")
        Spacer(Modifier.height(8.dp))
        ov.accounts.forEach { PoolAccountRow(it, now); Spacer(Modifier.height(10.dp)) }
    }
}

/** Per-currency spend — never summed across currencies (`org_spend` is null when mixed). */
private fun orgSpendText(ov: TeamOverview): String = when {
    ov.spendByCurrency.size > 1 -> ov.spendByCurrency.entries.joinToString("\n") { (c, v) -> fmtMoney(v, c) }
    ov.orgSpend != null -> fmtMoney(ov.orgSpend, ov.orgCurrency ?: ov.spendByCurrency.keys.firstOrNull()
        ?: ov.accounts.firstOrNull { it.currency.isNotBlank() }?.currency ?: "")
    ov.spendByCurrency.size == 1 -> ov.spendByCurrency.entries.first().let { (c, v) -> fmtMoney(v, c) }
    else -> "—"
}

@Composable
private fun PoolAccountRow(m: PoolAccount, now: Long) {
    Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(CardBg).padding(14.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Text(m.name, color = Ink, style = MaterialTheme.typography.titleSmall, maxLines = 1,
                modifier = Modifier.weight(1f))
            Column(horizontalAlignment = Alignment.End) {
                Text(m.monthSpend?.let { fmtMoney(it, m.currency) } ?: "—",
                    color = Ink, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold, fontFamily = MONO)
                m.monthTokens?.takeIf { it > 0 }?.let {
                    Text("${Snap.fmtTokens(it)} tok this month", color = Faint, style = MaterialTheme.typography.labelMedium)
                }
            }
        }
        Spacer(Modifier.height(10.dp))
        Row(Modifier.fillMaxWidth()) {
            PoolWindow("5h", m.fhPct, m.fhResetsAt, now, Modifier.weight(1f))
            Spacer(Modifier.width(12.dp))
            PoolWindow("wk", m.sdPct, m.sdResetsAt, now, Modifier.weight(1f))
        }
    }
}

@Composable
private fun PoolWindow(label: String, pct: Double?, resetsAt: Long?, now: Long, modifier: Modifier) {
    Column(modifier) {
        Text("$label " + (pct?.let { "${it.toInt()}%" } ?: "—"), color = Faint, style = MaterialTheme.typography.labelMedium)
        Spacer(Modifier.height(4.dp))
        Bar(pct ?: 0.0, usageColor(pct ?: 0.0), Modifier.fillMaxWidth())
        if (resetsAt != null && resetsAt > now) {
            Text(fmtCountdown(resetsAt, now), color = Faint, style = MaterialTheme.typography.labelMedium,
                modifier = Modifier.padding(top = 3.dp))
        }
    }
}

private fun fmtMoney(v: Double, cur: String): String =
    (if (cur.isNotBlank()) "$cur " else "") + String.format("%.2f", v)

/** Read-only mirror of the desktop's recent Claude Code conversations (opt-in on the desktop:
 *  Settings → Remote (phone) → mirror the active conversation). The phone never sends prompts. */
@Composable
private fun ConversationPage(s: Snap, chatSel: String?, onChatSel: (String?) -> Unit) {
    val chosen = chatSel?.let { sel -> s.transcripts.firstOrNull { (it.cwd ?: it.name) == sel } }
        ?: s.transcripts.firstOrNull()
    PageScroll {
        ConversationPicker(s.transcripts, chosen, onChatSel)
        Spacer(Modifier.height(16.dp))
        if (chosen == null || chosen.messages.isEmpty()) {
            Text(
                "No conversation mirrored yet.\n\nOn the desktop: Settings → Remote (phone) → turn on " +
                    "\"mirror the active conversation\" to read it here (end-to-end encrypted).",
                color = Dim, style = MaterialTheme.typography.bodyMedium,
            )
        } else {
            chosen.messages.forEach { MsgBubble(it); Spacer(Modifier.height(10.dp)) }
            Spacer(Modifier.height(6.dp))
            Text("Read-only mirror · updates on each sync", color = Faint, style = MaterialTheme.typography.labelMedium)
        }
    }
}

/** Title that doubles as a conversation picker: tap to choose which mirrored session to read. */
@Composable
private fun ConversationPicker(transcripts: List<Transcript>, chosen: Transcript?, onSel: (String?) -> Unit) {
    var open by remember { mutableStateOf(false) }
    val pickable = transcripts.size > 1
    val selKey = chosen?.let { it.cwd ?: it.name }
    Box {
        Column(
            Modifier.clip(RoundedCornerShape(8.dp))
                .then(if (pickable) Modifier.clickable(onClickLabel = "Choose conversation", role = Role.Button) { open = true } else Modifier)
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Conversation", color = Ink, style = MaterialTheme.typography.headlineMedium)
                if (pickable) Icon(Icons.Filled.ArrowDropDown, contentDescription = null, tint = Faint)
            }
            Text(
                chosen?.let { (if (it.active) "${it.name} · active" else it.name) + if (pickable) " · tap to switch" else "" } ?: "—",
                color = Faint, style = MaterialTheme.typography.labelMedium,
            )
        }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            transcripts.forEach { t ->
                val key = t.cwd ?: t.name
                DropdownMenuItem(
                    text = {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Box(Modifier.size(7.dp).clip(CircleShape).background(if (t.active) LiveGreen else Faint))
                            Text("  ${t.name}" + if (t.active) " (active)" else "",
                                color = if (key == selKey) Accent else Ink)
                        }
                    },
                    onClick = { onSel(key); open = false },
                )
            }
        }
    }
}

@Composable
private fun HistoryPage(s: Snap) {
    PageScroll {
        PageTitle("History", null)
        s.history?.let { h ->
            Spacer(Modifier.height(16.dp))
            Card {
                SectionLabel("RECENT USAGE")
                Spacer(Modifier.height(10.dp))
                Sparkline(h)
            }
        }
        Spacer(Modifier.height(20.dp))
        Column(
            Modifier.fillMaxWidth().semantics(mergeDescendants = true) {},
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(s.atTokens?.let { Snap.fmtTokens(it) } ?: "—", color = Accent, style = MaterialTheme.typography.displayMedium)
            Text("ALL-TIME TOKENS", color = Faint, style = MaterialTheme.typography.labelSmall)
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
    s: Snap, pairing: Pairing?, now: Long, lastOkAt: Long,
    accounts: List<Prefs.Account>, activeId: String?,
    onAddAccount: () -> Unit, onRemove: (String) -> Unit, onUnpairAll: () -> Unit,
    onRefresh: () -> Unit,
) {
    val ctx = LocalContext.current
    var lockOn by remember { mutableStateOf(WidgetData.lockscreen(ctx)) }
    var lockDenied by remember { mutableStateOf(false) }
    val canPost = Notifications.canPost(ctx)
    val permLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        lockOn = granted
        lockDenied = !granted
        WidgetData.setLockscreen(ctx, granted)
        updateLockNotification(ctx)
    }
    val openNotifSettings = {
        ctx.startActivity(
            Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                .putExtra(Settings.EXTRA_APP_PACKAGE, ctx.packageName)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }
    PageScroll {
        PageTitle("Settings", null)
        Spacer(Modifier.height(16.dp))
        Card {
            SectionLabel("PAIRED DESKTOPS")
            Spacer(Modifier.height(4.dp))
            accounts.forEach { acc ->
                val id = acc.pairing.accountId
                val lbl = acc.label?.ifBlank { null } ?: id.take(8)
                val isActive = id == activeId
                Row(Modifier.fillMaxWidth().heightIn(min = 48.dp), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(lbl, color = Ink, style = MaterialTheme.typography.bodyMedium, maxLines = 1)
                        if (isActive) Text("showing now", color = Faint, style = MaterialTheme.typography.labelMedium)
                    }
                    TextButton(
                        onClick = { onRemove(id) },
                        modifier = Modifier.heightIn(min = 48.dp).semantics { contentDescription = "Remove $lbl" },
                    ) {
                        Text("Remove", color = Danger, style = MaterialTheme.typography.labelLarge)
                    }
                }
            }
            Row {
                TextButton(onClick = onAddAccount, modifier = Modifier.heightIn(min = 48.dp)) {
                    Text("+ Add account", color = Accent, style = MaterialTheme.typography.labelLarge)
                }
                if (accounts.size > 1) {
                    Spacer(Modifier.weight(1f))
                    TextButton(onClick = onUnpairAll, modifier = Modifier.heightIn(min = 48.dp)) {
                        Text("Unpair all", color = Danger, style = MaterialTheme.typography.labelLarge)
                    }
                }
            }
            Text("Switch accounts from the name at the top of Home.", color = Faint,
                style = MaterialTheme.typography.labelMedium)
        }
        Spacer(Modifier.height(14.dp))
        Card {
            SectionLabel("ACCOUNT")
            Spacer(Modifier.height(4.dp))
            KV("Organization", s.org.ifBlank { "—" })
            KV("Email", s.email.ifBlank { "—" })
            KV("Plan", s.subscription.ifBlank { "—" })
            if (s.loginExpired) KV("Desktop login", "expired", Danger)
        }
        Spacer(Modifier.height(14.dp))
        Card {
            SectionLabel("SYNC")
            Spacer(Modifier.height(4.dp))
            val stale = ageSeconds(s.updatedAt, now) > STALE_AFTER_S
            KV("Desktop", when {
                stale -> "stale"
                s.ok -> "live"
                else -> "idle / error"
            }, if (stale) Warn else Ink)
            KV("Last snapshot", syncedAgo(s.updatedAt, now).removePrefix("synced "), if (stale) Warn else Ink)
            KV("Last checked", if (lastOkAt > 0) ago(((now - lastOkAt) / 1000).coerceAtLeast(0)) else "—")
            KV("Relay", pairing?.url?.substringAfter("://")?.substringBefore("/") ?: "—")
            s.statusWord?.let { KV("Claude status", s.statusDesc ?: it) }
        }
        Spacer(Modifier.height(14.dp))
        Card {
            SectionLabel("NOTIFICATIONS")
            Spacer(Modifier.height(8.dp))
            KV("Usage alerts", if (canPost) "on" else "off", if (canPost) Ink else Warn)
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("Show usage on the lock screen", color = Ink, style = MaterialTheme.typography.bodyMedium)
                    Text("Ongoing notification · 5h / weekly / context", color = Faint,
                        style = MaterialTheme.typography.labelMedium)
                }
                Switch(
                    checked = lockOn && canPost,
                    onCheckedChange = { on ->
                        when {
                            !on -> {
                                lockOn = false; lockDenied = false
                                WidgetData.setLockscreen(ctx, false); updateLockNotification(ctx)
                            }
                            Notifications.canPost(ctx) -> {
                                lockOn = true; lockDenied = false
                                WidgetData.setLockscreen(ctx, true); updateLockNotification(ctx)
                            }
                            Notifications.needsRuntimePermission(ctx) ->
                                permLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
                            else -> lockDenied = true          // switched off in system settings
                        }
                    },
                    colors = SwitchDefaults.colors(checkedTrackColor = Accent, checkedThumbColor = Ink),
                    modifier = Modifier.semantics { contentDescription = "Show usage on the lock screen" },
                )
            }
            if (!canPost && (lockDenied || lockOn)) {
                Spacer(Modifier.height(6.dp))
                Text(
                    "Notifications are blocked for this app, so alerts and the lock-screen card can't show.",
                    color = Warn, style = MaterialTheme.typography.bodySmall,
                )
                TextButton(onClick = openNotifSettings, modifier = Modifier.heightIn(min = 48.dp)) {
                    Text("Open notification settings", color = Accent)
                }
            }
        }
        Spacer(Modifier.height(18.dp))
        TextButton(onClick = onRefresh, modifier = Modifier.heightIn(min = 48.dp)) { Text("Refresh now", color = Accent) }
        Spacer(Modifier.height(18.dp))
        Text(
            "Unofficial · not affiliated with Anthropic. \"Claude\" and \"Claude Code\" " +
                "are trademarks of Anthropic.",
            color = Faint, style = MaterialTheme.typography.bodySmall,
            textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth(),
        )
    }
}

/** Extra-usage card for the Home bento. */
@Composable
private fun ExtraCard(e: ExtraUsage) {
    Card {
        SectionLabel("EXTRA USAGE · THIS MONTH")
        Spacer(Modifier.height(8.dp))
        val cur = e.currency
        val pre = if (cur.isNotBlank()) "$cur " else ""
        val dim = Dim
        Text(
            buildAnnotatedString {
                append(pre)
                withStyle(SpanStyle(fontWeight = FontWeight.Bold)) {
                    append(e.used?.let { String.format("%.2f", it) } ?: "—")
                }
                e.limit?.let {
                    withStyle(SpanStyle(color = dim)) { append("  of $pre${String.format("%.2f", it)}") }
                }
            },
            color = Ink, style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Normal, fontFamily = MONO,
        )
        e.pct?.let { p ->
            Spacer(Modifier.height(10.dp))
            Bar(p, usageColor(p), Modifier.fillMaxWidth())
            Spacer(Modifier.height(6.dp))
            Text("${p.toInt()}% of monthly cap", color = Faint, style = MaterialTheme.typography.labelMedium)
        }
    }
}

@Composable
private fun BottomNav(tabs: List<String>, selected: String, onSelect: (String) -> Unit) {
    NavigationBar(containerColor = Panel, tonalElevation = 0.dp) {
        val colors = NavigationBarItemDefaults.colors(
            selectedIconColor = Accent, selectedTextColor = Accent, indicatorColor = Panel2,
            unselectedIconColor = Dim, unselectedTextColor = Faint,
        )
        tabs.forEach { key ->
            val (label, icon) = when (key) {
                "home" -> "Home" to Icons.Filled.Speed
                "team" -> "Team" to Icons.Filled.Groups
                "sessions" -> "Sessions" to Icons.AutoMirrored.Filled.ViewList
                "conversation" -> "Convo" to Icons.Filled.Forum
                "history" -> "History" to Icons.Filled.Insights
                else -> "Settings" to Icons.Filled.Settings
            }
            NavigationBarItem(
                selected = selected == key,
                onClick = { onSelect(key) },
                icon = { Icon(icon, contentDescription = null) },   // the label is announced
                label = { Text(label, maxLines = 1) },
                colors = colors,
            )
        }
    }
}
