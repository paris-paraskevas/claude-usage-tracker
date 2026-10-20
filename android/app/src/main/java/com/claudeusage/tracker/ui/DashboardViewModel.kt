package com.claudeusage.tracker.ui

import android.app.Application
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.claudeusage.tracker.Pairing
import com.claudeusage.tracker.Prefs
import com.claudeusage.tracker.RelayClient
import com.claudeusage.tracker.Snap
import com.claudeusage.tracker.push.PushTokens
import com.claudeusage.tracker.widget.WidgetData
import com.claudeusage.tracker.widget.onSnapshot
import com.claudeusage.tracker.widget.redrawWidgets
import com.claudeusage.tracker.widget.removeAccountEverywhere
import com.claudeusage.tracker.widget.unpairEverywhere
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import java.io.IOException
import java.net.ConnectException
import java.net.SocketTimeoutException
import java.net.UnknownHostException

/**
 * Dashboard state that must survive rotation / configuration changes (it lives as long as the
 * activity's ViewModelStore): the paired accounts, the active one, the latest snapshot and the
 * last refresh error. On creation it seeds [snap] from the widget cache for the active account so
 * a cold start shows the last known numbers instead of "Waiting for your desktop".
 */
class DashboardViewModel(app: Application) : AndroidViewModel(app) {
    private val ctx: Application get() = getApplication()

    var accounts by mutableStateOf(Prefs.all(app))
        private set
    var activeId by mutableStateOf(Prefs.activeId(app))
        private set
    var snap by mutableStateOf<Snap?>(null)
        private set
    /** True while [snap] is the on-device cache, not yet confirmed by a live fetch. */
    var fromCache by mutableStateOf(false)
        private set
    /** The last refresh error (null after a successful fetch). */
    var error by mutableStateOf<String?>(null)
        private set
    /** Epoch ms of the last successful relay round-trip (0 = none yet this session). */
    var lastOkAt by mutableLongStateOf(0L)
        private set
    var loading by mutableStateOf(false)
        private set

    init { seedFromCache() }

    private fun seedFromCache() {
        val cached = WidgetData.loadSnap(ctx)?.let { runCatching { Snap.parse(it) }.getOrNull() }
        snap = cached
        fromCache = cached != null
    }

    /** Re-read the pairings (they may have changed while another screen was showing). */
    fun syncFromPrefs() {
        val newActive = Prefs.activeId(ctx)
        accounts = Prefs.all(ctx)
        if (newActive != activeId) {
            activeId = newActive
            error = null
            seedFromCache()
        }
    }

    /** Fetch the active account's snapshot. Safe to call repeatedly (poll loop + Retry). */
    suspend fun reload() {
        val p = Prefs.active(ctx) ?: return          // always the currently-active account
        val reqId = p.accountId
        loading = true
        try {
            val js = RelayClient(p).fetchSnapshot()
            // If the user switched accounts while this request was in flight, drop the result so
            // the previous account's data can't bleed into the now-active one.
            if (Prefs.activeId(ctx) != reqId) return
            lastOkAt = System.currentTimeMillis()
            error = null
            if (js != null) {                          // null = desktop hasn't synced yet: keep waiting state
                val parsed = Snap.parse(js)
                snap = parsed
                fromCache = false
                // learn this account's label from its org so the switcher reads nicely
                val cur = Prefs.all(ctx).firstOrNull { it.pairing.accountId == reqId }
                if (cur?.label.isNullOrBlank() && parsed.org.isNotBlank()) {
                    Prefs.setLabel(ctx, reqId, parsed.org)
                    accounts = Prefs.all(ctx)
                }
                // Keep the widgets (only instances whose content changed are redrawn) and the
                // lock-screen notification current.
                onSnapshot(ctx, reqId, js)
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            if (Prefs.activeId(ctx) == reqId) error = friendly(e)
        } finally {
            loading = false
        }
    }

    fun refresh() {
        viewModelScope.launch { reload() }
    }

    fun switchTo(id: String) {
        if (id == activeId) return
        Prefs.setActive(ctx, id)
        activeId = id
        error = null
        seedFromCache()                                 // nothing cached for it yet → waiting state
        refresh()
        // Widgets that follow the app's account switch with it (from the cache, right away).
        viewModelScope.launch { redrawWidgets(ctx) }
    }

    /** A new desktop was paired (QR scan from the dashboard, or a confirmed deep link). */
    fun added(p: Pairing) {
        Prefs.add(ctx, p)
        accounts = Prefs.all(ctx)
        activeId = p.accountId
        error = null
        seedFromCache()
        refresh()
        PushTokens.registerAsync(ctx)                   // let the new desktop push this phone too
    }

    /** Remove one account. Returns true when nothing is paired any more. */
    fun remove(id: String): Boolean {
        removeAccountEverywhere(ctx, id)
        accounts = Prefs.all(ctx)
        val newActive = Prefs.activeId(ctx)
        if (accounts.isEmpty()) {
            activeId = null; snap = null; error = null; fromCache = false
            return true
        }
        if (newActive != activeId) {
            activeId = newActive
            error = null
            seedFromCache()
            refresh()
        }
        return false
    }

    /** Forget every account (push tokens dropped, cache cleared, lock-screen card cancelled). */
    fun unpairAll() {
        unpairEverywhere(ctx)
        accounts = emptyList(); activeId = null; snap = null; error = null; fromCache = false
    }

    private fun friendly(e: Exception): String = when (e) {
        is UnknownHostException, is ConnectException -> "No connection to the relay"
        is SocketTimeoutException -> "The relay took too long to answer"
        is IOException -> e.message ?: "Couldn't reach the relay"
        is org.json.JSONException -> "Couldn't read the latest snapshot"
        else -> e.message ?: "Couldn't refresh"
    }
}
