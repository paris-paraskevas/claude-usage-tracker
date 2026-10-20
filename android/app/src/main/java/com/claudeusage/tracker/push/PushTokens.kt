package com.claudeusage.tracker.push

import android.content.Context
import com.claudeusage.tracker.AppScope
import com.claudeusage.tracker.Pairing
import com.claudeusage.tracker.Prefs
import com.claudeusage.tracker.RelayClient
import com.google.firebase.messaging.FirebaseMessaging
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume

/**
 * FCM token bookkeeping against the relay.
 *
 * The relay keeps each account's push-token set for 7 days (KV TTL) and only refreshes that TTL
 * when the token is PUT again — so a phone that isn't opened for a week would silently stop
 * getting alerts. [registerIfDue] re-sends at most once a day from the periodic widget refresh;
 * [registerAll] runs on pairing / app open / token rotation; [unregister] removes this phone
 * from an account's push set when the user removes the account or unpairs.
 *
 * Everything no-ops if Firebase isn't configured (no google-services.json at build time).
 */
object PushTokens {
    private const val RESEND_EVERY_MS = 24L * 60 * 60 * 1000

    /** The current FCM token, or null when Firebase isn't set up / unavailable. */
    suspend fun currentToken(): String? = try {
        suspendCancellableCoroutine<String?> { cont ->
            FirebaseMessaging.getInstance().token
                .addOnSuccessListener { t -> if (cont.isActive) cont.resume(t) }
                .addOnFailureListener { if (cont.isActive) cont.resume(null) }
                .addOnCanceledListener { if (cont.isActive) cont.resume(null) }
        }
    } catch (_: Exception) {
        null   // Firebase not initialised — push disabled, viewing still works.
    }

    /** Register [token] (or the current one) with every paired account. Records the send time
     *  only when at least one relay accepted it, so a failed attempt is retried next refresh. */
    suspend fun registerAll(ctx: Context, token: String? = null): Boolean {
        val accounts = Prefs.all(ctx)
        if (accounts.isEmpty()) return false
        val t = token ?: currentToken() ?: return false
        var any = false
        for (a in accounts) {
            if (runCatching { RelayClient(a.pairing).registerPushToken(t) }.getOrDefault(false)) any = true
        }
        if (any) Prefs.setPushTokenSent(ctx, t, System.currentTimeMillis())
        return any
    }

    /** Re-send the token if the last successful send is older than a day (or the token changed). */
    suspend fun registerIfDue(ctx: Context) {
        if (!Prefs.isPaired(ctx)) return
        val last = Prefs.pushTokenSentAt(ctx)
        val now = System.currentTimeMillis()
        if (last in 1..now && now - last < RESEND_EVERY_MS) {
            val cur = currentToken() ?: return
            if (cur == Prefs.pushToken(ctx)) return
            registerAll(ctx, cur)
            return
        }
        registerAll(ctx)
    }

    /** Fire-and-forget [registerAll] (pairing, app open). */
    fun registerAsync(ctx: Context, token: String? = null) {
        val app = ctx.applicationContext
        AppScope.launch { runCatching { registerAll(app, token) } }
    }

    /** Best-effort: remove this phone's token from [pairing]'s push set on the relay. */
    suspend fun unregister(ctx: Context, pairing: Pairing) {
        val t = Prefs.pushToken(ctx) ?: currentToken() ?: return
        runCatching { RelayClient(pairing).unregisterPushToken(t) }
    }
}
