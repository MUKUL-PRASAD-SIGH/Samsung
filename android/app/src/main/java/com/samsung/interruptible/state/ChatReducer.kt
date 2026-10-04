package com.samsung.interruptible.state

import com.samsung.interruptible.data.AgentAction
import com.samsung.interruptible.data.AgentAction.AgentStep
import com.samsung.interruptible.data.AgentAction.AudioOut
import com.samsung.interruptible.data.AgentAction.Clarification
import com.samsung.interruptible.data.AgentAction.Filler
import com.samsung.interruptible.data.AgentAction.GraphUpdate
import com.samsung.interruptible.data.AgentAction.SpeechState
import com.samsung.interruptible.data.AgentAction.SpokenResponse
import com.samsung.interruptible.data.AgentAction.StateSnapshot
import com.samsung.interruptible.data.AgentAction.ToolCall
import com.samsung.interruptible.data.AgentAction.ToolCancel
import com.samsung.interruptible.data.AgentAction.Transcript
import com.samsung.interruptible.data.AgentAction.VoiceActivity
import com.samsung.interruptible.data.ConnState
import com.samsung.interruptible.data.ServerMessage

/**
 * The whole UI state machine in one pure function: (state, server message) -> state. It mirrors what the web client does
 * with the same actions, and being pure it is unit-tested without a device.
 */
object ChatReducer {
    private const val MAX_TRACE = 400

    fun reduce(s: ChatState, msg: ServerMessage, nowMs: Long): ChatState = when (msg) {
        is ServerMessage.Error -> trace(
            s.copy(error = describeError(msg.code)), nowMs, "system", "Server refused a message: ${msg.code}",
        )
        is ServerMessage.TtsStatus -> s.copy(ttsAvailable = msg.available, speakReplies = msg.enabled && msg.available)
        is ServerMessage.Action -> reduceAction(s.copy(epoch = maxOf(s.epoch, msg.action.epoch)), msg.action, nowMs)
    }

    fun connectionChanged(s: ChatState, conn: ConnState, nowMs: Long): ChatState {
        if (conn == s.connection) return s
        val label = when (conn) {
            ConnState.Connected -> "Connected"
            ConnState.Connecting -> "Connecting..."
            ConnState.Disconnected -> "Disconnected"
            is ConnState.Refused -> "Refused: ${conn.reason}"
            is ConnState.Retrying -> "Connection lost (${conn.reason}); retrying in ${conn.inMs} ms"
        }
        // The agent's view is per connection: speech state is stale once the socket drops.
        val dropped = conn != ConnState.Connected && conn != ConnState.Connecting
        val cleared = if (dropped) s.copy(userSpeaking = false, livePartial = "", agentSpeaking = false, ducked = false) else s
        return trace(
            cleared.copy(connection = conn, error = if (conn == ConnState.Connected) null else cleared.error),
            nowMs, "system", label,
        )
    }

    fun userSent(s: ChatState, text: String): ChatState = addMessage(s, Role.USER, text)

    fun setHandsFree(s: ChatState, on: Boolean): ChatState =
        s.copy(handsFree = on, userSpeaking = if (on) s.userSpeaking else false, livePartial = if (on) s.livePartial else "")

    fun setSpeakReplies(s: ChatState, on: Boolean): ChatState = s.copy(speakReplies = on, agentSpeaking = s.agentSpeaking && on)

    fun setSharing(s: ChatState, on: Boolean, nowMs: Long): ChatState = trace(
        s.copy(sharing = on), nowMs, "vision",
        if (on) "Sharing camera (1 frame/s, analysed only when you ask about it)" else "Stopped sharing camera",
    )

    fun frameSent(s: ChatState, nowMs: Long): ChatState = s.copy(lastFrameAtMs = nowMs)

    fun dismissError(s: ChatState): ChatState = s.copy(error = null)

    // ------------------------------------------------------------------------------------------------ actions
    private fun reduceAction(s: ChatState, a: AgentAction, now: Long): ChatState = when (a) {
        is Filler -> trace(addMessage(s, Role.FILLER, a.text), now, "filler", a.text)
        is SpokenResponse -> trace(addMessage(s, Role.AGENT, a.text), now, "response", a.text)
        is Clarification -> trace(addMessage(s, Role.AGENT, a.question, isQuestion = true), now, "response", a.question)
        is ToolCall -> onToolCall(s, a, now)
        is ToolCancel -> onToolCancel(s, a, now)
        is StateSnapshot -> trace(
            s.copy(epoch = a.epoch, intent = a.intent, slots = a.slots, inFlight = a.inFlight), now, "snapshot",
            "epoch ${a.epoch} · ${a.intent ?: "no intent"} · ${a.inFlight.size} in flight",
        )
        is AgentStep -> onAgentStep(s, a, now)
        is GraphUpdate -> onGraph(s, a, now)
        is Transcript -> onTranscript(s, a, now)
        is VoiceActivity -> onVoiceActivity(s, a, now)
        is AudioOut -> s.copy(agentSpeaking = true)
        is SpeechState -> onSpeechState(s, a, now)
        is AgentAction.FileExported -> onExport(s, a, now)
        is AgentAction.Unknown -> s
    }

    private fun onExport(s: ChatState, a: AgentAction.FileExported, now: Long): ChatState {
        val item = ExportItem(s.nextId, a.filename, a.path, a.bytes, a.downloadPath, a.openedWith)
        // Show what was written as the workspace artifact, even when the model wrote the code inside the export call itself.
        val withArtifact = if (a.preview.isNotEmpty())
            s.copy(artifact = AgentAction.Artifact(a.filename, a.language, a.preview), artifactAuthor = "Kairos", artifactLoading = false) else s
        return trace(withArtifact.copy(exports = withArtifact.exports + item, nextId = withArtifact.nextId + 1), now, "agent_step",
            "Exported ${a.filename} (${a.bytes} bytes)")
    }

    private fun onToolCall(s: ChatState, a: ToolCall, now: Long): ChatState {
        val args = a.arguments.entries.joinToString(", ") { "${it.key}=${it.value}" }
        val t = trace(s, now, "tool_call", "${a.toolName}($args)" + if (a.isStateModifying) "  [state-changing]" else "")
        if (a.toolName != "spawn_agent") return t
        val name = a.arguments["name"] ?: "bob"
        val card = AgentCard(a.callId, name, a.arguments["role"] ?: "Worker", "working", "Starting...", 0, 0)
        return t.copy(agents = t.agents.filter { it.callId != a.callId } + card, artifactLoading = true)
    }

    private fun onToolCancel(s: ChatState, a: ToolCancel, now: Long): ChatState {
        val agents = s.agents.map {
            if (it.callId == a.callId) it.copy(status = "cancelled", thought = "Cancelled by your input (epoch ${a.epoch})", cancelReason = a.reason) else it
        }
        // A cancelled worker can no longer produce the artifact: don't leave the workspace spinning forever.
        return trace(
            s.copy(agents = agents, artifactLoading = if (s.artifact == null) false else s.artifactLoading),
            now, "tool_cancel", "Cancelled ${a.toolName} (${a.reason})",
        )
    }

    private fun onAgentStep(s: ChatState, a: AgentStep, now: Long): ChatState {
        val existing = s.agents.firstOrNull { it.callId == a.callId }
        val card = AgentCard(
            a.callId, a.name.ifBlank { existing?.name ?: "bob" }, a.role.ifBlank { existing?.role ?: "Worker" },
            a.status, a.thought, a.step, a.totalSteps, existing?.cancelReason,
        )
        val agents = if (existing != null) s.agents.map { if (it.callId == a.callId) card else it } else s.agents + card
        val withArtifact = if (a.artifact != null) s.copy(artifact = a.artifact, artifactAuthor = card.name, artifactLoading = false) else s
        return trace(withArtifact.copy(agents = agents), now, "agent_step", "[${card.name}] step ${a.step}/${a.totalSteps}: ${a.thought}")
    }

    private fun onGraph(s: ChatState, a: GraphUpdate, now: Long): ChatState {
        val nodes = if (a.op == "full") a.nodes else s.graphNodes + a.nodes.filter { n -> s.graphNodes.none { it.id == n.id } }
        val edges = if (a.op == "full") a.edges else s.graphEdges + a.edges
        return trace(s.copy(graphNodes = nodes, graphEdges = edges), now, "graph_update", "Graph: +${a.nodes.size} node(s), +${a.edges.size} edge(s)")
    }

    private fun onTranscript(s: ChatState, a: Transcript, now: Long): ChatState {
        val heard = a.text.trim()
        val uid = a.utteranceId
        if (uid == null) {
            // Push-to-talk style transcript with no streaming bubble: show what was heard as the user's message.
            return if (heard.isEmpty()) s else addMessage(s, Role.USER, heard)
        }
        val idx = s.messages.indexOfFirst { it.utteranceId == uid }
        val live = if (a.isPartial) s.copy(livePartial = heard) else s.copy(livePartial = "")
        if (idx == -1) return live
        val msgs = live.messages.toMutableList()
        when {
            a.isPartial -> msgs[idx] = msgs[idx].copy(text = "$heard…")
            heard.isNotEmpty() -> msgs[idx] = msgs[idx].copy(text = heard, pendingVoice = false)
            else -> msgs.removeAt(idx)                       // the VAD fired on noise: drop the empty bubble
        }
        val out = live.copy(messages = msgs)
        if (a.isPartial) return out
        val note = if (heard.isNotEmpty()) "Heard (${a.asrModel ?: "asr"}, ${a.latencyMs?.toInt() ?: "?"} ms): \"$heard\"" else "Voice: noise, no speech recognised"
        return trace(out, now, "audio", note)
    }

    private fun onVoiceActivity(s: ChatState, a: VoiceActivity, now: Long): ChatState = when (a.state) {
        "speech_start" -> {
            val withBubble = if (a.utteranceId != null && s.messages.none { it.utteranceId == a.utteranceId }) {
                addMessage(s, Role.USER, "…", utteranceId = a.utteranceId, pendingVoice = true)
            } else s
            withBubble.copy(userSpeaking = true, livePartial = "")
        }
        "speech_end" -> s.copy(userSpeaking = false)
        "barge_in" -> trace(s, now, "tool_cancel", "Voice barge-in interrupted running work: \"${a.detail ?: ""}\"")
        "idle" -> s.copy(userSpeaking = false, livePartial = "")
        else -> s
    }

    private fun onSpeechState(s: ChatState, a: SpeechState, now: Long): ChatState = when (a.state) {
        "started" -> s.copy(agentSpeaking = true)
        "finished" -> s.copy(agentSpeaking = false, ducked = false)
        "ducked" -> s.copy(ducked = true)
        "resumed" -> s.copy(ducked = false)
        "stopped" -> {
            // Remember what the user actually heard: the bubble shows the whole reply, so mark where it was cut off.
            val idx = s.messages.indexOfLast { it.text == a.text && it.role != Role.USER && it.heard == null }
            val msgs = if (idx >= 0) s.messages.toMutableList().also { it[idx] = it[idx].copy(heard = a.spokenText.orEmpty()) } else s.messages
            trace(
                s.copy(agentSpeaking = false, ducked = false, messages = msgs), now, "tool_cancel",
                "Stopped speaking (${a.reason ?: "interrupted"}) after \"${a.spokenText.orEmpty().take(60)}\"",
            )
        }
        else -> s
    }

    // ------------------------------------------------------------------------------------------------ helpers
    private fun addMessage(
        s: ChatState, role: Role, text: String, utteranceId: String? = null, pendingVoice: Boolean = false, isQuestion: Boolean = false,
    ): ChatState = s.copy(
        messages = s.messages + ChatMessage(s.nextId, role, text, utteranceId, pendingVoice, isQuestion),
        nextId = s.nextId + 1,
    )

    private fun trace(s: ChatState, atMs: Long, type: String, text: String): ChatState {
        val items = s.trace + TraceItem(s.nextId, atMs, type, text)
        return s.copy(trace = if (items.size > MAX_TRACE) items.takeLast(MAX_TRACE) else items, nextId = s.nextId + 1)
    }

    fun describeError(code: String): String = when (code) {
        "rate_limit" -> "You are sending too fast; the server dropped a message."
        "too_large" -> "That message was too large for the server."
        "bad_json" -> "The server could not read a message."
        else -> "The server reported: $code"
    }
}
