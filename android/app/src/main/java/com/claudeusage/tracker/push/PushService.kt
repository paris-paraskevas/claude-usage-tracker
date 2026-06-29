package com.claudeusage.tracker.push

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.claudeusage.tracker.Crypto
import com.claudeusage.tracker.Prefs
import com.claudeusage.tracker.R
import com.claudeusage.tracker.RelayClient
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * Receives FCM **data** messages (never notification messages, so Google never sees
 * plaintext), decrypts them with the pairing key, and posts a local notification.
 */
class PushService : FirebaseMessagingService() {

    override fun onNewToken(token: String) {
        // Register the token with every paired account so each desktop can push this phone.
        val accounts = Prefs.all(this)
        CoroutineScope(Dispatchers.IO).launch {
            accounts.forEach { a -> runCatching { RelayClient(a.pairing).registerPushToken(token) } }
        }
    }

    override fun onMessageReceived(message: RemoteMessage) {
        val nonce = message.data["nonce"] ?: return
        val ct = message.data["ct"] ?: return
        // The push doesn't say which account it's for — try each paired key until one decrypts.
        val accounts = Prefs.all(this)
        var plain: String? = null
        var label: String? = null
        for (a in accounts) {
            val s = Crypto.openString(a.pairing.e2eeKeyB64, nonce, ct)
            if (s != null) { plain = s; label = a.label; break }
        }
        if (plain == null) return
        val o = runCatching { JSONObject(plain) }.getOrNull() ?: return
        val title = o.optString("title", "Claude Usage")
        showNotification(
            title = if (accounts.size > 1 && !label.isNullOrBlank()) "$label · $title" else title,
            body = o.optString("body", ""),
            tag = o.optString("tag", ""),
        )
    }

    private fun showNotification(title: String, body: String, tag: String) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) return

        val n = NotificationCompat.Builder(this, "usage_alerts")
            .setSmallIcon(R.drawable.ic_stat_usage)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .build()
        val id = (if (tag.isNotEmpty()) tag else title).hashCode()
        NotificationManagerCompat.from(this).notify(id, n)
    }
}
