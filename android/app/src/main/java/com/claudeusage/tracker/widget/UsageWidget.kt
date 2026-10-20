package com.claudeusage.tracker.widget

import android.content.Context
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.ColorFilter
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.GlanceTheme
import androidx.glance.Image
import androidx.glance.ImageProvider
import androidx.glance.LocalSize
import androidx.glance.action.ActionParameters
import androidx.glance.action.actionStartActivity
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.SizeMode
import androidx.glance.appwidget.action.ActionCallback
import androidx.glance.appwidget.action.actionRunCallback
import androidx.glance.appwidget.appWidgetBackground
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
import androidx.glance.layout.size
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextAlign
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import com.claudeusage.tracker.Prefs
import com.claudeusage.tracker.R
import com.claudeusage.tracker.Snap
import com.claudeusage.tracker.Win
import com.claudeusage.tracker.ui.MainActivity
import com.claudeusage.tracker.ui.STALE_AFTER_S
import com.claudeusage.tracker.ui.bandColor
import com.claudeusage.tracker.ui.usageColor

private val SMALL = DpSize(110.dp, 80.dp)
private val MEDIUM = DpSize(180.dp, 110.dp)
private val Amber = Color(0xFFCDA24E)

/** Home-screen (and, where the launcher allows, lock-screen) widget. Renders the latest cached
 *  snapshot; the refresh worker keeps it current. Tapping opens the app; the ↻ button refreshes.
 *  Colours come from GlanceTheme (dynamic on Android 12+, light/dark aware); the usage bands keep
 *  their fixed severity colours. Two responsive layouts: compact (5h + weekly) and full. */
class UsageWidget : GlanceAppWidget() {
    override val sizeMode = SizeMode.Responsive(setOf(SMALL, MEDIUM))

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        val snap = WidgetData.loadSnap(context)?.let { runCatching { Snap.parse(it) }.getOrNull() }
        val paired = Prefs.isPaired(context)
        val account = accountPrefix(context)
        val now = System.currentTimeMillis()
        provideContent {
            GlanceTheme { WidgetUI(snap, paired, account, now) }
        }
    }
}

/** Widget ↻ button: fetch now and redraw. */
class RefreshWidgetAction : ActionCallback {
    override suspend fun onAction(context: Context, glanceId: GlanceId, parameters: ActionParameters) {
        refreshWidgets(context)
    }
}

@Composable
private fun WidgetUI(snap: Snap?, paired: Boolean, account: String?, now: Long) {
    val compact = LocalSize.current.width < MEDIUM.width || LocalSize.current.height < MEDIUM.height
    Column(
        modifier = GlanceModifier.fillMaxSize().appWidgetBackground()
            .background(GlanceTheme.colors.widgetBackground)
            .cornerRadius(18.dp).padding(if (compact) 10.dp else 14.dp)
            .clickable(actionStartActivity<MainActivity>()),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = GlanceModifier.fillMaxWidth()) {
            val title = when {
                !paired -> "Usage Tracker"
                account != null -> account
                else -> snap?.org?.ifBlank { null } ?: "Claude"
            }
            Text(
                title,
                style = TextStyle(color = GlanceTheme.colors.onSurface, fontSize = 14.sp, fontWeight = FontWeight.Bold),
                maxLines = 1,
                modifier = GlanceModifier.defaultWeight(),
            )
            if (paired) {
                Image(
                    provider = ImageProvider(R.drawable.ic_refresh),
                    contentDescription = "Refresh",
                    colorFilter = ColorFilter.tint(GlanceTheme.colors.onSurfaceVariant),
                    modifier = GlanceModifier.size(if (compact) 20.dp else 24.dp)
                        .clickable(actionRunCallback<RefreshWidgetAction>()),
                )
            }
        }
        Spacer(GlanceModifier.height(if (compact) 4.dp else 8.dp))
        when {
            !paired -> Hint("Tap to pair your phone")
            snap == null -> Hint("Tap to sync")
            compact -> {
                Line("5h", snap.fiveHour?.pct, usageColor(snap.fiveHour?.pct ?: 0.0), null, big = true)
                Line("Week", snap.sevenDay?.pct, usageColor(snap.sevenDay?.pct ?: 0.0), null, big = false)
            }
            else -> {
                Line("5h", snap.fiveHour?.pct, usageColor(snap.fiveHour?.pct ?: 0.0), resetText(snap.fiveHour), big = true)
                Line("Week", snap.sevenDay?.pct, usageColor(snap.sevenDay?.pct ?: 0.0), resetText(snap.sevenDay), big = true)
                Line("Ctx", snap.ctxPct, bandColor(snap.ctxPct ?: 0.0), null, big = false)
                Spacer(GlanceModifier.height(2.dp))
                val ageS = if (snap.updatedAt > 0) now / 1000 - snap.updatedAt else Long.MAX_VALUE
                val stale = ageS > STALE_AFTER_S
                Text(
                    if (snap.updatedAt > 0) (if (stale) "stale · as of " else "as of ") + fmtClock(snap.updatedAt * 1000)
                    else "not synced yet",
                    style = TextStyle(
                        color = if (stale) ColorProvider(Amber) else GlanceTheme.colors.onSurfaceVariant,
                        fontSize = 11.sp,
                    ),
                    maxLines = 1,
                )
            }
        }
    }
}

private fun resetText(w: Win?): String? = w?.resetsAt?.let { "resets ${fmtClock(it)}" }

@Composable
private fun Hint(t: String) =
    Text(t, style = TextStyle(color = GlanceTheme.colors.onSurfaceVariant, fontSize = 12.sp))

@Composable
private fun Line(label: String, pct: Double?, color: Color, sub: String?, big: Boolean) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = GlanceModifier.fillMaxWidth().padding(vertical = 2.dp),
    ) {
        Column(modifier = GlanceModifier.defaultWeight()) {
            Text(label, style = TextStyle(color = GlanceTheme.colors.onSurfaceVariant, fontSize = 13.sp), maxLines = 1)
            if (sub != null) {
                Text(sub, style = TextStyle(color = GlanceTheme.colors.onSurfaceVariant, fontSize = 10.sp), maxLines = 1)
            }
        }
        Text(
            if (pct != null) "${pct.toInt()}%" else "–",
            style = TextStyle(color = ColorProvider(color), fontSize = if (big) 22.sp else 16.sp,
                fontWeight = FontWeight.Bold, textAlign = TextAlign.End),
        )
    }
}
