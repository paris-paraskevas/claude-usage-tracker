package com.claudeusage.tracker.ui

import android.Manifest
import android.content.Context
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import com.claudeusage.tracker.Pairing
import com.claudeusage.tracker.Prefs
import com.claudeusage.tracker.RelayClient
import com.google.firebase.messaging.FirebaseMessaging
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Allow pairing via a cutpair1: deep link (in addition to scanning the QR).
        intent?.dataString?.let { data -> Pairing.parse(data)?.let { Prefs.save(this, it) } }
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
    val p = Prefs.load(ctx) ?: return
    try {
        FirebaseMessaging.getInstance().token.addOnSuccessListener { token ->
            CoroutineScope(Dispatchers.IO).launch { runCatching { RelayClient(p).registerPushToken(token) } }
        }
    } catch (_: Exception) {
        // Firebase not initialized — push disabled, Phase-1 viewing still works.
    }
}
