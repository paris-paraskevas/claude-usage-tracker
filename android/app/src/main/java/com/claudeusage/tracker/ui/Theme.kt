package com.claudeusage.tracker.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

// Warm dark palette — mirrors the desktop dashboard tokens (claude_usage_tracker.py :root).
val Bg = Color(0xFF100E0C)
val Panel = Color(0xFF1A1613)
val Panel2 = Color(0xFF221E1A)
val CardBg = Color(0xFF1E1A16)
val Card2Bg = Color(0xFF26211C)
val Ink = Color(0xFFF2EDE5)
val Dim = Color(0xFFA99F93)
val Faint = Color(0xFF786F65)
val Accent = Color(0xFFD97757)

// Exact mirror of the desktop palette in claude_usage_tracker.py (USAGE_BANDS + the
// dashboard's JS bandColor) so the tray, widget, desktop dashboard and phone read identically.
private val BandOk = Color(0xFF5E9E72)
private val BandWarn = Color(0xFFCDA24E)
private val BandHigh = Color(0xFFD4694F)
private val BandCrit = Color(0xFFCF6049)
private val BandMax = Color(0xFFC94F38)

/** 5-band severity — matches the desktop's usage_style() used for the limit gauges + tray icon. */
fun usageColor(pct: Double): Color = when {
    pct >= 100 -> BandMax
    pct >= 90 -> BandCrit
    pct >= 80 -> BandHigh
    pct >= 60 -> BandWarn
    else -> BandOk
}

/** 3-band — matches the desktop dashboard's JS bandColor() for the context + session bars. */
fun bandColor(pct: Double): Color = when {
    pct >= 80 -> BandHigh
    pct >= 60 -> BandWarn
    else -> BandOk
}

private val scheme = darkColorScheme(
    primary = Accent,
    onPrimary = Color(0xFF1C0F08),
    background = Bg,
    onBackground = Ink,
    surface = Panel,
    onSurface = Ink,
    surfaceVariant = Panel2,
    onSurfaceVariant = Dim,
)

/** Hex string ("#rrggbb") from the snapshot → Compose Color, with a safe fallback. */
fun hexColor(s: String?, fallback: Color = Dim): Color =
    runCatching { Color(android.graphics.Color.parseColor(s)) }.getOrDefault(fallback)

@Composable
fun AppTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = scheme, content = content)
}
