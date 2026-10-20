package com.claudeusage.tracker.widget

import android.annotation.SuppressLint
import android.content.Context
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.glance.appwidget.updateAll
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.claudeusage.tracker.AppScope
import com.claudeusage.tracker.Notifications
import com.claudeusage.tracker.Pairing
import com.claudeusage.tracker.Prefs
import com.claudeusage.tracker.R
import com.claudeusage.tracker.RelayClient
import com.claudeusage.tracker.Snap
import com.claudeusage.tracker.push.PushTokens
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** Small on-device cache shared by the widget + the ongoing notification. Usage numbers only:
 *  the mirrored conversation (transcripts), the account email and the sparkline history are
 *  stripped before caching (see [stripSensitive]), and pairing keys live in [Prefs]'s
 *  EncryptedSharedPreferences — so nothing sensitive sits in these plaintext prefs. */
object WidgetData {
    private const val FILE = "cut_widget"
    private const val K_SNAP = "snap"
    private const val K_SNAP_ACCT = "snap_acct"   // which account the cached snapshot belongs to
    private const val K_LOCK = "lockscreen"

    private fun sp(ctx: Context) = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun saveSnap(ctx: Context, accountId: String, json: String) =
        sp(ctx).edit().putString(K_SNAP, stripSensitive(json)).putString(K_SNAP_ACCT, accountId).apply()

    /** The widget + lock-screen notification render only usage numbers, so drop the mirrored
     *  conversation and the account email before this string lands in plaintext prefs. */
    private fun stripSensitive(json: String): String = runCatching {
        val o = JSONObject(json)
        o.remove("transcripts"); o.remove("transcript")
        o.remove("history")
        o.optJSONObject("account")?.remove("email")
        o.toString()
    }.getOrDefault("{}")

    /** The cached snapshot, but only if it belongs to the currently-active account. */
    fun loadSnap(ctx: Context): String? {
        val active = Prefs.activeId(ctx) ?: return null
        val s = sp(ctx)
        val owner = s.getString(K_SNAP_ACCT, null)
        // Caches written before v0.3 carry no owner; trust them only with a single account.
        if (owner == null && Prefs.all(ctx).size > 1) return null
        if (owner != null && owner != active) return null
        return s.getString(K_SNAP, null)
    }

    /** Drop the cached snapshot (unpair / the cached account was removed). */
    fun clearSnap(ctx: Context) = sp(ctx).edit().remove(K_SNAP).remove(K_SNAP_ACCT).apply()

    fun lockscreen(ctx: Context): Boolean = sp(ctx).getBoolean(K_LOCK, false)
    fun setLockscreen(ctx: Context, on: Boolean) = sp(ctx).edit().putBoolean(K_LOCK, on).apply()
}

private const val WORK_NAME = "cut_widget_refresh"
private const val LOCK_NOTIF_ID = 7711

private fun connected() =
    Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

/** Periodic background refresh (min 15 min). Idempotent. */
fun scheduleWidgetRefresh(ctx: Context) {
    val req = PeriodicWorkRequestBuilder<WidgetRefreshWorker>(15, TimeUnit.MINUTES)
        .setConstraints(connected()).build()
    WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(WORK_NAME, ExistingPeriodicWorkPolicy.KEEP, req)
}

/** Fire a one-shot refresh now (widget added, app opened, etc.). */
fun refreshWidgetsNow(ctx: Context) {
    WorkManager.getInstance(ctx).enqueue(
        OneTimeWorkRequestBuilder<WidgetRefreshWorker>().setConstraints(connected()).build())
}

/** Redraw the widget + lock-screen notification from whatever is cached (no network). */
suspend fun redrawWidgets(ctx: Context) {
    runCatching { UsageWidget().updateAll(ctx) }
    updateLockNotification(ctx)
}

/** Fetch the latest snapshot, cache it, redraw the widget, and refresh the ongoing notification.
 *  Without a pairing it clears the cache, cancels the notification and redraws the widget into
 *  its "pair your phone" state; on a fetch error it keeps the last value. */
suspend fun refreshWidgets(ctx: Context) {
    val p = Prefs.load(ctx)
    if (p == null) {
        WidgetData.clearSnap(ctx)
        redrawWidgets(ctx)
        return
    }
    val js = runCatching { RelayClient(p).fetchSnapshot() }.getOrNull()
    // Only cache if the account is still the active one (the user may have switched mid-flight).
    if (js != null && Prefs.activeId(ctx) == p.accountId) WidgetData.saveSnap(ctx, p.accountId, js)
    redrawWidgets(ctx)
}

class WidgetRefreshWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        refreshWidgets(applicationContext)
        // The relay forgets push tokens after 7 days unless re-sent; re-send at most daily so
        // alerts keep arriving even if the app itself isn't opened for a while.
        runCatching { PushTokens.registerIfDue(applicationContext) }
        return Result.success()
    }
}

/** Remove one paired account everywhere: drop its push token on the relay (best effort, in the
 *  background), forget it locally, clear the cache if it was showing, and redraw. */
fun removeAccountEverywhere(ctx: Context, accountId: String) {
    val app = ctx.applicationContext
    val pairing = Prefs.all(app).firstOrNull { it.pairing.accountId == accountId }?.pairing
    val wasActive = Prefs.activeId(app) == accountId
    val token = Prefs.pushToken(app)
    Prefs.remove(app, accountId)
    if (wasActive) WidgetData.clearSnap(app)
    AppScope.launch {
        if (pairing != null) unregisterToken(app, pairing, token)
        refreshWidgets(app)
    }
}

/** Unpair every account: drop push tokens, clear secrets + cache, cancel the ongoing
 *  notification, and redraw the widget into its unpaired state. */
fun unpairEverywhere(ctx: Context) {
    val app = ctx.applicationContext
    val pairings = Prefs.all(app).map { it.pairing }
    val token = Prefs.pushToken(app)
    Prefs.clear(app)
    WidgetData.clearSnap(app)
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

/** "14:30" today, else "Tue 09:00" — absolute, because widgets/notifications don't tick. */
internal fun fmtClock(epochMs: Long): String {
    val now = java.util.Calendar.getInstance()
    val then = java.util.Calendar.getInstance().apply { timeInMillis = epochMs }
    val sameDay = now.get(java.util.Calendar.YEAR) == then.get(java.util.Calendar.YEAR) &&
        now.get(java.util.Calendar.DAY_OF_YEAR) == then.get(java.util.Calendar.DAY_OF_YEAR)
    val locale = java.util.Locale.getDefault()
    // "j" = the locale's preferred hour cycle (12h/24h).
    val pattern = android.text.format.DateFormat.getBestDateTimePattern(locale, if (sameDay) "jmm" else "EEEjmm")
    return java.text.SimpleDateFormat(pattern, locale).format(java.util.Date(epochMs))
}
