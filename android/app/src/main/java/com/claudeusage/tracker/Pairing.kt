package com.claudeusage.tracker

import android.util.Base64
import org.json.JSONObject

/**
 * The pairing payload encoded in the desktop's QR:
 *   cutpair1:<base64url(JSON{ "u":relayUrl, "a":accountId, "t":readToken, "k":e2eeKey })>
 */
data class Pairing(
    val url: String,
    val accountId: String,
    val readToken: String,
    val e2eeKeyB64: String,
) {
    companion object {
        fun parse(text: String?): Pairing? {
            if (text == null || !text.startsWith("cutpair1:")) return null
            return try {
                var raw = text.removePrefix("cutpair1:")
                // restore base64url padding
                val pad = (4 - raw.length % 4) % 4
                raw += "=".repeat(pad)
                val json = String(Base64.decode(raw, Base64.URL_SAFE), Charsets.UTF_8)
                val o = JSONObject(json)
                val p = Pairing(
                    url = o.getString("u").trimEnd('/'),
                    accountId = o.getString("a"),
                    readToken = o.getString("t"),
                    e2eeKeyB64 = o.getString("k"),
                )
                // Require HTTPS — the relay carries the bearer read-token, which would be
                // sniffable over plain HTTP (the payload itself stays E2EE). The hosted relay is
                // HTTPS; a self-hoster should front their relay with TLS.
                if (!p.url.startsWith("https://")) return null
                if (p.url.isBlank() || p.accountId.isBlank() || p.readToken.isBlank() || p.e2eeKeyB64.isBlank()) null else p
            } catch (e: Exception) {
                null
            }
        }
    }
}
