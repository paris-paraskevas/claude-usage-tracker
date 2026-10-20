package com.claudeusage.tracker.widget

import android.appwidget.AppWidgetManager
import android.content.ComponentName
import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.glance.GlanceId
import androidx.glance.appwidget.GlanceAppWidgetManager
import androidx.glance.appwidget.state.getAppWidgetState
import androidx.glance.appwidget.state.updateAppWidgetState
import androidx.glance.state.PreferencesGlanceStateDefinition
import androidx.lifecycle.lifecycleScope
import com.claudeusage.tracker.Prefs
import com.claudeusage.tracker.Snap
import com.claudeusage.tracker.ui.AppTheme
import com.claudeusage.tracker.ui.Bg
import com.claudeusage.tracker.ui.Dim
import com.claudeusage.tracker.ui.Ink
import com.claudeusage.tracker.ui.MainActivity
import kotlinx.coroutines.launch

/**
 * Picks which paired account one widget instance shows (`widgetFeatures` =
 * `reconfigurable|configuration_optional`): on Android 12+ a new widget is placed straight away
 * following the app's (primary) account and this screen opens from the launcher's
 * "Reconfigure"/widget settings. Older launchers open it on placement; backing out keeps the
 * widget with the default, never removes it.
 */
class WidgetConfigActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val appWidgetId = intent?.extras?.getInt(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID)
            ?: AppWidgetManager.INVALID_APPWIDGET_ID
        // Exported for the launcher: only act on a widget that is really one of ours.
        val info = AppWidgetManager.getInstance(this).getAppWidgetInfo(appWidgetId)
        if (appWidgetId == AppWidgetManager.INVALID_APPWIDGET_ID ||
            info?.provider != ComponentName(this, UsageWidgetReceiver::class.java)
        ) {
            setResult(RESULT_CANCELED)
            finish()
            return
        }
        val ok = Intent().putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, appWidgetId)
        setResult(RESULT_OK, ok)   // Back = keep the widget as it is

        val accounts = Prefs.all(this)
        // Before Android 12 this also runs on placement; with nothing to choose, don't ask.
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S && accounts.size <= 1) {
            finish()
            return
        }
        val glanceId = GlanceAppWidgetManager(this).getGlanceIdBy(appWidgetId)
        enableEdgeToEdge()

        lifecycleScope.launch {
            val current = runCatching {
                getAppWidgetState(this@WidgetConfigActivity, PreferencesGlanceStateDefinition, glanceId)[KEY_ACCOUNT]
            }.getOrNull()?.takeIf { id -> accounts.any { it.pairing.accountId == id } }
            val activeLabel = Prefs.activeId(this@WidgetConfigActivity)?.let { labelOf(it) }
            val options = accounts.mapIndexed { i, a ->
                a.pairing.accountId to (labelOf(a.pairing.accountId) ?: "Desktop ${i + 1}")
            }
            setContent {
                AppTheme {
                    ConfigScreen(
                        options = options,
                        activeLabel = activeLabel,
                        initial = current,
                        onPair = {
                            startActivity(Intent(this@WidgetConfigActivity, MainActivity::class.java))
                            finish()
                        },
                        onDone = { choice -> save(glanceId, choice) },
                        onCancel = { finish() },
                    )
                }
            }
        }
    }

    private fun labelOf(accountId: String): String? {
        Prefs.all(this).firstOrNull { it.pairing.accountId == accountId }?.label?.let { return it }
        val js = WidgetData.loadSnap(this, accountId) ?: return null
        return runCatching { Snap.parse(js).org }.getOrNull()?.ifBlank { null }
    }

    /** null = follow the app's active account. */
    private fun save(glanceId: GlanceId, accountId: String?) {
        val app = applicationContext
        lifecycleScope.launch {
            updateAppWidgetState(app, glanceId) { prefs ->
                if (accountId == null) prefs.remove(KEY_ACCOUNT) else prefs[KEY_ACCOUNT] = accountId
                prefs.remove(KEY_RENDERED)
            }
            UsageWidget().update(app, glanceId)
            refreshWidgetsNow(app, accountId ?: Prefs.activeId(app))
            finish()
        }
    }
}

@Composable
private fun ConfigScreen(
    options: List<Pair<String, String>>,
    activeLabel: String?,
    initial: String?,
    onPair: () -> Unit,
    onDone: (String?) -> Unit,
    onCancel: () -> Unit,
) {
    // "" = follow the app (rememberSaveable can't hold a null String in a Bundle-friendly way).
    var selected by rememberSaveable { mutableStateOf(initial ?: "") }
    Column(
        Modifier.fillMaxSize().background(Bg).safeDrawingPadding()
            .verticalScroll(rememberScrollState()).padding(horizontal = 20.dp, vertical = 16.dp),
    ) {
        Text(
            "Widget account",
            color = Ink,
            style = MaterialTheme.typography.headlineMedium,
            modifier = Modifier.semantics { heading() },
        )
        Spacer(Modifier.height(4.dp))
        if (options.isEmpty()) {
            Text(
                "No desktop is paired yet. Pair one in the app, then the widget shows its usage.",
                color = Dim,
                style = MaterialTheme.typography.bodyMedium,
            )
            Spacer(Modifier.height(16.dp))
            Button(onClick = onPair) { Text("Open the app") }
        } else {
            Text(
                "Choose which paired desktop this widget shows.",
                color = Dim,
                style = MaterialTheme.typography.bodyMedium,
            )
            Spacer(Modifier.height(12.dp))
            Column(Modifier.selectableGroup()) {
                Option(
                    title = "Follow the app",
                    sub = activeLabel?.let { "The account open in the app — now $it" } ?: "The account open in the app",
                    selected = selected.isEmpty(),
                    onSelect = { selected = "" },
                )
                options.forEach { (id, label) ->
                    Option(title = label, sub = null, selected = selected == id, onSelect = { selected = id })
                }
            }
            Spacer(Modifier.height(16.dp))
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                TextButton(onClick = onCancel) { Text("Cancel") }
                Spacer(Modifier.width(8.dp))
                Button(onClick = { onDone(selected.ifEmpty { null }) }) { Text("Done") }
            }
        }
    }
}

@Composable
private fun Option(title: String, sub: String?, selected: Boolean, onSelect: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().heightIn(min = 56.dp)
            .selectable(selected = selected, onClick = onSelect, role = Role.RadioButton)
            .padding(vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        RadioButton(selected = selected, onClick = null)
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(title, color = Ink, style = MaterialTheme.typography.titleMedium)
            if (sub != null) Text(sub, color = Dim, style = MaterialTheme.typography.bodySmall)
        }
    }
}
