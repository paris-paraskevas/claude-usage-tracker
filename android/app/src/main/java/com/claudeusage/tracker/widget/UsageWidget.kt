package com.claudeusage.tracker.widget

import android.content.Context
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.action.actionStartActivity
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.cornerRadius
import androidx.glance.appwidget.provideContent
import androidx.glance.background
import androidx.glance.layout.Alignment
import androidx.glance.layout.Column
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.fillMaxWidth
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextAlign
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import com.claudeusage.tracker.Prefs
import com.claudeusage.tracker.Snap
import com.claudeusage.tracker.ui.Bg
import com.claudeusage.tracker.ui.Dim
import com.claudeusage.tracker.ui.Faint
import com.claudeusage.tracker.ui.Ink
import com.claudeusage.tracker.ui.MainActivity
import com.claudeusage.tracker.ui.bandColor
import com.claudeusage.tracker.ui.usageColor

/** Home-screen (and, where the launcher allows, lock-screen) widget. Renders the latest
 *  cached snapshot; the refresh worker keeps it current. Tapping opens the app. */
class UsageWidget : GlanceAppWidget() {
    override suspend fun provideGlance(context: Context, id: GlanceId) {
        val snap = WidgetData.loadSnap(context)?.let { runCatching { Snap.parse(it) }.getOrNull() }
        val paired = Prefs.isPaired(context)
        provideContent { WidgetUI(snap, paired) }
    }
}

@Composable
private fun WidgetUI(snap: Snap?, paired: Boolean) {
    Column(
        modifier = GlanceModifier.fillMaxSize().background(ColorProvider(Bg))
            .cornerRadius(18.dp).padding(14.dp).clickable(actionStartActivity<MainActivity>()),
    ) {
        Text(
            if (paired) (snap?.org?.ifBlank { "Claude" } ?: "Claude") else "Usage Tracker for Claude",
            style = TextStyle(color = ColorProvider(Ink), fontSize = 14.sp, fontWeight = FontWeight.Bold),
            maxLines = 1,
        )
        Spacer(GlanceModifier.height(8.dp))
        when {
            !paired -> Hint("Tap to pair your phone")
            snap == null -> Hint("Tap to sync")
            else -> {
                val five = snap.wins.firstOrNull { it.key == "five_hour" }
                val week = snap.wins.firstOrNull { it.key == "seven_day" }
                Line("5h", five?.pct, usageColor(five?.pct ?: 0.0))
                Spacer(GlanceModifier.height(4.dp))
                Line("Week", week?.pct, usageColor(week?.pct ?: 0.0))
                Spacer(GlanceModifier.height(4.dp))
                Line("Ctx", snap.ctxPct, bandColor(snap.ctxPct ?: 0.0))
            }
        }
    }
}

@Composable
private fun Hint(t: String) =
    Text(t, style = TextStyle(color = ColorProvider(Dim), fontSize = 12.sp))

@Composable
private fun Line(label: String, pct: Double?, color: Color) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = GlanceModifier.fillMaxWidth().padding(vertical = 3.dp),
    ) {
        Text(label, style = TextStyle(color = ColorProvider(Faint), fontSize = 13.sp))
        Text(
            if (pct != null) "${pct.toInt()}%" else "–",
            style = TextStyle(color = ColorProvider(color), fontSize = 22.sp,
                fontWeight = FontWeight.Bold, textAlign = TextAlign.End),
            modifier = GlanceModifier.fillMaxWidth(),
        )
    }
}
