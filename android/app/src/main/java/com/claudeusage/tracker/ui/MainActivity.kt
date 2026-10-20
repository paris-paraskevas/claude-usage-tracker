package com.claudeusage.tracker.ui

import android.Manifest
import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.lifecycleScope
import com.claudeusage.tracker.Notifications
import com.claudeusage.tracker.Pairing
import com.claudeusage.tracker.Prefs
import com.claudeusage.tracker.push.PushTokens
import com.claudeusage.tracker.widget.refreshWidgetsNow
import com.claudeusage.tracker.widget.scheduleWidgetRefresh
import kotlinx.coroutines.launch

/** Holds a pairing parsed from a cutpair1: deep link until the user confirms it. A ViewModel so
 *  the confirmation survives rotation without writing the secret into saved-instance state. */
class PendingPairViewModel : ViewModel() {
    val pending = mutableStateOf<Pairing?>(null)
}

class MainActivity : ComponentActivity() {
    private val pendingPair by lazy { ViewModelProvider(this)[PendingPairViewModel::class.java].pending }
    private val dashboard by lazy { ViewModelProvider(this)[DashboardViewModel::class.java] }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Edge-to-edge (forced at targetSdk 35) with the default auto styles: the system bars'
        // icons follow the app's light/dark theme. The Compose roots inset themselves.
        enableEdgeToEdge()
        // Only on a fresh launch: after a rotation the same intent would re-stage a pairing the
        // user may already have confirmed or dismissed. New links arrive via onNewIntent.
        if (savedInstanceState == null) capturePairLink(intent)
        // Keep the home-screen widget + (opt-in) lock-screen notification fresh.
        scheduleWidgetRefresh(this)
        refreshWidgetsNow(this)
        if (Prefs.isPaired(this)) lifecycleScope.launch { runCatching { PushTokens.registerAll(this@MainActivity) } }
        setContent {
            AppTheme {
                var paired by rememberSaveable { mutableStateOf(Prefs.isPaired(this)) }
                // Ask for notifications right after a successful pairing (not on first launch).
                var askNotif by rememberSaveable { mutableStateOf(false) }
                val onPairedNew: () -> Unit = {
                    if (Notifications.needsRuntimePermission(this@MainActivity)) askNotif = true
                }

                Box(Modifier.fillMaxSize().background(Bg)) {
                    if (paired) {
                        DashboardScreen(
                            onUnpaired = { paired = false },
                            onPairedNew = onPairedNew,
                        )
                    } else {
                        PairScreen(onPaired = { p ->
                            dashboard.added(p)
                            paired = true
                            onPairedNew()
                        })
                    }
                }

                pendingPair.value?.let { p ->
                    PairConfirmDialog(
                        pairing = p,
                        onConfirm = {
                            dashboard.added(p)                   // add + make active + register push
                            pendingPair.value = null
                            paired = true
                            onPairedNew()
                        },
                        onDismiss = { pendingPair.value = null },
                    )
                }

                if (askNotif) NotificationRationale(onDone = { askNotif = false })
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        capturePairLink(intent)
    }

    /** Stage a cutpair1: deep link for confirmation — never auto-pair. An untrusted link could
     *  otherwise silently add an attacker's desktop as the account this phone displays. */
    private fun capturePairLink(intent: Intent?) {
        intent?.dataString?.let { Pairing.parse(it) }?.let { pendingPair.value = it }
    }
}

/** One-line rationale, then the system permission prompt (Android 13+). */
@Composable
private fun NotificationRationale(onDone: () -> Unit) {
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { onDone() }
    AlertDialog(
        onDismissRequest = onDone,
        title = { Text("Get usage alerts?") },
        text = { Text("Allow notifications so your desktop can warn you when you're close to a limit.") },
        confirmButton = {
            TextButton(onClick = { launcher.launch(Manifest.permission.POST_NOTIFICATIONS) }) { Text("Allow") }
        },
        dismissButton = { TextButton(onClick = onDone) { Text("Not now") } },
    )
}

@Composable
private fun PairConfirmDialog(pairing: Pairing, onConfirm: () -> Unit, onDismiss: () -> Unit) {
    val host = pairing.url.substringAfter("://").substringBefore("/")
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Pair this device?") },
        text = {
            Text(
                "This links a desktop via the relay at \"$host\" and shows its Claude usage on this " +
                    "phone. Only continue if you opened this from your own desktop's pairing QR.",
            )
        },
        confirmButton = { TextButton(onClick = onConfirm) { Text("Pair") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}
