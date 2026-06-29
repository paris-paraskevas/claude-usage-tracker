package com.claudeusage.tracker.ui

import android.Manifest
import android.content.Context
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import com.claudeusage.tracker.Pairing
import com.claudeusage.tracker.Prefs
import com.claudeusage.tracker.RelayClient
import com.claudeusage.tracker.widget.refreshWidgetsNow
import com.claudeusage.tracker.widget.scheduleWidgetRefresh
import com.google.firebase.messaging.FirebaseMessaging
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // targetSdk 35 forces edge-to-edge. Opt in explicitly and pin both bars to the
        // "dark" style (transparent scrim, light icons) so the clock/battery stay legible
        // on our always-dark UI regardless of the phone's light/dark setting. The Compose
        // roots inset themselves via WindowInsets.safeDrawing.
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
        )
        // Allow pairing via a cutpair1: deep link (in addition to scanning the QR).
        intent?.dataString?.let { data -> Pairing.parse(data)?.let { Prefs.save(this, it) } }
        // Keep the home-screen widget + (opt-in) lock-screen notification fresh.
        scheduleWidgetRefresh(this)
        refreshWidgetsNow(this)
        setContent {
            AppTheme {
                var paired by remember { mutableStateOf(Prefs.isPaired(this)) }

                val notifPerm = rememberLauncherForActivityResult(
                    ActivityResultContracts.RequestPermission()
                ) { /* ignored — push still arrives; user can grant later in settings */ }

                LaunchedEffect(Unit) {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                        notifPerm.launch(Manifest.permission.POST_NOTIFICATIONS)
                    }
                }

                if (paired) {
                    LaunchedEffect(Unit) { registerFcmToken(this@MainActivity) }
                    DashboardScreen(onUnpair = {
                        Prefs.clear(this@MainActivity)
                        paired = false
                    })
                } else {
                    PairScreen(onPaired = { paired = true })
                }
            }
        }
    }
}

/** Best-effort: send our FCM token to the relay so the desktop can push us alerts.
 *  No-ops cleanly if Firebase isn't configured (no google-services.json). */
fun registerFcmToken(ctx: Context) {
    val accounts = Prefs.all(ctx)
    if (accounts.isEmpty()) return
    try {
        FirebaseMessaging.getInstance().token.addOnSuccessListener { token ->
            accounts.forEach { a ->
                CoroutineScope(Dispatchers.IO).launch { runCatching { RelayClient(a.pairing).registerPushToken(token) } }
            }
        }
    } catch (_: Exception) {
        // Firebase not initialized — push disabled, viewing still works.
    }
}
