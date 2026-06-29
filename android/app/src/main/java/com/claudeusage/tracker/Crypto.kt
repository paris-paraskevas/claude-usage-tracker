package com.claudeusage.tracker

import android.util.Base64
import com.goterl.lazysodium.LazySodiumAndroid
import com.goterl.lazysodium.SodiumAndroid
import com.goterl.lazysodium.interfaces.SecretBox

/**
 * libsodium crypto_secretbox (XSalsa20-Poly1305) — the exact scheme PyNaCl uses on
 * the desktop. Keys/nonces/ciphertext are base64 (standard) per docs/REMOTE.md.
 * The phone decrypts snapshots/pushes and (for remote-control) seals commands it sends.
 */
object Crypto {
    private val ls = LazySodiumAndroid(SodiumAndroid())

    /** Decrypt a relay blob; returns the UTF-8 plaintext bytes, or null on any failure. */
    fun open(e2eeKeyB64: String, nonceB64: String, ctB64: String): ByteArray? {
        return try {
            val key = Base64.decode(e2eeKeyB64, Base64.DEFAULT)
            val nonce = Base64.decode(nonceB64, Base64.DEFAULT)
            val cipher = Base64.decode(ctB64, Base64.DEFAULT)
            if (key.size != SecretBox.KEYBYTES || nonce.size != SecretBox.NONCEBYTES) return null
            if (cipher.size < SecretBox.MACBYTES) return null
            val msg = ByteArray(cipher.size - SecretBox.MACBYTES)
            val ok = ls.cryptoSecretBoxOpenEasy(msg, cipher, cipher.size.toLong(), nonce, key)
            if (ok) msg else null
        } catch (e: Exception) {
            null
        }
    }

    fun openString(e2eeKeyB64: String, nonceB64: String, ctB64: String): String? =
        open(e2eeKeyB64, nonceB64, ctB64)?.toString(Charsets.UTF_8)

    /** Encrypt plaintext → (nonceB64, ctB64) with crypto_secretbox; matches PyNaCl on the
     *  desktop (random nonce, MAC||cipher). Returns null on failure. */
    fun seal(e2eeKeyB64: String, plaintext: ByteArray): Pair<String, String>? {
        return try {
            val key = Base64.decode(e2eeKeyB64, Base64.DEFAULT)
            if (key.size != SecretBox.KEYBYTES) return null
            val nonce = ls.nonce(SecretBox.NONCEBYTES)
            val cipher = ByteArray(plaintext.size + SecretBox.MACBYTES)
            val ok = ls.cryptoSecretBoxEasy(cipher, plaintext, plaintext.size.toLong(), nonce, key)
            if (!ok) return null
            Pair(Base64.encodeToString(nonce, Base64.NO_WRAP),
                 Base64.encodeToString(cipher, Base64.NO_WRAP))
        } catch (e: Exception) {
            null
        }
    }

    fun sealString(e2eeKeyB64: String, plaintext: String): Pair<String, String>? =
        seal(e2eeKeyB64, plaintext.toByteArray(Charsets.UTF_8))
}
