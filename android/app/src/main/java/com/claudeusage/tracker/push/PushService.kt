package com.claudeusage.tracker.push

import android.annotation.SuppressLint
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.claudeusage.tracker.Crypto
import com.claudeusage.tracker.Notifications
import com.claudeusage.tracker.Prefs
import com.claudeusage.tracker.R
import com.claudeusage.tracker.widget.refreshWidgetsNow
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import org.json.JSONObject

/**
 * Receives FCM **data** messages (never notification messages, so Google never sees
 * plaintext), decrypts them with the pairing key, posts a local notification, and refreshes the
 * home-screen widgets for that account.
 */
class PushService : FirebaseMessagingService() {

    override fun onNewToken(token: String) {
        // Register the new token with every paired account so each desktop can push this phone.
        PushTokens.registerAsync(this, token)
    }

    override fun onMessageReceived(message: RemoteMessage) {
        val nonce = message.data["nonce"] ?: return
        val ct = message.data["ct"] ?: return
        // The push doesn't say which account it's for — try each paired key until one decrypts.
        val accounts = Prefs.all(this)
        var plain: String? = null
        var label: String? = null
        var accountId: String? = null
        for (a in accounts) {
            val s = Crypto.openString(a.pairing.e2eeKeyB64, nonce, ct)
            if (s != null) { plain = s; label = a.label; accountId = a.pairing.accountId; break }
        }
        if (plain == null) return
        // Every push is a usage change on that desktop (limit ahead, reached, refilled…): refresh
        // its widgets now instead of waiting for the 15-minute fallback. The worker fetches the
        // snapshot and redraws only the widget instances whose content changed.
        refreshWidgetsNow(this, accountId)
        val o = runCatching { JSONObject(plain) }.getOrNull() ?: return
        val title = o.optString("title", "Claude Usage")
        showNotification(
            title = if (accounts.size > 1 && !label.isNullOrBlank()) "$label · $title" else title,
            body = o.optString("body", ""),
            tag = o.optString("tag", ""),
        )
    }

    @SuppressLint("MissingPermission")   // checked by Notifications.canPost
    private fun showNotification(title: String, body: String, tag: String) {
        if (!Notifications.canPost(this)) return

        val n = NotificationCompat.Builder(this, Notifications.CHANNEL_ALERTS)
            .setSmallIcon(R.drawable.ic_stat_usage)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(Notifications.openAppIntent(this, requestCode = 1))
            .build()
        val id = (if (tag.isNotEmpty()) tag else title).hashCode()
        NotificationManagerCompat.from(this).notify(id, n)
    }
}
