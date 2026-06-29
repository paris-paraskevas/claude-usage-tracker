package com.claudeusage.tracker

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import org.json.JSONArray
import org.json.JSONObject

/**
 * Pairing secrets at rest, in EncryptedSharedPreferences (AES-256).
 *
 * Holds a LIST of paired desktops (e.g. personal on a laptop + work on a PC) plus the
 * currently-active one, so the phone can switch between accounts. The legacy single-pairing
 * layout is migrated transparently on first read. `load()`/`save()` keep working for callers
 * that only care about the active account (RelayClient, the widget, FCM registration).
 */
object Prefs {
    private const val FILE = "cut_secure_prefs"
    private const val K_ACCOUNTS = "accounts"   // JSON array of {u,a,t,k,label}
    private const val K_ACTIVE = "active"        // active accountId

    /** A paired desktop: its [pairing] secrets + an optional display [label] (defaults to org). */
    data class Account(val pairing: Pairing, val label: String?)

    private fun sp(ctx: Context): SharedPreferences {
        val key = MasterKey.Builder(ctx)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        return EncryptedSharedPreferences.create(
            ctx, FILE, key,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    }

    private fun toJson(list: List<Account>) = JSONArray().apply {
        list.forEach {
            put(JSONObject()
                .put("u", it.pairing.url).put("a", it.pairing.accountId)
                .put("t", it.pairing.readToken).put("k", it.pairing.e2eeKeyB64)
                .put("label", it.label ?: ""))
        }
    }.toString()

    private fun writeAll(ctx: Context, list: List<Account>) =
        sp(ctx).edit().putString(K_ACCOUNTS, toJson(list)).apply()

    /** All paired accounts, migrating the legacy single pairing on first read. */
    fun all(ctx: Context): List<Account> {
        val s = sp(ctx)
        val raw = s.getString(K_ACCOUNTS, null)
        if (raw != null) {
            return runCatching {
                val ja = JSONArray(raw)
                (0 until ja.length()).mapNotNull { i ->
                    val o = ja.getJSONObject(i)
                    val p = Pairing(o.optString("u"), o.optString("a"), o.optString("t"), o.optString("k"))
                    if (p.url.isBlank() || p.accountId.isBlank()) null
                    else Account(p, o.optString("label").ifBlank { null })
                }
            }.getOrDefault(emptyList())
        }
        // migrate the old single-pairing keys, if present
        val url = s.getString("url", null); val a = s.getString("account", null)
        val t = s.getString("token", null); val k = s.getString("key", null)
        if (url != null && a != null && t != null && k != null) {
            val list = listOf(Account(Pairing(url, a, t, k), null))
            s.edit().putString(K_ACCOUNTS, toJson(list)).putString(K_ACTIVE, a)
                .remove("url").remove("account").remove("token").remove("key").apply()
            return list
        }
        return emptyList()
    }

    fun isPaired(ctx: Context): Boolean = all(ctx).isNotEmpty()

    /** The active accountId, falling back to the first account (or null if none). */
    fun activeId(ctx: Context): String? {
        val list = all(ctx)
        val id = sp(ctx).getString(K_ACTIVE, null)
        return list.firstOrNull { it.pairing.accountId == id }?.pairing?.accountId
            ?: list.firstOrNull()?.pairing?.accountId
    }

    /** The active pairing (or null). Back-compat alias for single-account callers. */
    fun active(ctx: Context): Pairing? {
        val id = activeId(ctx)
        return all(ctx).firstOrNull { it.pairing.accountId == id }?.pairing
    }

    fun load(ctx: Context): Pairing? = active(ctx)

    fun setActive(ctx: Context, accountId: String) =
        sp(ctx).edit().putString(K_ACTIVE, accountId).apply()

    /** Add (or replace by accountId) a pairing and make it active. */
    fun add(ctx: Context, p: Pairing, label: String? = null) {
        val list = all(ctx).filter { it.pairing.accountId != p.accountId } + Account(p, label)
        writeAll(ctx, list)
        setActive(ctx, p.accountId)
    }

    fun save(ctx: Context, p: Pairing) = add(ctx, p)

    /** Set/refresh an account's display label (e.g. once we learn its org from a snapshot). */
    fun setLabel(ctx: Context, accountId: String, label: String?) {
        val list = all(ctx).map {
            if (it.pairing.accountId == accountId) it.copy(label = label?.ifBlank { null }) else it
        }
        writeAll(ctx, list)
    }

    /** Remove one account; if it was active, fall back to whatever remains. */
    fun remove(ctx: Context, accountId: String) {
        val list = all(ctx).filter { it.pairing.accountId != accountId }
        writeAll(ctx, list)
        if (sp(ctx).getString(K_ACTIVE, null) == accountId) {
            sp(ctx).edit().putString(K_ACTIVE, list.firstOrNull()?.pairing?.accountId).apply()
        }
    }

    /** Forget every account. */
    fun clear(ctx: Context) {
        sp(ctx).edit().clear().apply()
    }
}
