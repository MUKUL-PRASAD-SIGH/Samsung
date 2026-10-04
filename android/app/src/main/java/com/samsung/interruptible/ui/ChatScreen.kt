package com.samsung.interruptible.ui

import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.view.CameraController
import androidx.camera.view.LifecycleCameraController
import androidx.camera.view.PreviewView
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.MicOff
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material.icons.filled.Videocam
import androidx.compose.material.icons.filled.VideocamOff
import androidx.compose.material.icons.filled.VolumeOff
import androidx.compose.material.icons.filled.VolumeUp
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import com.samsung.interruptible.camera.FrameSampler
import com.samsung.interruptible.state.AgentCard
import com.samsung.interruptible.state.ChatMessage
import com.samsung.interruptible.state.ChatState
import com.samsung.interruptible.state.Role
import java.util.concurrent.Executors

@Composable
fun ChatScreen(
    state: ChatState,
    onSend: (String) -> Boolean,
    onStop: () -> Unit,
    onToggleMic: () -> Unit,
    onToggleSpeak: () -> Unit,
    onToggleCamera: () -> Unit,
    onFrame: (ByteArray, Int, Int) -> Boolean,
    onDismissError: () -> Unit,
) {
    var draft by remember { mutableStateOf("") }
    val listState = rememberLazyListState()
    val busy = state.inFlight.isNotEmpty() || state.agentSpeaking || state.agents.any { it.status == "working" }

    LaunchedEffect(state.messages.size, state.agents.size) {
        val last = state.messages.size + state.agents.size + (if (state.artifact != null) 1 else 0) - 1
        if (last >= 0) listState.animateScrollToItem(last)
    }

    Column(Modifier.fillMaxSize()) {
        state.error?.let { ErrorBanner(it, onDismissError) }
        if (state.sharing) SharingChip(onFrame = onFrame, onStop = onToggleCamera, lastFrameAtMs = state.lastFrameAtMs)

        LazyColumn(
            modifier = Modifier.weight(1f).fillMaxWidth(),
            state = listState,
            contentPadding = androidx.compose.foundation.layout.PaddingValues(12.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            if (state.messages.isEmpty() && state.agents.isEmpty()) item { EmptyHint(state.connection.toString().contains("Connected")) }
            items(state.messages, key = { it.id }) { MessageBubble(it) }
            items(state.agents, key = { it.callId }) { AgentCardView(it) }
            if (state.artifact != null || state.artifactLoading) item { ArtifactCard(state) }
        }

        if (state.handsFree) ListeningBar(state)
        if (busy) LinearProgressIndicator(Modifier.fillMaxWidth().height(2.dp), color = Palette.Accent)

        Row(Modifier.fillMaxWidth().background(Palette.Surface).padding(horizontal = 8.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
            IconToggle(state.handsFree, Icons.Filled.Mic, Icons.Filled.MicOff, "Hands-free voice", Palette.Rose, onToggleMic)
            if (state.ttsAvailable) IconToggle(state.speakReplies, Icons.Filled.VolumeUp, Icons.Filled.VolumeOff, "Speak replies", Palette.Emerald, onToggleSpeak)
            IconToggle(state.sharing, Icons.Filled.Videocam, Icons.Filled.VideocamOff, "Share camera", Palette.Emerald, onToggleCamera)
            OutlinedTextField(
                value = draft, onValueChange = { draft = it }, modifier = Modifier.weight(1f).padding(horizontal = 4.dp),
                placeholder = { Text("Ask, or interrupt…") }, maxLines = 4,
            )
            if (busy && draft.isBlank()) {
                IconButton(onClick = onStop, colors = IconButtonDefaults.iconButtonColors(containerColor = Palette.Rose)) {
                    Icon(Icons.Filled.Stop, contentDescription = "Stop")
                }
            } else {
                IconButton(onClick = { if (onSend(draft)) draft = "" }, enabled = draft.isNotBlank()) {
                    Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "Send", tint = if (draft.isBlank()) Palette.Muted else Palette.Accent)
                }
            }
        }
    }
}

@Composable
private fun IconToggle(on: Boolean, onIcon: androidx.compose.ui.graphics.vector.ImageVector, offIcon: androidx.compose.ui.graphics.vector.ImageVector,
                       description: String, activeColor: Color, onClick: () -> Unit) {
    IconButton(onClick = onClick) {
        Icon(if (on) onIcon else offIcon, contentDescription = description, tint = if (on) activeColor else Palette.Muted)
    }
}

@Composable
private fun EmptyHint(connected: Boolean) {
    Column(Modifier.fillMaxWidth().padding(top = 48.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Text("Say or type something", fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
        Text(
            "Try “Find flights from Delhi to Mumbai”, then interrupt: “No wait, make it Goa”.",
            color = Palette.Muted, fontSize = 13.sp, modifier = Modifier.padding(top = 6.dp, start = 24.dp, end = 24.dp),
        )
        if (!connected) Text("Waiting for the server…", color = Palette.Amber, fontSize = 12.sp, modifier = Modifier.padding(top = 12.dp))
    }
}

@Composable
private fun ErrorBanner(text: String, onDismiss: () -> Unit) {
    Row(Modifier.fillMaxWidth().background(Palette.Rose.copy(alpha = 0.18f)).padding(horizontal = 12.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(text, modifier = Modifier.weight(1f), fontSize = 12.sp)
        TextButton(onClick = onDismiss) { Text("Dismiss") }
    }
}

@Composable
private fun ListeningBar(state: ChatState) {
    val label = when {
        state.userSpeaking -> "Hearing you…  ${state.livePartial}"
        state.agentSpeaking && state.ducked -> "Hearing you over the agent…"
        state.agentSpeaking -> "Speaking — talk to interrupt"
        else -> "Listening — speak any time to interrupt"
    }
    Row(
        Modifier.fillMaxWidth().background(Palette.Accent.copy(alpha = 0.10f)).padding(horizontal = 12.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(Icons.Filled.Mic, null, tint = if (state.userSpeaking) Palette.Rose else Palette.Accent, modifier = Modifier.size(16.dp))
        Text("  $label", fontSize = 12.sp, color = Palette.Accent, maxLines = 1)
    }
}

@Composable
private fun MessageBubble(m: ChatMessage) {
    val isUser = m.role == Role.USER
    val bg = when {
        isUser -> Palette.Accent.copy(alpha = 0.22f)
        m.role == Role.FILLER -> Palette.Amber.copy(alpha = 0.10f)
        m.isQuestion -> Palette.Violet.copy(alpha = 0.20f)
        else -> Palette.SurfaceHigh
    }
    Row(Modifier.fillMaxWidth(), horizontalArrangement = if (isUser) Arrangement.End else Arrangement.Start) {
        Column(
            Modifier.widthIn(max = 320.dp).clip(RoundedCornerShape(16.dp)).background(bg).padding(horizontal = 12.dp, vertical = 8.dp),
        ) {
            if (m.role == Role.FILLER) {
                Text(m.text, color = Palette.Amber, fontSize = 13.sp, fontStyle = FontStyle.Italic)
            } else {
                MarkdownText(m.text, alpha = if (m.pendingVoice) 0.6f else 1f)
            }
            m.heard?.let {
                Text(
                    if (it.isBlank()) "You interrupted before this was heard" else "You heard: “${it.trim()}” … then interrupted",
                    color = Palette.Muted, fontSize = 11.sp, modifier = Modifier.padding(top = 4.dp),
                )
            }
        }
    }
}

@Composable
private fun MarkdownText(text: String, alpha: Float = 1f) {
    Column {
        Markdown.parse(text).forEach { (bullet, spans) ->
            Text(
                text = buildAnnotatedString {
                    if (bullet) append("•  ")
                    spans.forEach { s ->
                        val style = SpanStyle(
                            fontWeight = if (s.bold) FontWeight.Bold else null,
                            fontFamily = if (s.code) FontFamily.Monospace else null,
                            background = if (s.code) Color.White.copy(alpha = 0.10f) else Color.Unspecified,
                        )
                        withStyle(style) { append(s.text) }
                    }
                },
                fontSize = 14.sp, color = Color.White.copy(alpha = alpha),
            )
        }
    }
}

@Composable
private fun AgentCardView(card: AgentCard) {
    val accent = when (card.status) { "completed" -> Palette.Emerald; "cancelled" -> Palette.Rose; else -> Palette.Violet }
    Card(colors = CardDefaults.cardColors(containerColor = Palette.Surface), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(8.dp).background(accent, RoundedCornerShape(50)))
                Text("  ${card.name}", fontWeight = FontWeight.SemiBold, color = accent)
                Text("  ${card.role}", color = Palette.Muted, fontSize = 12.sp)
            }
            Text(card.thought, fontSize = 13.sp, modifier = Modifier.padding(top = 4.dp))
            if (card.totalSteps > 0) {
                LinearProgressIndicator(
                    progress = { card.step.toFloat() / card.totalSteps }, color = accent,
                    modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
                )
            }
        }
    }
}

@Composable
private fun ArtifactCard(state: ChatState) {
    val clipboard = LocalClipboardManager.current
    Card(colors = CardDefaults.cardColors(containerColor = Palette.Surface), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp)) {
            val a = state.artifact
            if (a == null) {
                Text("Building…", color = Palette.Muted)
                LinearProgressIndicator(Modifier.fillMaxWidth().padding(top = 8.dp), color = Palette.Violet)
            } else {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(a.title, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                    Text(a.language, color = Palette.Muted, fontSize = 11.sp)
                    TextButton(onClick = { clipboard.setText(AnnotatedString(a.content)) }) { Text("Copy") }
                }
                state.artifactAuthor?.let { Text("by $it", color = Palette.Muted, fontSize = 11.sp) }
                Text(
                    a.content, fontFamily = FontFamily.Monospace, fontSize = 11.sp,
                    modifier = Modifier.padding(top = 6.dp).fillMaxWidth().background(Color.Black.copy(alpha = 0.35f)).padding(8.dp),
                )
            }
        }
    }
}

/** The "agent can see you" indicator the web UI also shows: always visible while sharing, with a live preview. */
@Composable
private fun SharingChip(onFrame: (ByteArray, Int, Int) -> Boolean, onStop: () -> Unit, lastFrameAtMs: Long) {
    val context = LocalContext.current
    val owner = LocalLifecycleOwner.current
    val executor = remember { Executors.newSingleThreadExecutor() }
    val controller = remember {
        LifecycleCameraController(context).apply {
            cameraSelector = CameraSelector.DEFAULT_BACK_CAMERA
            setEnabledUseCases(CameraController.IMAGE_ANALYSIS)
            imageAnalysisBackpressureStrategy = ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST
        }
    }
    LaunchedEffect(controller) {
        controller.setImageAnalysisAnalyzer(executor, FrameSampler { jpeg, w, h -> onFrame(jpeg, w, h) })
        controller.bindToLifecycle(owner)
    }
    androidx.compose.runtime.DisposableEffect(controller) {
        onDispose {
            controller.unbind()
            executor.shutdown()
        }
    }
    Row(
        Modifier.fillMaxWidth().background(Palette.Emerald.copy(alpha = 0.12f)).padding(horizontal = 12.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        AndroidView(
            factory = { ctx -> PreviewView(ctx).also { it.controller = controller } },
            modifier = Modifier.size(width = 64.dp, height = 40.dp).clip(RoundedCornerShape(6.dp)),
        )
        Text(
            "  Sharing your camera — the agent only looks when you ask about it" + if (lastFrameAtMs > 0) "" else " (starting…)",
            fontSize = 11.sp, color = Palette.Emerald, modifier = Modifier.weight(1f), maxLines = 2,
        )
        TextButton(onClick = onStop) { Text("Stop", color = Palette.Emerald) }
    }
}
