package com.samsung.interruptible.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.samsung.interruptible.data.Settings
import com.samsung.interruptible.data.Urls
import com.samsung.interruptible.state.ChatState
import com.samsung.interruptible.state.TraceItem
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** The agent's own view of the session: epoch, intent, filled slots and calls in flight (the §2.3 state snapshot). */
@OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)
@Composable
fun StatePanel(state: ChatState) {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)) {
        SectionTitle("Epoch")
        Text("#${state.epoch}", fontSize = 28.sp, fontWeight = FontWeight.Bold, color = Palette.Accent)
        Text("Every correction or interrupt starts a new epoch; work from an older one is cancelled.", fontSize = 12.sp, color = Palette.Muted)

        SectionTitle("Intent")
        Text(state.intent ?: "—", fontSize = 16.sp)

        SectionTitle("Slots")
        if (state.slots.isEmpty()) Text("nothing collected yet", color = Palette.Muted, fontSize = 13.sp)
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            state.slots.forEach { (k, v) ->
                Row(Modifier.clip(RoundedCornerShape(50)).background(Palette.SurfaceHigh).padding(horizontal = 10.dp, vertical = 5.dp)) {
                    Text("$k ", color = Palette.Muted, fontSize = 12.sp)
                    Text(v, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                }
            }
        }

        SectionTitle("In flight")
        if (state.inFlight.isEmpty()) Text("no running calls", color = Palette.Muted, fontSize = 13.sp)
        state.inFlight.forEach {
            Row(Modifier.fillMaxWidth().padding(vertical = 3.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(8.dp).background(Palette.Amber, CircleShape))
                Text("  ${it.tool}", fontWeight = FontWeight.SemiBold)
                Text("  ${it.status} · epoch ${it.epoch}", color = Palette.Muted, fontSize = 12.sp)
            }
        }
    }
}

private val timeFormat = SimpleDateFormat("HH:mm:ss", Locale.US)

/** A horizontal timeline of everything that happened (cancellations stand out), then the full log. */
@Composable
fun TracePanel(state: ChatState) {
    Column(Modifier.fillMaxSize()) {
        TimelineStrip(state.trace)
        LazyColumn(Modifier.weight(1f), contentPadding = androidx.compose.foundation.layout.PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            items(state.trace.asReversed(), key = { it.id }) { item ->
                Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(8.dp)).background(Palette.Surface).padding(8.dp)) {
                    Box(Modifier.padding(top = 5.dp).size(8.dp).background(Palette.forTrace(item.type), CircleShape))
                    Column(Modifier.padding(start = 8.dp)) {
                        Row {
                            Text(item.type, color = Palette.forTrace(item.type), fontSize = 11.sp, fontFamily = FontFamily.Monospace)
                            Text("  ${timeFormat.format(Date(item.atMs))}", color = Palette.Muted, fontSize = 11.sp)
                        }
                        Text(item.text, fontSize = 12.sp, fontFamily = FontFamily.Monospace)
                    }
                }
            }
        }
    }
}

@Composable
private fun TimelineStrip(items: List<TraceItem>) {
    if (items.isEmpty()) return
    val first = items.first().atMs
    val span = maxOf(items.last().atMs - first, 1_000L)
    Column(Modifier.fillMaxWidth().background(Palette.Surface).padding(12.dp)) {
        Row {
            Text("TRACE TIMELINE", fontSize = 10.sp, color = Palette.Muted, modifier = Modifier.weight(1f))
            Text("%.1fs span".format(span / 1000.0), fontSize = 10.sp, color = Palette.Muted)
        }
        Canvas(Modifier.fillMaxWidth().height(28.dp)) {
            drawLine(Color.White.copy(alpha = 0.15f), Offset(0f, size.height / 2), Offset(size.width, size.height / 2), strokeWidth = 2f)
            items.forEach { item ->
                val x = ((item.atMs - first).toFloat() / span).coerceIn(0f, 1f) * size.width
                val cancel = item.type == "tool_cancel"
                drawCircle(Palette.forTrace(item.type), radius = if (cancel) 9f else 6f, center = Offset(x, size.height / 2))
            }
        }
    }
}

/** The cognitive graph: turns left to right, their entities and artifacts hanging underneath. */
@Composable
fun GraphPanel(state: ChatState) {
    if (state.graphNodes.isEmpty()) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text("The graph appears after the first completed turn.", color = Palette.Muted)
        }
        return
    }
    val layout = remember(state.graphNodes, state.graphEdges) { GraphLayout.compute(state.graphNodes, state.graphEdges) }
    val density = androidx.compose.ui.platform.LocalDensity.current
    Box(Modifier.fillMaxSize().horizontalScroll(rememberScrollState()).verticalScroll(rememberScrollState())) {
        // GraphLayout works in dp; the canvas below converts to pixels itself.
        Box(Modifier.size(layout.width.dp, layout.height.dp)) {
            Canvas(Modifier.fillMaxSize()) {
                val nodeW = 150f * density.density
                state.graphEdges.forEach { e ->
                    val a = layout.positions[e.source] ?: return@forEach
                    val b = layout.positions[e.target] ?: return@forEach
                    val supersedes = e.edgeType == "SUPERSEDES"
                    drawLine(
                        color = if (supersedes) Palette.Rose else Color.White.copy(alpha = 0.25f),
                        start = Offset(a.x * density.density + nodeW / 2, a.y * density.density + 14f * density.density),
                        end = Offset(b.x * density.density + nodeW / 2, b.y * density.density + 14f * density.density),
                        strokeWidth = if (supersedes) 3f else 2f,
                        pathEffect = if (supersedes) PathEffect.dashPathEffect(floatArrayOf(14f, 10f)) else null,
                    )
                }
            }
            state.graphNodes.forEach { n ->
                val p = layout.positions[n.id] ?: return@forEach
                val color = when (n.nodeType) { "turn" -> Palette.Accent; "artifact" -> Palette.Violet; else -> Palette.Emerald }
                Text(
                    n.label.take(34), fontSize = 11.sp, maxLines = 2, color = Color.White,
                    modifier = Modifier
                        .offset { IntOffset((p.x * density.density).toInt(), (p.y * density.density).toInt()) }
                        .width(150.dp).clip(RoundedCornerShape(8.dp))
                        .background(color.copy(alpha = 0.25f)).padding(horizontal = 8.dp, vertical = 6.dp),
                )
            }
        }
    }
}

/** The connection form. Separate from the dialog window so it can be rendered and tested on its own. */
@Composable
fun SettingsForm(initial: Settings, onSave: (Settings) -> Unit, onCancel: () -> Unit) {
    var server by remember { mutableStateOf(initial.serverUrl) }
    var token by remember { mutableStateOf(initial.token) }
    var session by remember { mutableStateOf(initial.sessionId) }
    val insecure = Urls.isInsecureRemote(server)

    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("Connection", fontSize = 20.sp, fontWeight = FontWeight.SemiBold)
        OutlinedTextField(server, { server = it }, label = { Text("Server") }, singleLine = true,
            supportingText = { Text("ws://10.0.2.2:8000 reaches your computer from the emulator") })
        OutlinedTextField(token, { token = it }, label = { Text("Access token (AUTH_TOKEN)") }, singleLine = true)
        OutlinedTextField(session, { session = it }, label = { Text("Session id") }, singleLine = true,
            isError = session.isNotBlank() && !Urls.isValidSessionId(session),
            supportingText = { Text("Letters, digits, - and _ (max 64). Same id = same conversation.") })
        if (insecure) {
            Text("This address is not encrypted and not local: anyone on the network could read your token and conversation. Use wss://.",
                color = Palette.Amber, fontSize = 12.sp)
        }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
            TextButton(onClick = onCancel) { Text("Cancel") }
            TextButton(onClick = { onSave(initial.copy(serverUrl = server.trim(), token = token.trim(), sessionId = session)) }) {
                Text("Save & reconnect")
            }
        }
    }
}

@Composable
fun SettingsDialog(initial: Settings, onDismiss: () -> Unit, onSave: (Settings) -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        text = { SettingsForm(initial, onSave, onDismiss) },
        confirmButton = {},
    )
}
