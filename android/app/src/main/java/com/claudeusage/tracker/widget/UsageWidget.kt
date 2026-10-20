package com.claudeusage.tracker.widget

import android.content.Context
import android.os.Build
import androidx.compose.runtime.Composable
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.glance.ColorFilter
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.GlanceTheme
import androidx.glance.Image
import androidx.glance.ImageProvider
import androidx.glance.LocalSize
import androidx.glance.action.ActionParameters
import androidx.glance.action.actionParametersOf
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
import androidx.glance.currentState
import androidx.glance.layout.Alignment
import androidx.glance.layout.Box
import androidx.glance.layout.Column
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.fillMaxWidth
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.layout.size
import androidx.glance.layout.width
import androidx.glance.semantics.contentDescription
import androidx.glance.semantics.semantics
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import com.claudeusage.tracker.R
import com.claudeusage.tracker.ui.MainActivity

/** Per-instance Glance state: the account this widget shows (absent = follow the app's active
 *  account) and the fingerprint of what it last drew (see [updateWidgets]). */
internal val KEY_ACCOUNT = stringPreferencesKey("account_id")
internal val KEY_RENDERED = stringPreferencesKey("rendered_fp")

/** Intent extra / action parameter carrying an account id (widget tap → app on that account). */
const val EXTRA_ACCOUNT_ID = "account_id"
internal val AccountParam = ActionParameters.Key<String>(EXTRA_ACCOUNT_ID)

// Responsive buckets = the handheld minimum sizes (dp) of the three target cell spans. Glance
// draws each layout once and the launcher picks the largest bucket that fits the real size.
private val SIZE_2X1 = DpSize(109.dp, 56.dp)
private val SIZE_2X2 = DpSize(109.dp, 115.dp)
private val SIZE_4X2 = DpSize(245.dp, 115.dp)

/**
 * Home-screen widget (A-Android board, V2-SPEC §9).
 *  - 2×1: the binding limit's %, a thin bar and one line ("Limit ~16:26" / "Refills 16:40").
 *  - 2×2 (default): header + 5-hour and Weekly meters + the binding limit's pace line.
 *  - 4×2: both limits side by side with pace lines, account label, 48dp refresh button.
 * Light/dark with dynamic colour (brand scheme below Android 12); state colours are fixed
 * day/night pairs; every state carries a word and an icon. Times are absolute (no countdowns).
 *
 * [preview] renders a fixed model instead of the cache (generated picker previews).
 */
class UsageWidget internal constructor(private val preview: WidgetUi?) : GlanceAppWidget() {
    constructor() : this(null)

    override val sizeMode: SizeMode = SizeMode.Responsive(setOf(SIZE_2X1, SIZE_2X2, SIZE_4X2))

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        provideContent {
            // Reading the Glance state here ties this composition to update(): updateWidgets()
            // writes a new fingerprint into it whenever the data changed, which recomposes and
            // re-reads the cache below, even while a session is still alive.
            val ui = preview ?: WidgetModel.load(
                context,
                currentState<Preferences>()[KEY_ACCOUNT],
                System.currentTimeMillis(),
            )
            GlanceTheme(colors = widgetColors()) { WidgetContent(ui) }
        }
    }
}

/** Widget ↻ button: hand the fetch to the sync worker (it redraws only if the data changed). */
class RefreshWidgetAction : ActionCallback {
    override suspend fun onAction(context: Context, glanceId: GlanceId, parameters: ActionParameters) {
        refreshWidgetsNow(context, parameters[AccountParam])
    }
}

@Composable
private fun WidgetContent(ui: WidgetUi) {
    val size = LocalSize.current
    val params = ui.accountId?.let { actionParametersOf(AccountParam to it) } ?: actionParametersOf()
    val inks = inks(ui.limited)
    val bg = if (ui.limited) WidgetTones.LimitedBg.provider else GlanceTheme.colors.widgetBackground
    Box(modifier = surface(bg).clickable(actionStartActivity<MainActivity>(params))) {
        when {
            size.width >= SIZE_4X2.width && size.height >= SIZE_4X2.height -> Large(ui, inks)
            size.height >= SIZE_2X2.height -> Medium(ui, inks)
            else -> Small(ui, inks)
        }
    }
}

/** Edge-to-edge background with the system widget corner radius (Android 12+); below 12,
 *  where cornerRadius is ignored, a tinted rounded-rect drawable. */
private fun surface(bg: ColorProvider): GlanceModifier {
    val base = GlanceModifier.fillMaxSize().appWidgetBackground()
    return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
        base.background(bg).cornerRadius(android.R.dimen.system_app_widget_background_radius)
    } else {
        base.background(ImageProvider(R.drawable.widget_bg_shape), colorFilter = ColorFilter.tint(bg))
    }
}

private fun barFill(m: MeterUi): Tone = if (m.dim) WidgetTones.StaleFill else fillTone(m.level)

private fun valueInk(m: MeterUi, inks: Inks): ColorProvider = if (m.dim) inks.dim else inks.strong

// ---- 2×1 -------------------------------------------------------------------------------------

@Composable
private fun Small(ui: WidgetUi, inks: Inks) {
    Column(
        modifier = GlanceModifier.fillMaxSize().padding(horizontal = 14.dp, vertical = 8.dp)
            .semantics { contentDescription = ui.smallSpoken },
        verticalAlignment = Alignment.CenterVertically,
    ) {
        val hero = ui.hero
        when {
            ui.message != null -> StatusLine(ui.messageMark, ui.message, inks, size = 13.sp, bold = true, maxLines = 2)
            ui.limited -> {
                Text(
                    ui.limitedTitle ?: "Limit reached",
                    style = TextStyle(color = inks.dim, fontSize = 12.sp, fontWeight = FontWeight.Medium),
                    maxLines = 1,
                )
                Text(
                    ui.backAt ?: "Limit reached",
                    style = TextStyle(color = inks.strong, fontSize = 20.sp, fontWeight = FontWeight.Bold),
                    maxLines = 1,
                )
            }
            hero != null -> {
                Row(modifier = GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        hero.pctText,
                        style = TextStyle(color = valueInk(hero, inks), fontSize = 20.sp, fontWeight = FontWeight.Bold),
                        maxLines = 1,
                    )
                    Spacer(GlanceModifier.defaultWeight())
                    Text(
                        hero.shortLabel,
                        style = TextStyle(color = inks.dim, fontSize = 12.sp, fontWeight = FontWeight.Medium),
                        maxLines = 1,
                    )
                }
                Spacer(GlanceModifier.height(4.dp))
                MeterBar(hero.progress, barFill(hero), inks.track, height = 5.dp)
                Spacer(GlanceModifier.height(5.dp))
                StatusLine(ui.heroMark, ui.heroLine, inks)
            }
            else -> StatusLine(ui.heroMark, ui.heroLine, inks)
        }
    }
}

// ---- 2×2 -------------------------------------------------------------------------------------

@Composable
private fun Medium(ui: WidgetUi, inks: Inks) {
    Column(modifier = GlanceModifier.fillMaxSize().padding(horizontal = 16.dp, vertical = 12.dp)) {
        Header(ui.title, ui.updated, ui.headerSpoken, inks)
        Spacer(GlanceModifier.height(6.dp))
        if (ui.message != null) {
            Message(ui, inks)
        } else {
            ui.five?.let { MeterRow(it, inks) }
            if (ui.five != null && ui.week != null) Spacer(GlanceModifier.height(8.dp))
            ui.week?.let { MeterRow(it, inks) }
            Spacer(GlanceModifier.defaultWeight())
            StatusLine(
                ui.bottomMark, ui.bottom, inks,
                size = if (ui.limited) 15.sp else 12.sp,
                bold = ui.limited,
            )
        }
    }
}

@Composable
private fun MeterRow(m: MeterUi, inks: Inks) {
    Column(modifier = GlanceModifier.fillMaxWidth().semantics { contentDescription = m.spoken }) {
        Row(modifier = GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(
                m.label,
                style = TextStyle(color = valueInk(m, inks), fontSize = 13.sp, fontWeight = FontWeight.Medium),
                maxLines = 1,
                modifier = GlanceModifier.defaultWeight(),
            )
            Text(
                m.pctText,
                style = TextStyle(color = valueInk(m, inks), fontSize = 13.sp, fontWeight = FontWeight.Bold),
                maxLines = 1,
            )
        }
        Spacer(GlanceModifier.height(4.dp))
        MeterBar(m.progress, barFill(m), inks.track)
        m.refill?.let {
            Spacer(GlanceModifier.height(3.dp))
            Text(it, style = TextStyle(color = inks.dim, fontSize = 12.sp), maxLines = 1)
        }
    }
}

// ---- 4×2 -------------------------------------------------------------------------------------

@Composable
private fun Large(ui: WidgetUi, inks: Inks) {
    val canRefresh = ui.phase != Phase.NOT_PAIRED
    Box(modifier = GlanceModifier.fillMaxSize()) {
        Column(modifier = GlanceModifier.fillMaxSize().padding(horizontal = 16.dp, vertical = 12.dp)) {
            Header(ui.titleWide, ui.updatedWide, ui.headerSpoken, inks, endPadding = if (canRefresh) 36.dp else 0.dp)
            Spacer(GlanceModifier.height(8.dp))
            if (ui.message != null) {
                Message(ui, inks)
            } else {
                Row(modifier = GlanceModifier.fillMaxWidth()) {
                    MeterColumn(ui.five, inks, GlanceModifier.defaultWeight())
                    Spacer(GlanceModifier.width(16.dp))
                    MeterColumn(ui.week, inks, GlanceModifier.defaultWeight())
                }
                if (ui.status != null) {
                    Spacer(GlanceModifier.defaultWeight())
                    StatusLine(ui.statusMark, ui.status, inks)
                }
            }
        }
        if (canRefresh) RefreshButton(ui.accountId)
    }
}

@Composable
private fun MeterColumn(m: MeterUi?, inks: Inks, modifier: GlanceModifier) {
    if (m == null) {
        Spacer(modifier)
        return
    }
    Column(modifier = modifier.semantics { contentDescription = m.spoken }) {
        Row(modifier = GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.Bottom) {
            Text(
                m.label,
                style = TextStyle(color = valueInk(m, inks), fontSize = 13.sp, fontWeight = FontWeight.Medium),
                maxLines = 1,
                modifier = GlanceModifier.defaultWeight(),
            )
            Text(
                m.pctText,
                style = TextStyle(color = valueInk(m, inks), fontSize = 22.sp, fontWeight = FontWeight.Bold),
                maxLines = 1,
            )
        }
        Spacer(GlanceModifier.height(4.dp))
        MeterBar(m.progress, barFill(m), inks.track)
        Spacer(GlanceModifier.height(4.dp))
        m.refill?.let { Text(it, style = TextStyle(color = inks.dim, fontSize = 12.sp), maxLines = 1) }
        StatusLine(m.mark, m.paceLong, inks)
    }
}

/** 48dp touch target in the top-end corner (overlapping the content padding) around a 32dp
 *  visual, so the header stays one text line tall. */
@Composable
private fun RefreshButton(accountId: String?) {
    val params = accountId?.let { actionParametersOf(AccountParam to it) } ?: actionParametersOf()
    Box(modifier = GlanceModifier.fillMaxSize(), contentAlignment = Alignment.TopEnd) {
        Box(
            modifier = GlanceModifier.size(48.dp).padding(top = 4.dp, end = 4.dp)
                .clickable(actionRunCallback<RefreshWidgetAction>(params))
                .semantics { contentDescription = "Refresh usage" },
            contentAlignment = Alignment.Center,
        ) {
            Box(
                modifier = GlanceModifier.size(32.dp).background(
                    ImageProvider(R.drawable.widget_circle),
                    colorFilter = ColorFilter.tint(GlanceTheme.colors.secondaryContainer),
                ),
                contentAlignment = Alignment.Center,
            ) {
                Image(
                    provider = ImageProvider(R.drawable.ic_refresh),
                    contentDescription = null,
                    colorFilter = ColorFilter.tint(GlanceTheme.colors.onSecondaryContainer),
                    modifier = GlanceModifier.size(18.dp),
                )
            }
        }
    }
}

// ---- shared ----------------------------------------------------------------------------------

@Composable
private fun Header(title: String, right: String?, spoken: String, inks: Inks, endPadding: Dp = 0.dp) {
    Row(
        modifier = GlanceModifier.fillMaxWidth().height(24.dp).padding(end = endPadding)
            .semantics { contentDescription = spoken },
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Image(
            provider = ImageProvider(R.drawable.ic_stat_usage),
            contentDescription = null,
            colorFilter = ColorFilter.tint(if (inks.limitedSurface) inks.strong else GlanceTheme.colors.primary),
            modifier = GlanceModifier.size(14.dp),
        )
        Spacer(GlanceModifier.width(6.dp))
        Text(
            title,
            style = TextStyle(color = inks.dim, fontSize = 12.sp, fontWeight = FontWeight.Medium),
            maxLines = 1,
            modifier = GlanceModifier.defaultWeight(),
        )
        if (right != null) {
            Spacer(GlanceModifier.width(6.dp))
            Text(right, style = TextStyle(color = inks.dim, fontSize = 12.sp, fontWeight = FontWeight.Medium), maxLines = 1)
        }
    }
}

/** Not paired / loading / waiting: a sentence instead of empty meters. */
@Composable
private fun Message(ui: WidgetUi, inks: Inks) {
    Column(modifier = GlanceModifier.fillMaxWidth()) {
        StatusLine(ui.messageMark, ui.message, inks, size = 15.sp, bold = true, maxLines = 2)
        ui.messageDetail?.let {
            Spacer(GlanceModifier.height(4.dp))
            Text(it, style = TextStyle(color = inks.dim, fontSize = 12.sp), maxLines = 2)
        }
    }
}
