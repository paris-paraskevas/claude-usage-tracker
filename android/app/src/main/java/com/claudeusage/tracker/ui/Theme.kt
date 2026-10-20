package com.claudeusage.tracker.ui

import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.ColorScheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

/** The app's colour tokens (mirrors the desktop dashboard's :root variables). Read them through
 *  the composable getters below ([Bg], [Ink], …) so they follow light/dark/dynamic theming. */
@Immutable
data class Palette(
    val bg: Color, val panel: Color, val panel2: Color, val card: Color, val card2: Color,
    val ink: Color, val dim: Color, val faint: Color, val accent: Color,
    /** Error / destructive text (readable on [bg] and [card]). */
    val danger: Color,
    /** Amber "attention" text (stale data, warnings). */
    val warn: Color,
)

// Warm dark palette — the brand look; mirrors the desktop dashboard tokens (claude_usage_tracker.py :root).
// Faint is the desktop's contrast-fixed value (>= 4.5:1 on every panel).
private val BrandDark = Palette(
    bg = Color(0xFF100E0C), panel = Color(0xFF1A1613), panel2 = Color(0xFF221E1A),
    card = Color(0xFF1E1A16), card2 = Color(0xFF26211C),
    ink = Color(0xFFF2EDE5), dim = Color(0xFFA99F93), faint = Color(0xFF948A7F),
    accent = Color(0xFFD97757), danger = Color(0xFFD4694F), warn = Color(0xFFCDA24E),
)

// Warm light counterpart (used below Android 12, where there is no dynamic colour).
private val BrandLight = Palette(
    bg = Color(0xFFF5F1EA), panel = Color(0xFFFBF9F5), panel2 = Color(0xFFE6DFD4),
    card = Color(0xFFFFFFFF), card2 = Color(0xFFF1ECE4),
    ink = Color(0xFF1F1A15), dim = Color(0xFF574E45), faint = Color(0xFF6B6258),
    accent = Color(0xFFB4532F), danger = Color(0xFFB3412A), warn = Color(0xFF8A6514),
)

private fun Palette.toScheme(dark: Boolean): ColorScheme =
    if (dark) darkColorScheme(
        primary = accent, onPrimary = Color(0xFF1C0F08),
        background = bg, onBackground = ink,
        surface = panel, onSurface = ink,
        surfaceVariant = panel2, onSurfaceVariant = dim,
        surfaceContainerLowest = bg, surfaceContainerLow = card, surfaceContainer = panel,
        surfaceContainerHigh = card2, surfaceContainerHighest = panel2,
        outline = faint, error = danger,
    ) else lightColorScheme(
        primary = accent, onPrimary = Color.White,
        background = bg, onBackground = ink,
        surface = panel, onSurface = ink,
        surfaceVariant = panel2, onSurfaceVariant = dim,
        surfaceContainerLowest = card, surfaceContainerLow = card, surfaceContainer = panel,
        surfaceContainerHigh = card2, surfaceContainerHighest = panel2,
        outline = faint, error = danger,
    )

/** Palette derived from a (dynamic) Material scheme, so wallpaper colours flow through. */
private fun ColorScheme.toPalette(warn: Color) = Palette(
    bg = background, panel = surfaceContainer, panel2 = surfaceContainerHighest,
    card = surfaceContainerLow, card2 = surfaceContainerHigh,
    ink = onSurface, dim = onSurfaceVariant, faint = outline, accent = primary,
    danger = error, warn = warn,
)

private val LocalPalette = staticCompositionLocalOf { BrandDark }

val Bg: Color
    @Composable @ReadOnlyComposable get() = LocalPalette.current.bg
val Panel: Color
    @Composable @ReadOnlyComposable get() = LocalPalette.current.panel
val Panel2: Color
    @Composable @ReadOnlyComposable get() = LocalPalette.current.panel2
val CardBg: Color
    @Composable @ReadOnlyComposable get() = LocalPalette.current.card
val Card2Bg: Color
    @Composable @ReadOnlyComposable get() = LocalPalette.current.card2
val Ink: Color
    @Composable @ReadOnlyComposable get() = LocalPalette.current.ink
val Dim: Color
    @Composable @ReadOnlyComposable get() = LocalPalette.current.dim
val Faint: Color
    @Composable @ReadOnlyComposable get() = LocalPalette.current.faint
val Accent: Color
    @Composable @ReadOnlyComposable get() = LocalPalette.current.accent
val Danger: Color
    @Composable @ReadOnlyComposable get() = LocalPalette.current.danger
val Warn: Color
    @Composable @ReadOnlyComposable get() = LocalPalette.current.warn

// Exact mirror of the desktop palette in claude_usage_tracker.py (USAGE_BANDS + the
// dashboard's JS bandColor) so the tray, widget, desktop dashboard and phone read identically.
// Fixed across themes: they are severity colours, always paired with a number/word.
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

/** The "good/live" green used for active dots (always paired with a text label). */
val LiveGreen = BandOk

/** Hex string ("#rrggbb") from the snapshot → Compose Color, with a safe fallback. */
fun hexColor(s: String?, fallback: Color = Color.Gray): Color =
    if (s.isNullOrBlank()) fallback
    else runCatching { Color(android.graphics.Color.parseColor(s)) }.getOrDefault(fallback)

val MONO = FontFamily.Monospace

/** Type scale. Sizes match the previous hand-tuned values; they're sp, so they follow the
 *  system font scale (gauges compensate, see GaugeStat). */
val AppTypography = Typography(
    displayMedium = TextStyle(fontSize = 46.sp, fontWeight = FontWeight.Bold, fontFamily = MONO),
    headlineMedium = TextStyle(fontSize = 26.sp, fontWeight = FontWeight.Bold),
    headlineSmall = TextStyle(fontSize = 22.sp, fontWeight = FontWeight.Bold),
    titleLarge = TextStyle(fontSize = 18.sp, fontWeight = FontWeight.SemiBold),
    titleMedium = TextStyle(fontSize = 16.sp, fontWeight = FontWeight.Medium),
    titleSmall = TextStyle(fontSize = 15.sp, fontWeight = FontWeight.Medium),
    bodyLarge = TextStyle(fontSize = 15.sp, lineHeight = 21.sp),
    bodyMedium = TextStyle(fontSize = 14.sp, lineHeight = 20.sp),
    bodySmall = TextStyle(fontSize = 13.sp, lineHeight = 18.sp),
    labelLarge = TextStyle(fontSize = 14.sp, fontWeight = FontWeight.Medium),
    labelMedium = TextStyle(fontSize = 12.sp, fontFamily = MONO),
    labelSmall = TextStyle(fontSize = 11.sp, fontFamily = MONO, letterSpacing = 1.sp),
)

/** Brand dark palette in dark mode; in light mode, wallpaper-based dynamic colour on
 *  Android 12+ and the warm brand light palette below that. Follows the system setting. */
@Composable
fun AppTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    dynamicColor: Boolean = true,
    content: @Composable () -> Unit,
) {
    val ctx = LocalContext.current
    val (scheme, palette) = when {
        darkTheme -> BrandDark.toScheme(dark = true) to BrandDark
        dynamicColor && Build.VERSION.SDK_INT >= Build.VERSION_CODES.S -> {
            val s = dynamicLightColorScheme(ctx)
            s to s.toPalette(warn = BrandLight.warn)
        }
        else -> BrandLight.toScheme(dark = false) to BrandLight
    }
    CompositionLocalProvider(LocalPalette provides palette) {
        MaterialTheme(colorScheme = scheme, typography = AppTypography, content = content)
    }
}
