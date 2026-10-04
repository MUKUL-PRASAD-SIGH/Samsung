package com.samsung.interruptible.state

import com.samsung.interruptible.data.AgentAction
import com.samsung.interruptible.data.ConnState

enum class Role { USER, AGENT, FILLER, SYSTEM }

data class ChatMessage(
    val id: Long,
    val role: Role,
    val text: String,
    /** Voice bubbles are keyed by the server's utterance id so partial transcripts refine one bubble. */
    val utteranceId: String? = null,
    val pendingVoice: Boolean = false,
    val isQuestion: Boolean = false,
    /** Set when the user cut the spoken reply off: the words they actually heard (may be empty). */
    val heard: String? = null,
)

data class TraceItem(val id: Long, val atMs: Long, val type: String, val text: String)

data class AgentCard(
    val callId: String,
    val name: String,
    val role: String,
    val status: String,           // working | completed | cancelled
    val thought: String,
    val step: Int,
    val totalSteps: Int,
    val cancelReason: String? = null,
)

data class ExportItem(val id: Long, val filename: String, val path: String, val bytes: Int, val downloadPath: String, val openedWith: String?)

data class ChatState(
    val connection: ConnState = ConnState.Disconnected,
    val messages: List<ChatMessage> = emptyList(),
    // --- the agent's own view of the session (state_snapshot)
    val epoch: Int = 1,
    val intent: String? = null,
    val slots: Map<String, String> = emptyMap(),
    val inFlight: List<AgentAction.InFlightCall> = emptyList(),
    // --- diagnostics
    val trace: List<TraceItem> = emptyList(),
    val agents: List<AgentCard> = emptyList(),
    val artifact: AgentAction.Artifact? = null,
    val artifactAuthor: String? = null,
    val artifactLoading: Boolean = false,
    val exports: List<ExportItem> = emptyList(),
    val graphNodes: List<AgentAction.GraphNode> = emptyList(),
    val graphEdges: List<AgentAction.GraphEdge> = emptyList(),
    // --- voice and camera
    val handsFree: Boolean = false,
    val userSpeaking: Boolean = false,
    val livePartial: String = "",
    val agentSpeaking: Boolean = false,
    val ducked: Boolean = false,
    val ttsAvailable: Boolean = false,
    val speakReplies: Boolean = false,
    val sharing: Boolean = false,
    val lastFrameAtMs: Long = 0,
    val error: String? = null,
    val nextId: Long = 1,
)
