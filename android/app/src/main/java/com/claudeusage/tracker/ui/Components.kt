package com.claudeusage.tracker.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Error
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.claudeusage.tracker.History
import com.claudeusage.tracker.Msg
import com.claudeusage.tracker.Sess
import com.claudeusage.tracker.Snap

// ---- layout ---------------------------------------------------------------

/** A page body: scrolls, consistent padding, and a readable max width on tablets/landscape. */
@Composable
fun PageScroll(content: @Composable ColumnScope.() -> Unit) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.TopCenter) {
        Column(
            Modifier.widthIn(max = 980.dp).fillMaxWidth().verticalScroll(rememberScrollState())
                .padding(horizontal = 20.dp).padding(top = 12.dp, bottom = 28.dp),
            content = content,
        )
    }
}

@Composable
fun PageTitle(title: String, sub: String?) {
    Text(title, color = Ink, style = MaterialTheme.typography.headlineMedium,
        modifier = Modifier.semantics { heading() })
    if (sub != null) Text(sub, color = Faint, style = MaterialTheme.typography.labelMedium)
}

@Composable
fun Card(modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    Column(modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(CardBg).padding(16.dp), content = content)
}

@Composable
fun SectionLabel(t: String) =
    Text(t, color = Faint, style = MaterialTheme.typography.labelSmall, modifier = Modifier.semantics { heading() })

@Composable
fun KV(k: String, v: String, vColor: Color = Ink) {
    Row(Modifier.fillMaxWidth().padding(vertical = 7.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(k, color = Dim, style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(1f))
        Text(v, color = vColor, style = MaterialTheme.typography.bodySmall, fontFamily = MONO, textAlign = TextAlign.End)
    }
}

// ---- data components ------------------------------------------------------

@Composable
fun Bar(pct: Double, color: Color, modifier: Modifier = Modifier) {
    Box(modifier.height(8.dp).clip(RoundedCornerShape(4.dp)).background(Panel2)) {
        Box(
            Modifier.fillMaxHeight().fillMaxWidth((pct.coerceIn(0.0, 100.0) / 100.0).toFloat())
                .clip(RoundedCornerShape(4.dp)).background(color)
        )
    }
}

/** The gauge diameter actually used: grows with the system font scale (so the centred number
 *  never clips) but never wider than [maxWidth]. */
@Composable
fun gaugeSize(dim: Dp, maxWidth: Dp = Dp.Infinity): Dp {
    val fs = LocalDensity.current.fontScale.coerceIn(1f, 1.6f)
    val d = dim * fs
    return if (d > maxWidth) maxWidth else d
}

/** A circular 270° gauge with a big centred percentage; `big` bumps the type for the hero.
 *  TalkBack reads [spoken] (e.g. "5-hour window, 64 percent used, resets in 1 hour 10 minutes")
 *  instead of the individual glyphs. */
@Composable
fun GaugeStat(
    label: String, pct: Double?, color: Color, sub: String?, detail: String?,
    dim: Dp, big: Boolean, spoken: String, modifier: Modifier = Modifier,
    /** Show a decorative ▾ (the parent is a dropdown anchor); hidden from TalkBack. */
    dropdown: Boolean = false,
) {
    Column(
        modifier.clearAndSetSemantics { contentDescription = spoken },
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Box(Modifier.size(dim), contentAlignment = Alignment.Center) {
            val track = Panel2
            Canvas(Modifier.fillMaxSize()) {
                val sw = size.minDimension * 0.09f
                val tl = Offset(sw / 2f, sw / 2f)
                val arc = Size(size.width - sw, size.height - sw)
                drawArc(track, 135f, 270f, false, tl, arc, style = Stroke(sw, cap = StrokeCap.Round))
                val p = (pct ?: 0.0).coerceIn(0.0, 100.0).toFloat()
                if (p > 0f) drawArc(color, 135f, 270f * p / 100f, false, tl, arc, style = Stroke(sw, cap = StrokeCap.Round))
            }
            Row(verticalAlignment = Alignment.Bottom) {
                Text(if (pct != null) "${pct.toInt()}" else "–", color = color, maxLines = 1, softWrap = false,
                    fontSize = if (big) 50.sp else 32.sp, fontWeight = FontWeight.Bold, fontFamily = MONO)
                Text("%", color = Dim, fontSize = if (big) 18.sp else 13.sp, fontFamily = MONO, maxLines = 1,
                    modifier = Modifier.padding(start = 2.dp, bottom = if (big) 6.dp else 4.dp))
            }
        }
        Spacer(Modifier.height(10.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(label, color = Ink, style = if (big) MaterialTheme.typography.titleSmall else MaterialTheme.typography.bodySmall,
                fontWeight = FontWeight.Medium, textAlign = TextAlign.Center)
            if (dropdown) Icon(Icons.Filled.ArrowDropDown, contentDescription = null, tint = Faint, modifier = Modifier.size(18.dp))
        }
        if (sub != null) Text(sub, color = Faint, style = MaterialTheme.typography.labelMedium, textAlign = TextAlign.Center)
        if (detail != null) Text(detail, color = Dim, style = MaterialTheme.typography.labelMedium, textAlign = TextAlign.Center)
    }
}

/** A small secondary row for a scoped weekly window (Opus / Sonnet): label, bar, %, reset. */
@Composable
fun MiniWindowRow(label: String, pct: Double, sub: String?) {
    Column(Modifier.fillMaxWidth().padding(vertical = 6.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(label, color = Dim, style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(1f))
            Text("${pct.toInt()}%", color = usageColor(pct), style = MaterialTheme.typography.bodySmall,
                fontWeight = FontWeight.Bold, fontFamily = MONO)
        }
        Spacer(Modifier.height(4.dp))
        Bar(pct, usageColor(pct), Modifier.fillMaxWidth())
        if (sub != null) Text(sub, color = Faint, style = MaterialTheme.typography.labelMedium,
            modifier = Modifier.padding(top = 3.dp))
    }
}

@Composable
fun SessionCard(s: Sess) {
    Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(Panel).padding(14.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Box(Modifier.size(8.dp).clip(CircleShape).background(if (s.active) LiveGreen else Faint))
            Text(s.name, color = Ink, style = MaterialTheme.typography.titleMedium, maxLines = 1,
                modifier = Modifier.padding(start = 10.dp).weight(1f))
            Text("${s.pct.toInt()}%", color = bandColor(s.pct), style = MaterialTheme.typography.headlineSmall,
                fontFamily = MONO)
        }
        Spacer(Modifier.height(10.dp))
        Bar(s.pct, bandColor(s.pct), Modifier.fillMaxWidth())
        Spacer(Modifier.height(8.dp))
        Text("${Snap.fmtTokens(s.tokens)} tokens · context ${s.pct.toInt()}% · ${if (s.active) "active" else "idle"}",
            color = Faint, style = MaterialTheme.typography.labelMedium)
    }
}

@Composable
fun StatTile(label: String, value: String, modifier: Modifier = Modifier) {
    Column(modifier.clip(RoundedCornerShape(12.dp)).background(Panel).padding(16.dp)) {
        Text(value, color = Ink, style = MaterialTheme.typography.headlineSmall, fontFamily = MONO, maxLines = 2)
        Spacer(Modifier.height(4.dp))
        Text(label, color = Faint, style = MaterialTheme.typography.bodySmall, fontSize = 12.sp)
    }
}

@Composable
fun VerdictBanner(text: String, color: Color) {
    Box(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp))
            .background(color.copy(alpha = 0.14f)).padding(horizontal = 16.dp, vertical = 12.dp)
    ) {
        Text(text.uppercase(), color = color, style = MaterialTheme.typography.bodySmall,
            fontWeight = FontWeight.SemiBold, letterSpacing = 0.5.sp)
    }
}

/** Anthropic status: an icon + the word, so it never relies on colour alone. */
@Composable
fun StatusChip(word: String, color: Color) {
    val icon = when (word) {
        "Ok" -> Icons.Filled.CheckCircle
        "Down" -> Icons.Filled.Error
        else -> Icons.Filled.Warning
    }
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.padding(end = 4.dp).semantics(mergeDescendants = true) {
            contentDescription = "Claude service status: $word"
        },
    ) {
        Icon(icon, contentDescription = null, tint = color, modifier = Modifier.size(14.dp))
        Text(word, color = color, style = MaterialTheme.typography.labelMedium, modifier = Modifier.padding(start = 4.dp))
    }
}

@Composable
fun WaitingForSync() {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Text("Waiting for your desktop", color = Ink, style = MaterialTheme.typography.titleLarge)
        Spacer(Modifier.height(10.dp))
        Text(
            "Syncing for the first time. Keep the desktop app open with Remote (phone) " +
                "enabled — this usually takes a few seconds.",
            color = Dim, style = MaterialTheme.typography.bodySmall, textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(24.dp))
        LinearProgressIndicator(
            modifier = Modifier.fillMaxWidth(0.62f).height(5.dp).clip(RoundedCornerShape(3.dp)),
            color = Accent, trackColor = Panel2,
        )
    }
}

@Composable
fun ErrorState(msg: String, onRetry: () -> Unit, onRemove: (() -> Unit)?) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Icon(Icons.Filled.Error, contentDescription = null, tint = Danger, modifier = Modifier.size(32.dp))
        Spacer(Modifier.height(10.dp))
        Text(msg, color = Danger, style = MaterialTheme.typography.bodyLarge, textAlign = TextAlign.Center)
        Spacer(Modifier.height(14.dp))
        TextButton(onClick = onRetry) { Text("Try again", color = Accent) }
        if (onRemove != null) TextButton(onClick = onRemove) { Text("Remove this account", color = Dim) }
    }
}

/** Two-line sparkline (5h + weekly) of the desktop's recent samples. */
@Composable
fun Sparkline(h: History, modifier: Modifier = Modifier) {
    val five = usageColor(0.0)
    val week = Accent
    val grid = Panel2
    val first = h.t.first(); val last = h.t.last()
    val hours = ((last - first) / 3600.0)
    val spoken = "Usage trend over the last ${String.format("%.0f", maxOf(1.0, hours))} hours: " +
        "5-hour window from ${h.fiveHour.first().toInt()} to ${h.fiveHour.last().toInt()} percent, " +
        "weekly from ${h.sevenDay.first().toInt()} to ${h.sevenDay.last().toInt()} percent"
    Column(modifier.clearAndSetSemantics { contentDescription = spoken }) {
        Canvas(Modifier.fillMaxWidth().height(96.dp)) {
            val span = (last - first).coerceAtLeast(1).toFloat()
            fun x(i: Int) = (h.t[i] - first) / span * size.width
            fun y(p: Double) = size.height - (p.coerceIn(0.0, 100.0) / 100.0).toFloat() * size.height
            listOf(0.0, 50.0, 100.0).forEach { g ->
                drawLine(grid, Offset(0f, y(g)), Offset(size.width, y(g)), strokeWidth = 1.dp.toPx())
            }
            fun line(vals: DoubleArray, c: Color) {
                val path = Path()
                for (i in vals.indices) {
                    if (i == 0) path.moveTo(x(i), y(vals[i])) else path.lineTo(x(i), y(vals[i]))
                }
                drawPath(path, c, style = Stroke(2.dp.toPx(), cap = StrokeCap.Round))
            }
            line(h.sevenDay, week)
            line(h.fiveHour, five)
        }
        Spacer(Modifier.height(6.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(14.dp)) {
            Legend("5-hour", five); Legend("Weekly", week)
            Text("last ${fmtDuration((last - first))}", color = Faint, style = MaterialTheme.typography.labelMedium)
        }
    }
}

@Composable
private fun Legend(t: String, c: Color) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(10.dp, 3.dp).background(c))
        Text(" $t", color = Dim, style = MaterialTheme.typography.labelMedium)
    }
}

// ---- mirrored conversation (read-only) ------------------------------------

@Composable
fun MsgBubble(m: Msg) {
    val user = m.role == "user"
    val who = if (user) "You" else "Claude"
    val time = m.ts?.let { fmtClockShort(it) }
    Row(Modifier.fillMaxWidth(), horizontalArrangement = if (user) Arrangement.End else Arrangement.Start) {
        Column(
            Modifier.fillMaxWidth(0.88f).clip(RoundedCornerShape(12.dp))
                .background(if (user) Accent.copy(alpha = 0.16f) else Panel).padding(12.dp),
        ) {
            Text(if (time != null) "$who · $time" else who, color = if (user) Accent else Dim,
                style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(4.dp))
            MarkdownText(m.text, Ink)
        }
    }
}

private fun fmtClockShort(epochMs: Long): String {
    val locale = java.util.Locale.getDefault()
    val pattern = android.text.format.DateFormat.getBestDateTimePattern(locale, "jmm")
    return java.text.SimpleDateFormat(pattern, locale).format(java.util.Date(epochMs))
}

/** Minimal, dependency-free Markdown renderer for the mirrored conversation. Claude's replies are
 *  Markdown; without this the bubbles showed raw "**bold**", "# heading", "- item", `code`. Covers
 *  the common subset — fenced code blocks, ATX headings, bullet/numbered lists, blockquotes, and
 *  inline **bold** / *italic* / `code` / [links]. Anything unrecognized stays literal. */
@Composable
fun MarkdownText(text: String, color: Color) {
    val codeBg = Panel2
    val linkColor = Accent
    val quoteColor = Dim
    fun md(s: String) = inlineMd(s, codeBg, color, linkColor)
    val lines = text.split("\n")
    Column(Modifier.fillMaxWidth()) {
        var i = 0
        while (i < lines.size) {
            val raw = lines[i]
            val line = raw.trimStart()
            if (line.startsWith("```")) {
                val code = StringBuilder()
                i++
                while (i < lines.size && !lines[i].trimStart().startsWith("```")) {
                    code.append(lines[i]).append('\n'); i++
                }
                i++  // consume the closing fence
                CodeBlock(code.toString().trimEnd('\n'))
                continue
            }
            val ordered = ORDERED.find(line)
            when {
                line.startsWith("### ") -> MdLine(md(line.removePrefix("### ")), color, 15, FontWeight.SemiBold)
                line.startsWith("## ") -> MdLine(md(line.removePrefix("## ")), color, 17, FontWeight.Bold)
                line.startsWith("# ") -> MdLine(md(line.removePrefix("# ")), color, 19, FontWeight.Bold)
                line.startsWith("> ") -> MdLine(md(line.removePrefix("> ")), quoteColor, 14)
                line.startsWith("- ") || line.startsWith("* ") || line.startsWith("+ ") ->
                    BulletLine("•", md(line.drop(2)), color)
                ordered != null ->
                    BulletLine(ordered.groupValues[1] + ".", md(line.removePrefix(ordered.value)), color)
                raw.isBlank() -> Spacer(Modifier.height(6.dp))
                else -> MdLine(md(raw), color, 14)
            }
            i++
        }
    }
}

private val ORDERED = Regex("^(\\d+)\\. ")

@Composable
private fun MdLine(text: AnnotatedString, color: Color, sizeSp: Int, weight: FontWeight? = null) {
    Text(text, color = color, fontSize = sizeSp.sp, fontWeight = weight, lineHeight = (sizeSp + 6).sp,
        modifier = Modifier.padding(vertical = 1.dp))
}

@Composable
private fun BulletLine(marker: String, text: AnnotatedString, color: Color) {
    Row(Modifier.fillMaxWidth().padding(vertical = 1.dp)) {
        Text("$marker ", color = Dim, style = MaterialTheme.typography.bodyMedium, fontFamily = MONO)
        Text(text, color = color, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
    }
}

@Composable
private fun CodeBlock(code: String) {
    Text(
        code, color = Ink, fontSize = 13.sp, fontFamily = MONO, lineHeight = 18.sp,
        modifier = Modifier.padding(vertical = 4.dp).fillMaxWidth()
            .clip(RoundedCornerShape(8.dp)).background(Bg).padding(10.dp),
    )
}

/** Inline Markdown → styled text: **bold**, *italic*, `code`, [text](url). Underscores are left
 *  literal on purpose so snake_case / file_names aren't mangled. Unbalanced markers stay literal. */
private fun inlineMd(src: String, codeBg: Color, ink: Color, link: Color): AnnotatedString = buildAnnotatedString {
    var i = 0
    while (i < src.length) {
        val c = src[i]
        when {
            c == '*' && i + 1 < src.length && src[i + 1] == '*' -> {
                val end = src.indexOf("**", i + 2)
                if (end > i + 1) {
                    withStyle(SpanStyle(fontWeight = FontWeight.Bold)) { append(src.substring(i + 2, end)) }
                    i = end + 2
                } else { append(c); i++ }
            }
            c == '*' -> {
                val end = src.indexOf('*', i + 1)
                if (end > i) {
                    withStyle(SpanStyle(fontStyle = FontStyle.Italic)) { append(src.substring(i + 1, end)) }
                    i = end + 1
                } else { append(c); i++ }
            }
            c == '`' -> {
                val end = src.indexOf('`', i + 1)
                if (end > i) {
                    withStyle(SpanStyle(fontFamily = MONO, background = codeBg, color = ink)) {
                        append(" " + src.substring(i + 1, end) + " ")
                    }
                    i = end + 1
                } else { append(c); i++ }
            }
            c == '[' -> {
                val close = src.indexOf(']', i + 1)
                if (close > i && close + 1 < src.length && src[close + 1] == '(') {
                    val urlEnd = src.indexOf(')', close + 2)
                    if (urlEnd > close) {
                        withStyle(SpanStyle(color = link, textDecoration = TextDecoration.Underline)) {
                            append(src.substring(i + 1, close))
                        }
                        i = urlEnd + 1
                    } else { append(c); i++ }
                } else { append(c); i++ }
            }
            else -> { append(c); i++ }
        }
    }
}
