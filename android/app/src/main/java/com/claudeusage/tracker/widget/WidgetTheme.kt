package com.claudeusage.tracker.widget

import android.content.Context
import android.content.res.Configuration
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.os.Build
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.ColorFilter
import androidx.glance.GlanceModifier
import androidx.glance.GlanceTheme
import androidx.glance.Image
import androidx.glance.ImageProvider
import androidx.glance.LocalContext
import androidx.glance.appwidget.LinearProgressIndicator
import androidx.glance.color.ColorProviders
import androidx.glance.color.colorProviders
import androidx.glance.layout.Alignment
import androidx.glance.layout.ContentScale
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxWidth
import androidx.glance.layout.height
import androidx.glance.layout.size
import androidx.glance.layout.width
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import com.claudeusage.tracker.Level
import com.claudeusage.tracker.R
import androidx.glance.color.ColorProvider as DayNight

/** A fixed day/night colour pair. State colours never follow the wallpaper: they must keep their
 *  meaning and their contrast (checked ≥ 4.5:1 for text, ≥ 3:1 for bar fills against the track,
 *  on the brand surfaces and on typical dynamic-colour surfaces). */
internal class Tone(val day: Color, val night: Color) {
    val provider: ColorProvider = DayNight(day = day, night = night)

    fun resolve(ctx: Context): Color = if (isNight(ctx)) night else day
}

internal fun isNight(ctx: Context): Boolean =
    (ctx.resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES

/** State colours — the A-Android board hues (ok blue, watch amber, will-hit vermillion). */
internal object WidgetTones {
    val OkFill = Tone(Color(0xFF0067A0), Color(0xFF4FA8E0))
    val OkText = Tone(Color(0xFF00497A), Color(0xFF7CC0EC))
    val WatchFill = Tone(Color(0xFF9A6200), Color(0xFFF2B544))
    val WatchText = Tone(Color(0xFF7A4D00), Color(0xFFF2B544))
    val HitFill = Tone(Color(0xFFB7470F), Color(0xFFF07B3F))
    val HitText = Tone(Color(0xFF9A3A0B), Color(0xFFF49B6E))

    /** Dimmed (stale) bar fill. */
    val StaleFill = Tone(Color(0xFF8C8580), Color(0xFF8A817C))

    /** Bar track: translucent, so it sits on any (dynamic) background. */
    val Track = Tone(Color(0x1F000000), Color(0x29FFFFFF))

    // "Limit reached": a solid surface in the will-hit hue, with its own fixed ink.
    val LimitedBg = Tone(Color(0xFFFFDBCB), Color(0xFF3B2A21))
    val LimitedInk = Tone(Color(0xFF2B140A), Color(0xFFFFFFFF))
    val LimitedInkDim = Tone(Color(0xFF6B3A1C), Color(0xFFFFDBCB))
    val LimitedTrack = Tone(Color(0x29000000), Color(0x33FFFFFF))
}

/** Brand fallback scheme below Android 12 (no dynamic colour there): warm Material 3 tones from
 *  the app accent; surfaces and inks match the A-Android board. */
internal val BrandWidgetColors: ColorProviders = colorProviders(
    primary = DayNight(Color(0xFF8F4C38), Color(0xFFFFB5A0)),
    onPrimary = DayNight(Color(0xFFFFFFFF), Color(0xFF561F0F)),
    primaryContainer = DayNight(Color(0xFFFFDBD1), Color(0xFF723523)),
    onPrimaryContainer = DayNight(Color(0xFF3A0B01), Color(0xFFFFDBD1)),
    secondary = DayNight(Color(0xFF77574E), Color(0xFFE7BDB2)),
    onSecondary = DayNight(Color(0xFFFFFFFF), Color(0xFF442A22)),
    secondaryContainer = DayNight(Color(0xFFEBDACE), Color(0xFF4A403B)),
    onSecondaryContainer = DayNight(Color(0xFF231A15), Color(0xFFECE0DA)),
    tertiary = DayNight(Color(0xFF6C5D2F), Color(0xFFD8C58D)),
    onTertiary = DayNight(Color(0xFFFFFFFF), Color(0xFF3B2F05)),
    tertiaryContainer = DayNight(Color(0xFFF5E1A7), Color(0xFF534619)),
    onTertiaryContainer = DayNight(Color(0xFF231B00), Color(0xFFF5E1A7)),
    error = DayNight(Color(0xFFBA1A1A), Color(0xFFFFB4AB)),
    errorContainer = DayNight(Color(0xFFFFDAD6), Color(0xFF93000A)),
    onError = DayNight(Color(0xFFFFFFFF), Color(0xFF690005)),
    onErrorContainer = DayNight(Color(0xFF410002), Color(0xFFFFDAD6)),
    background = DayNight(Color(0xFFFFF8F6), Color(0xFF1A110F)),
    onBackground = DayNight(Color(0xFF231A15), Color(0xFFECE0DA)),
    surface = DayNight(Color(0xFFFFF8F6), Color(0xFF1A110F)),
    onSurface = DayNight(Color(0xFF231A15), Color(0xFFECE0DA)),
    surfaceVariant = DayNight(Color(0xFFF5DED8), Color(0xFF53433F)),
    onSurfaceVariant = DayNight(Color(0xFF52443C), Color(0xFFBFB0A8)),
    outline = DayNight(Color(0xFF85736E), Color(0xFFA08C87)),
    inverseOnSurface = DayNight(Color(0xFFFFEDE8), Color(0xFF392E2B)),
    inverseSurface = DayNight(Color(0xFF392E2B), Color(0xFFF1DFDA)),
    inversePrimary = DayNight(Color(0xFFFFB5A0), Color(0xFF8F4C38)),
    widgetBackground = DayNight(Color(0xFFF7EDE6), Color(0xFF2A2624)),
)

/** Dynamic (wallpaper) colour on Android 12+, the brand scheme below. */
@Composable
internal fun widgetColors(): ColorProviders =
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) GlanceTheme.colors else BrandWidgetColors

internal fun fillTone(level: Level): Tone = when (level) {
    Level.OK -> WidgetTones.OkFill
    Level.WATCH -> WidgetTones.WatchFill
    Level.WILL_HIT, Level.LIMITED -> WidgetTones.HitFill
}

/** Ink colours for one surface: the theme's, or the fixed ones on the "limit reached" surface. */
internal class Inks(val strong: ColorProvider, val dim: ColorProvider, val track: Tone, val limitedSurface: Boolean)

@Composable
internal fun inks(limited: Boolean): Inks =
    if (limited) {
        Inks(WidgetTones.LimitedInk.provider, WidgetTones.LimitedInkDim.provider, WidgetTones.LimitedTrack, true)
    } else {
        Inks(GlanceTheme.colors.onSurface, GlanceTheme.colors.onSurfaceVariant, WidgetTones.Track, false)
    }

@Composable
internal fun markColor(mark: Mark, inks: Inks): ColorProvider = when (mark) {
    Mark.OK -> if (inks.limitedSurface) inks.dim else WidgetTones.OkText.provider
    Mark.WATCH -> if (inks.limitedSurface) inks.dim else WidgetTones.WatchText.provider
    Mark.WILL_HIT, Mark.LIMITED -> if (inks.limitedSurface) inks.strong else WidgetTones.HitText.provider
    Mark.PROBLEM -> if (inks.limitedSurface) inks.strong else WidgetTones.WatchText.provider
    Mark.PAIR, Mark.SYNC -> GlanceTheme.colors.primary
    Mark.OFFLINE, Mark.INFO -> inks.dim
}

private fun markIcon(mark: Mark): Int = when (mark) {
    Mark.OK -> R.drawable.ic_w_ok
    Mark.WATCH -> R.drawable.ic_w_watch
    Mark.WILL_HIT -> R.drawable.ic_w_will_hit
    Mark.LIMITED -> R.drawable.ic_w_limited
    Mark.OFFLINE -> R.drawable.ic_w_offline
    Mark.PROBLEM -> R.drawable.ic_w_problem
    Mark.PAIR -> R.drawable.ic_w_pair
    Mark.SYNC -> R.drawable.ic_refresh
    Mark.INFO -> R.drawable.ic_w_info
}

/** Icon + words: a state is never carried by colour alone. The icon is decorative (the words,
 *  or the enclosing group's description, are what TalkBack reads). */
@Composable
internal fun StatusLine(
    mark: Mark,
    text: String?,
    inks: Inks,
    modifier: GlanceModifier = GlanceModifier,
    size: TextUnit = 12.sp,
    bold: Boolean = false,
    maxLines: Int = 1,
) {
    if (text.isNullOrEmpty()) return
    val color = markColor(mark, inks)
    Row(modifier = modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Image(
            provider = ImageProvider(markIcon(mark)),
            contentDescription = null,
            colorFilter = ColorFilter.tint(color),
            modifier = GlanceModifier.size(if (size.value > 14f) 16.dp else 13.dp),
        )
        Spacer(GlanceModifier.width(4.dp))
        Text(
            text,
            style = TextStyle(
                color = color,
                fontSize = size,
                fontWeight = if (bold) FontWeight.Bold else FontWeight.Medium,
            ),
            maxLines = maxLines,
        )
    }
}

/**
 * The usage bar. Android 12+: a Glance [LinearProgressIndicator] with day/night tints. Below 12
 * RemoteViews can't tint a ProgressBar, so the bar is drawn into a small bitmap in the current
 * day/night colours (it is redrawn on every widget update).
 */
@Composable
internal fun MeterBar(progress: Float, fill: Tone, track: Tone, height: Dp = 6.dp) {
    val p = progress.coerceIn(0f, 1f)
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
        LinearProgressIndicator(
            progress = p,
            modifier = GlanceModifier.fillMaxWidth().height(height),
            color = fill.provider,
            backgroundColor = track.provider,
        )
    } else {
        val ctx = LocalContext.current
        Image(
            provider = ImageProvider(barBitmap(ctx, p, fill.resolve(ctx), track.resolve(ctx), height)),
            contentDescription = null,
            contentScale = ContentScale.FillBounds,
            modifier = GlanceModifier.fillMaxWidth().height(height),
        )
    }
}

private fun barBitmap(ctx: Context, progress: Float, fill: Color, track: Color, height: Dp): Bitmap {
    val density = ctx.resources.displayMetrics.density
    val h = (height.value * density).toInt().coerceAtLeast(2)
    val w = (140 * density).toInt().coerceAtLeast(h * 4)
    val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
    val canvas = Canvas(bmp)
    val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    val r = h / 2f
    paint.color = track.toArgb()
    canvas.drawRoundRect(RectF(0f, 0f, w.toFloat(), h.toFloat()), r, r, paint)
    if (progress > 0f) {
        paint.color = fill.toArgb()
        canvas.drawRoundRect(RectF(0f, 0f, (w * progress).coerceAtLeast(h.toFloat()), h.toFloat()), r, r, paint)
    }
    return bmp
}
