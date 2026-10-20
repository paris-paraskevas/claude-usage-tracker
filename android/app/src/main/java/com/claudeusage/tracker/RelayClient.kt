package com.claudeusage.tracker

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Talks to the zero-knowledge relay. Fetches and decrypts the snapshot; registers the
 *  FCM push token. All payloads are end-to-end encrypted (see Crypto / docs/REMOTE.md). */
class RelayClient(private val p: Pairing) {
    private val http get() = SHARED

    companion object {
        // One client (connection pool + dispatcher) for the whole process; RelayClient itself is
        // created per call site, so a per-instance OkHttpClient would leak pools.
        private val SHARED: OkHttpClient by lazy {
            OkHttpClient.Builder().callTimeout(15, TimeUnit.SECONDS).build()
        }
    }

    private fun base() = "${p.url.trimEnd('/')}/v1/acct/${p.accountId}"

    /** The decrypted snapshot JSON string, or null if the relay has nothing yet (204). */
    suspend fun fetchSnapshot(): String? = withContext(Dispatchers.IO) {
        val req = Request.Builder()
            .url("${base()}/snapshot")
            .header("Authorization", "Bearer ${p.readToken}")
            .get().build()
        http.newCall(req).execute().use { resp ->
            // 204 = account exists but no snapshot; 404 = desktop hasn't pushed yet.
            // Both mean "not synced yet" — surface a friendly waiting state, not an error.
            if (resp.code == 204 || resp.code == 404) return@withContext null
            if (!resp.isSuccessful) throw IOException(
                when (resp.code) {
                    401, 403 -> "The relay rejected this pairing — re-pair from the desktop QR"
                    429 -> "The relay is rate-limiting requests — try again shortly"
                    in 500..599 -> "The relay is having trouble (HTTP ${resp.code})"
                    else -> "Relay error (HTTP ${resp.code})"
                }
            )
            val body = resp.body?.string() ?: return@withContext null
            val o = JSONObject(body)
            Crypto.openString(p.e2eeKeyB64, o.getString("nonce"), o.getString("ct"))
                ?: throw IOException("decryption failed — re-pair the device")
        }
    }

    /** Add this phone's FCM token to the account's push set (relay keeps it ~7 days, so it is
     *  re-sent at most daily from the background refresh — see push/PushTokens.kt). */
    suspend fun registerPushToken(token: String): Boolean = pushToken("PUT", token)

    /** Remove this phone's FCM token from the account's push set (unpair / remove account). */
    suspend fun unregisterPushToken(token: String): Boolean = pushToken("DELETE", token)

    private suspend fun pushToken(method: String, token: String): Boolean = withContext(Dispatchers.IO) {
        val payload = JSONObject().put("token", token).put("platform", "android").toString()
        val req = Request.Builder()
            .url("${base()}/push-token")
            .header("Authorization", "Bearer ${p.readToken}")
            .method(method, payload.toRequestBody("application/json".toMediaType()))
            .build()
        runCatching { http.newCall(req).execute().use { it.isSuccessful } }.getOrDefault(false)
    }
}
