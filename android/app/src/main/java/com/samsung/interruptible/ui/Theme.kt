package com.samsung.interruptible.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

object Palette {
    val Background = Color(0xFF0B0E14)
    val Surface = Color(0xFF151A23)
    val SurfaceHigh = Color(0xFF1C2330)
    val Accent = Color(0xFF38BDF8)       // sky: the agent
    val Amber = Color(0xFFFBBF24)        // fillers, tool calls
    val Rose = Color(0xFFF43F5E)         // cancellations, listening
    val Emerald = Color(0xFF34D399)      // snapshots, connected
    val Violet = Color(0xFFA78BFA)       // spawned agents
    val Muted = Color(0xFF94A3B8)

    fun forTrace(type: String): Color = when (type) {
        "tool_call", "filler" -> Amber
        "tool_cancel" -> Rose
        "snapshot", "graph_update" -> Emerald
        "agent_step" -> Violet
        "audio", "vision" -> Color(0xFFC084FC)
        "response" -> Accent
        else -> Muted
    }
}

private val Scheme = darkColorScheme(
    primary = Palette.Accent,
    secondary = Palette.Violet,
    tertiary = Palette.Amber,
    background = Palette.Background,
    surface = Palette.Surface,
    surfaceVariant = Palette.SurfaceHigh,
    error = Palette.Rose,
    onBackground = Color(0xFFE2E8F0),
    onSurface = Color(0xFFE2E8F0),
)

@Composable
fun AgentTheme(content: @Composable () -> Unit) {
    // The app is dark-only by design (it mirrors the web console); the parameter keeps previews honest.
    @Suppress("UNUSED_VARIABLE") val system = isSystemInDarkTheme()
    MaterialTheme(colorScheme = Scheme, content = content)
}
