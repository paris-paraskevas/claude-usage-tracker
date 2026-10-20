package com.claudeusage.tracker

import android.util.Base64
import com.goterl.lazysodium.LazySodiumAndroid
import com.goterl.lazysodium.SodiumAndroid
import com.goterl.lazysodium.interfaces.SecretBox

/**
 * libsodium crypto_secretbox (XSalsa20-Poly1305) — the exact scheme PyNaCl uses on
 * the desktop. Keys/nonces/ciphertext are base64 (standard) per docs/REMOTE.md.
 * The phone only decrypts (snapshots + pushes); it never sends anything to the desktop.
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
}
