package com.claudeusage.tracker.widget

import android.annotation.SuppressLint
import android.content.Context
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.glance.appwidget.GlanceAppWidgetManager
import androidx.glance.appwidget.state.getAppWidgetState
import androidx.glance.appwidget.state.updateAppWidgetState
import androidx.glance.state.PreferencesGlanceStateDefinition
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.claudeusage.tracker.AppScope
import com.claudeusage.tracker.Notifications
import com.claudeusage.tracker.Pairing
import com.claudeusage.tracker.Prefs
import com.claudeusage.tracker.R
import com.claudeusage.tracker.RelayClient
import com.claudeusage.tracker.Snap
import com.claudeusage.tracker.push.PushTokens
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** Small on-device cache shared by the widgets + the ongoing notification, one snapshot per
 *  paired account (each widget instance can show a different account). Usage numbers only: the
 *  mirrored conversation (transcripts), the account email, the sparkline history and the team
 *  suggestion are stripped before caching (see [stripSensitive]), and pairing keys live in
 *  [Prefs]'s EncryptedSharedPreferences — so nothing sensitive sits in these plaintext prefs. */
object WidgetData {
    private const val FILE = "cut_widget"
    private const val K_SNAP = "snap:"              // + accountId → stripped snapshot JSON
    private const val K_EMPTY = "empty:"            // + accountId → the relay has nothing yet
    private const val K_LEGACY_SNAP = "snap"        // ≤ v0.3: one snapshot …
    private const val K_LEGACY_ACCT = "snap_acct"   // … and the account it belonged to
    private const val K_LOCK = "lockscreen"
    private const val K_PREVIEW_FOR = "preview_for" // account whose generated picker preview is set

    private fun sp(ctx: Context) = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun saveSnap(ctx: Context, accountId: String, json: String) =
        sp(ctx).edit().putString(K_SNAP + accountId, stripSensitive(json)).remove(K_EMPTY + accountId).apply()

    /** The relay answered "no snapshot yet" for this account (desktop never synced). */
    fun markEmpty(ctx: Context, accountId: String) = sp(ctx).edit().putBoolean(K_EMPTY + accountId, true).apply()

    fun isEmpty(ctx: Context, accountId: String): Boolean = sp(ctx).getBoolean(K_EMPTY + accountId, false)

    /** The widget + lock-screen notification render only usage numbers, so drop the mirrored
     *  conversation, the account email and the teammate suggestion before this string lands in
     *  plaintext prefs. */
    private fun stripSensitive(json: String): String = runCatching {
        val o = JSONObject(json)
        o.remove("transcripts"); o.remove("transcript")
        o.remove("history")
        o.optJSONObject("account")?.remove("email")
        o.optJSONObject("decision")?.remove("suggestion")
        o.toString()
    }.getOrDefault("{}")

    /** The cached snapshot of the currently-active account. */
    fun loadSnap(ctx: Context): String? = Prefs.activeId(ctx)?.let { loadSnap(ctx, it) }

    /** The cached snapshot of one account (null if none; removing an account clears its entry). */
    fun loadSnap(ctx: Context, accountId: String): String? {
        migrateLegacy(ctx)
        return sp(ctx).getString(K_SNAP + accountId, null)
    }

    /** v0.3 kept a single snapshot + its owner; move it under the per-account key. A cache
     *  without an owner is trusted only when exactly one account is paired. */
    private fun migrateLegacy(ctx: Context) {
        val s = sp(ctx)
        val legacy = s.getString(K_LEGACY_SNAP, null) ?: return
        val owner = s.getString(K_LEGACY_ACCT, null)
            ?: Prefs.all(ctx).singleOrNull()?.pairing?.accountId
        s.edit().apply {
            if (owner != null && !s.contains(K_SNAP + owner)) putString(K_SNAP + owner, legacy)
            remove(K_LEGACY_SNAP); remove(K_LEGACY_ACCT)
        }.apply()
    }

    /** Drop one account's cached snapshot (the account was removed). */
    fun clearSnap(ctx: Context, accountId: String) =
        sp(ctx).edit().remove(K_SNAP + accountId).remove(K_EMPTY + accountId).apply()

    /** Drop every cached snapshot (unpair everything). */
    fun clearAll(ctx: Context) {
        val s = sp(ctx)
        s.edit().apply {
            s.all.keys.filter { it.startsWith(K_SNAP) || it.startsWith(K_EMPTY) || it == K_LEGACY_SNAP || it == K_LEGACY_ACCT }
                .forEach { remove(it) }
            remove(K_PREVIEW_FOR)
        }.apply()
    }

    fun lockscreen(ctx: Context): Boolean = sp(ctx).getBoolean(K_LOCK, false)
    fun setLockscreen(ctx: Context, on: Boolean) = sp(ctx).edit().putBoolean(K_LOCK, on).apply()

    internal fun previewFor(ctx: Context): String? = sp(ctx).getString(K_PREVIEW_FOR, null)
    internal fun setPreviewFor(ctx: Context, accountId: String?) =
        sp(ctx).edit().putString(K_PREVIEW_FOR, accountId).apply()
}

private const val WORK_NAME = "cut_widget_refresh"
private const val WORK_NOW = "cut_widget_refresh_now"
private const val IN_ACCOUNT = "account_id"
private const val LOCK_NOTIF_ID = 7711

private fun connected() =
    Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

/** Periodic background refresh (WorkManager minimum: 15 min) — the fallback behind FCM-driven
 *  and in-app updates (the widget's updatePeriodMillis is 0). Idempotent. */
fun scheduleWidgetRefresh(ctx: Context) {
    val req = PeriodicWorkRequestBuilder<WidgetRefreshWorker>(15, TimeUnit.MINUTES)
        .setConstraints(connected()).build()
    WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(WORK_NAME, ExistingPeriodicWorkPolicy.KEEP, req)
}

/** Fire a one-shot refresh now (widget added, app opened, refresh tapped, push received).
 *  [accountId] limits the fetch to one account; null = every account a widget or the app shows. */
fun refreshWidgetsNow(ctx: Context, accountId: String? = null) {
    val req = OneTimeWorkRequestBuilder<WidgetRefreshWorker>()
        .setConstraints(connected())
        .apply { if (accountId != null) setInputData(workDataOf(IN_ACCOUNT to accountId)) }
        .build()
    // One pending run per account is enough; taps while one is queued don't pile up fetches.
    WorkManager.getInstance(ctx).enqueueUniqueWork(
        WORK_NOW + (accountId?.let { ":$it" } ?: ""),
        ExistingWorkPolicy.KEEP,
        req,
    )
}

/** Redraw the widgets (only those whose content changed) + the lock-screen notification from
 *  whatever is cached (no network). */
suspend fun redrawWidgets(ctx: Context) {
    runCatching { updateWidgets(ctx) }.onFailure { if (it is CancellationException) throw it }
    updateLockNotification(ctx)
}

/** A fresh snapshot arrived for [accountId] (app poll, worker): cache it and redraw what changed. */
suspend fun onSnapshot(ctx: Context, accountId: String, json: String) {
    WidgetData.saveSnap(ctx, accountId, json)
    redrawWidgets(ctx)
    WidgetPreviews.maybePublish(ctx)
}

/**
 * Update each widget instance whose drawn content would change, and only those. The fingerprint
 * of what an instance shows (its account's numbers, levels, absolute times, staleness, label) is
 * kept in its Glance state; writing a new one there is also what makes a still-running Glance
 * session recompose and re-read the cache.
 */
internal suspend fun updateWidgets(ctx: Context) {
    val ids = GlanceAppWidgetManager(ctx).getGlanceIds(UsageWidget::class.java)
    if (ids.isEmpty()) return
    val widget = UsageWidget()
    val now = System.currentTimeMillis()
    for (id in ids) {
        val state = getAppWidgetState(ctx, PreferencesGlanceStateDefinition, id)
        val fp = WidgetModel.load(ctx, state[KEY_ACCOUNT], now).fingerprint()
        if (state[KEY_RENDERED] == fp) continue
        updateAppWidgetState(ctx, id) { it[KEY_RENDERED] = fp }
        widget.update(ctx, id)
    }
}

/** The accounts worth fetching: the app's active one plus any a widget instance is set to. */
private suspend fun accountsInUse(ctx: Context, all: List<Prefs.Account>): List<Prefs.Account> {
    val wanted = HashSet<String>()
    Prefs.activeId(ctx)?.let { wanted += it }
    runCatching {
        GlanceAppWidgetManager(ctx).getGlanceIds(UsageWidget::class.java).forEach { id ->
            getAppWidgetState(ctx, PreferencesGlanceStateDefinition, id)[KEY_ACCOUNT]?.let { wanted += it }
        }
    }.onFailure { if (it is CancellationException) throw it }
    return all.filter { it.pairing.accountId in wanted }
}

/** Fetch the latest snapshots, cache them, and redraw what changed. Without a pairing it clears
 *  the cache and redraws into "Pair your desktop"; on a fetch error it keeps the last values
 *  (which turn stale on their own via the snapshot's age). */
suspend fun refreshWidgets(ctx: Context, onlyAccount: String? = null) {
    val all = Prefs.all(ctx)
    if (all.isEmpty()) {
        WidgetData.clearAll(ctx)
        redrawWidgets(ctx)
        return
    }
    val targets = if (onlyAccount != null) all.filter { it.pairing.accountId == onlyAccount } else accountsInUse(ctx, all)
    for (a in targets) {
        val id = a.pairing.accountId
        val js = try {
            RelayClient(a.pairing).fetchSnapshot()
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            continue
        }
        // The account may have been removed while the request was in flight.
        if (Prefs.all(ctx).none { it.pairing.accountId == id }) continue
        if (js == null) {
            WidgetData.markEmpty(ctx, id)
            continue
        }
        WidgetData.saveSnap(ctx, id, js)
        if (a.label.isNullOrBlank()) {
            runCatching { Snap.parse(js).org }.getOrNull()?.takeIf { it.isNotBlank() }?.let { Prefs.setLabel(ctx, id, it) }
        }
    }
    redrawWidgets(ctx)
    WidgetPreviews.maybePublish(ctx)
}

class WidgetRefreshWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        refreshWidgets(applicationContext, inputData.getString(IN_ACCOUNT))
        // The relay forgets push tokens after 7 days unless re-sent; re-send at most daily so
        // alerts keep arriving even if the app itself isn't opened for a while.
        runCatching { PushTokens.registerIfDue(applicationContext) }
        return Result.success()
    }
}

/** Remove one paired account everywhere: drop its push token on the relay (best effort, in the
 *  background), forget it locally, clear its cache, and redraw (widgets set to it fall back to
 *  the app's account). */
fun removeAccountEverywhere(ctx: Context, accountId: String) {
    val app = ctx.applicationContext
    val pairing = Prefs.all(app).firstOrNull { it.pairing.accountId == accountId }?.pairing
    val token = Prefs.pushToken(app)
    Prefs.remove(app, accountId)
    WidgetData.clearSnap(app, accountId)
    // The generated picker preview may show this account: regenerate it for the remaining
    // active account, or fall back to the static preview when nothing is paired any more.
    if (WidgetData.previewFor(app) == accountId) WidgetData.setPreviewFor(app, null)
    if (Prefs.all(app).isEmpty()) WidgetPreviews.remove(app)
    AppScope.launch {
        if (pairing != null) unregisterToken(app, pairing, token)
        refreshWidgets(app)
    }
}

/** Unpair every account: drop push tokens, clear secrets + cache, cancel the ongoing
 *  notification, remove the generated picker preview, and redraw into the unpaired state. */
fun unpairEverywhere(ctx: Context) {
    val app = ctx.applicationContext
    val pairings = Prefs.all(app).map { it.pairing }
    val token = Prefs.pushToken(app)
    Prefs.clear(app)
    WidgetData.clearAll(app)
    WidgetPreviews.remove(app)
    NotificationManagerCompat.from(app).cancel(LOCK_NOTIF_ID)
    AppScope.launch {
        pairings.forEach { unregisterToken(app, it, token) }
        redrawWidgets(app)
    }
}

private suspend fun unregisterToken(ctx: Context, pairing: Pairing, knownToken: String?) {
    if (knownToken != null) runCatching { RelayClient(pairing).unregisterPushToken(knownToken) }
    else PushTokens.unregister(ctx, pairing)
}

/** Ongoing, lock-screen-visible notification with the current usage — the phone's
 *  "lock-screen widget". Cancelled when the toggle is off, when unpaired, or when it can't post. */
@SuppressLint("MissingPermission")   // checked by Notifications.canPost
fun updateLockNotification(ctx: Context) {
    val nm = NotificationManagerCompat.from(ctx)
    if (!WidgetData.lockscreen(ctx) || !Prefs.isPaired(ctx) || !Notifications.canPost(ctx)) {
        nm.cancel(LOCK_NOTIF_ID); return
    }
    val snap = WidgetData.loadSnap(ctx)?.let { runCatching { Snap.parse(it) }.getOrNull() }
    if (snap == null) { nm.cancel(LOCK_NOTIF_ID); return }

    Notifications.createChannels(ctx)
    val five = snap.fiveHour
    val week = snap.sevenDay
    val ctxp = snap.ctxPct?.toInt()
    val reset = five?.resetsAt?.let { " · resets ${fmtClock(it)}" } ?: ""
    val title = (accountPrefix(ctx) ?: "Claude") + " · 5h ${five?.pct?.toInt() ?: "–"}%$reset"
    val n = NotificationCompat.Builder(ctx, Notifications.CHANNEL_LOCK)
        .setSmallIcon(R.drawable.ic_stat_usage)
        .setContentTitle(title)
        .setContentText("Weekly ${week?.pct?.toInt() ?: "–"}%   ·   Context ${ctxp ?: "–"}%")
        .setContentIntent(Notifications.openAppIntent(ctx, requestCode = 2))
        .setOngoing(true)
        .setOnlyAlertOnce(true)
        .setShowWhen(true)
        .setWhen(if (snap.updatedAt > 0) snap.updatedAt * 1000 else System.currentTimeMillis())
        .setPriority(NotificationCompat.PRIORITY_LOW)
        .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
        .build()
    nm.notify(LOCK_NOTIF_ID, n)
}

/** The active account's label when more than one account is paired (else null). */
internal fun accountPrefix(ctx: Context): String? {
    val all = Prefs.all(ctx)
    if (all.size < 2) return null
    val id = Prefs.activeId(ctx)
    return all.firstOrNull { it.pairing.accountId == id }?.label?.ifBlank { null }
}
