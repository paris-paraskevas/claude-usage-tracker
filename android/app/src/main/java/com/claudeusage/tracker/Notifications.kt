package com.claudeusage.tracker

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.claudeusage.tracker.ui.MainActivity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

/** Notification channels, permission checks and the tap-to-open intent, shared by the push
 *  alerts (push/PushService) and the ongoing lock-screen notification (widget/WidgetRefresh). */
object Notifications {
    const val CHANNEL_ALERTS = "usage_alerts"
    const val CHANNEL_LOCK = "usage_persistent"

    fun createChannels(ctx: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ALERTS, "Usage alerts", NotificationManager.IMPORTANCE_HIGH)
                .apply { description = "Claude usage thresholds, burn-rate, context, and service status" }
        )
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_LOCK, "Usage on lock screen", NotificationManager.IMPORTANCE_LOW)
                .apply {
                    description = "Ongoing 5h / weekly / context usage (Settings → Lock screen)"
                    setShowBadge(false)
                }
        )
    }

    /** True when this app may post notifications (runtime permission on 13+, and the user
     *  hasn't switched notifications off for the app). */
    fun canPost(ctx: Context): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) return false
        return NotificationManagerCompat.from(ctx).areNotificationsEnabled()
    }

    /** Whether the runtime permission still needs to be requested (Android 13+ only). */
    fun needsRuntimePermission(ctx: Context): Boolean =
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED

    /** Tapping a notification opens (or brings back) the app. */
    fun openAppIntent(ctx: Context, requestCode: Int = 0): PendingIntent {
        val intent = Intent(ctx, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        return PendingIntent.getActivity(
            ctx, requestCode, intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
    }
}

/** Process-wide scope for fire-and-forget work that must outlive a screen (push-token
 *  registration, unpair clean-up). Failures are swallowed per task by the callers. */
object AppScope : CoroutineScope by CoroutineScope(SupervisorJob() + Dispatchers.IO)
