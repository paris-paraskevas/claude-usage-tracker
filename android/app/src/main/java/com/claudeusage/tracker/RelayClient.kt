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
    private val http = OkHttpClient.Builder()
        .callTimeout(15, TimeUnit.SECONDS)
        .build()

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
            if (!resp.isSuccessful) throw IOException("relay ${resp.code}")
            val body = resp.body?.string() ?: return@withContext null
            val o = JSONObject(body)
            Crypto.openString(p.e2eeKeyB64, o.getString("nonce"), o.getString("ct"))
                ?: throw IOException("decryption failed — re-pair the device")
        }
    }

    suspend fun registerPushToken(token: String): Boolean = withContext(Dispatchers.IO) {
        val payload = JSONObject().put("token", token).put("platform", "android").toString()
        val req = Request.Builder()
            .url("${base()}/push-token")
            .header("Authorization", "Bearer ${p.readToken}")
            .put(payload.toRequestBody("application/json".toMediaType()))
            .build()
        runCatching { http.newCall(req).execute().use { it.isSuccessful } }.getOrDefault(false)
    }

    /** Seal {type:"prompt", text, cwd?, session_id?} with the pairing key and enqueue it on the
     *  relay for the desktop to run (if armed). `sessionId` (+ `cwd`) tells the desktop which
     *  conversation to RESUME, so the reply has that session's full context and lands back in it;
     *  null lets the desktop use the most-recent session. Returns true on success. */
    suspend fun sendCommand(text: String, cwd: String? = null, sessionId: String? = null): Boolean = withContext(Dispatchers.IO) {
        val cmd = JSONObject().put("type", "prompt").put("text", text)
            .apply {
                if (!cwd.isNullOrBlank()) put("cwd", cwd)
                if (!sessionId.isNullOrBlank()) put("session_id", sessionId)
            }.toString()
        val sealed = Crypto.sealString(p.e2eeKeyB64, cmd) ?: return@withContext false
        val payload = JSONObject().put("v", 1).put("nonce", sealed.first).put("ct", sealed.second)
            .put("ts", System.currentTimeMillis() / 1000).toString()
        val req = Request.Builder()
            .url("${base()}/command")
            .header("Authorization", "Bearer ${p.readToken}")
            .put(payload.toRequestBody("application/json".toMediaType()))
            .build()
        runCatching { http.newCall(req).execute().use { it.isSuccessful } }.getOrDefault(false)
    }
}
