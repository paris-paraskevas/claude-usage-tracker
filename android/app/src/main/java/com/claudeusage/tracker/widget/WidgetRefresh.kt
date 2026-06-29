package com.claudeusage.tracker.widget

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.glance.appwidget.updateAll
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.claudeusage.tracker.Prefs
import com.claudeusage.tracker.R
import com.claudeusage.tracker.RelayClient
import com.claudeusage.tracker.Snap
import java.util.concurrent.TimeUnit

/** Small on-device cache shared by the widget + the ongoing notification (no secrets — the
 *  pairing keys stay in [Prefs]'s EncryptedSharedPreferences; this only holds usage numbers). */
object WidgetData {
    private const val FILE = "cut_widget"
    private const val K_SNAP = "snap"
    private const val K_LOCK = "lockscreen"

    private fun sp(ctx: Context) = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)
    fun saveSnap(ctx: Context, json: String) = sp(ctx).edit().putString(K_SNAP, json).apply()
    fun loadSnap(ctx: Context): String? = sp(ctx).getString(K_SNAP, null)
    fun lockscreen(ctx: Context): Boolean = sp(ctx).getBoolean(K_LOCK, false)
    fun setLockscreen(ctx: Context, on: Boolean) = sp(ctx).edit().putBoolean(K_LOCK, on).apply()
}

private const val WORK_NAME = "cut_widget_refresh"
private const val LOCK_CHANNEL = "usage_persistent"
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

/** Fetch the latest snapshot, cache it, redraw the widget, and refresh the ongoing notification.
 *  No-ops cleanly without a pairing; keeps the last value on a fetch error. */
suspend fun refreshWidgets(ctx: Context) {
    val p = Prefs.load(ctx) ?: return
    val js = runCatching { RelayClient(p).fetchSnapshot() }.getOrNull()
    if (js != null) WidgetData.saveSnap(ctx, js)
    runCatching { UsageWidget().updateAll(ctx) }
    updateLockNotification(ctx)
}

class WidgetRefreshWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        refreshWidgets(applicationContext)
        return Result.success()
    }
}

/** Ongoing, lock-screen-visible notification with the current usage — the phone's
 *  "lock-screen widget". Cancelled when the toggle is off. */
fun updateLockNotification(ctx: Context) {
    val nm = NotificationManagerCompat.from(ctx)
    if (!WidgetData.lockscreen(ctx)) { nm.cancel(LOCK_NOTIF_ID); return }
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
        ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS)
        != PackageManager.PERMISSION_GRANTED
    ) return
    val snap = WidgetData.loadSnap(ctx)?.let { runCatching { Snap.parse(it) }.getOrNull() } ?: return

    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        val ch = NotificationChannel(LOCK_CHANNEL, "Usage on lock screen", NotificationManager.IMPORTANCE_LOW)
        ch.setShowBadge(false)
        (ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).createNotificationChannel(ch)
    }
    fun pct(key: String) = snap.wins.firstOrNull { it.key == key }?.pct?.toInt()
    val five = pct("five_hour")
    val week = pct("seven_day")
    val ctxp = snap.ctxPct?.toInt()
    val n = NotificationCompat.Builder(ctx, LOCK_CHANNEL)
        .setSmallIcon(R.drawable.ic_stat_usage)
        .setContentTitle("Claude · 5h ${five ?: "–"}%")
        .setContentText("Weekly ${week ?: "–"}%   ·   Context ${ctxp ?: "–"}%")
        .setOngoing(true)
        .setOnlyAlertOnce(true)
        .setShowWhen(false)
        .setPriority(NotificationCompat.PRIORITY_LOW)
        .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
        .build()
    nm.notify(LOCK_NOTIF_ID, n)
}
