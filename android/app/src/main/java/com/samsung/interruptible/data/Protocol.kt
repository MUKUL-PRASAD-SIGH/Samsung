package com.samsung.interruptible.data

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put

/**
 * The wire protocol of the interruptible-agent server (`/ws/{session_id}`), as seen by a client.
 *
 * Server -> client: every message is a JSON object. Agent actions carry `action_type`; a few control messages carry
 * `type` instead (`error`, `tts_status`). Parsing is deliberately tolerant: unknown fields are ignored and an unknown
 * `action_type` becomes [AgentAction.Unknown], so a server that grows a new action never crashes an older app.
 */
sealed interface ServerMessage {
    data class Action(val action: AgentAction) : ServerMessage

    /** The server refused or dropped something (rate_limit, too_large, bad_json, ...). */
    data class Error(val code: String) : ServerMessage

    data class TtsStatus(val enabled: Boolean, val available: Boolean) : ServerMessage
}

sealed interface AgentAction {
    val epoch: Int
    val timestamp: Double

    data class Filler(override val epoch: Int, override val timestamp: Double, val text: String) : AgentAction

    data class SpokenResponse(override val epoch: Int, override val timestamp: Double, val text: String, val isFinal: Boolean) : AgentAction

    data class Clarification(override val epoch: Int, override val timestamp: Double, val question: String) : AgentAction

    data class ToolCall(
        override val epoch: Int, override val timestamp: Double,
        val callId: String, val toolName: String, val arguments: Map<String, String>, val isStateModifying: Boolean,
    ) : AgentAction

    data class ToolCancel(
        override val epoch: Int, override val timestamp: Double,
        val callId: String, val toolName: String, val reason: String,
    ) : AgentAction

    data class InFlightCall(val callId: String, val tool: String, val epoch: Int, val status: String)

    data class StateSnapshot(
        override val epoch: Int, override val timestamp: Double,
        val intent: String?, val slots: Map<String, String>, val inFlight: List<InFlightCall>,
    ) : AgentAction

    data class Artifact(val title: String, val language: String, val content: String)

    data class AgentStep(
        override val epoch: Int, override val timestamp: Double,
        val callId: String, val name: String, val role: String, val step: Int, val totalSteps: Int,
        val thought: String, val status: String, val artifact: Artifact?,
    ) : AgentAction

    data class GraphNode(val id: String, val nodeType: String, val label: String)

    data class GraphEdge(val source: String, val target: String, val edgeType: String)

    data class GraphUpdate(
        override val epoch: Int, override val timestamp: Double,
        val op: String, val nodes: List<GraphNode>, val edges: List<GraphEdge>,
    ) : AgentAction

    data class Transcript(
        override val epoch: Int, override val timestamp: Double,
        val text: String, val asrModel: String?, val latencyMs: Double?, val isPartial: Boolean, val utteranceId: String?,
    ) : AgentAction

    data class VoiceActivity(
        override val epoch: Int, override val timestamp: Double,
        val state: String, val utteranceId: String?, val detail: String?,
    ) : AgentAction

    /** One sentence of the agent's spoken reply: PCM16 mono, little-endian, base64. */
    data class AudioOut(
        override val epoch: Int, override val timestamp: Double,
        val utteranceId: String, val seq: Int, val text: String, val sampleRate: Int, val durationMs: Double,
        val audioB64: String, val isLast: Boolean,
    ) : AgentAction

    /** started | ducked | resumed | finished | stopped. A `stopped` reply carries the words that WERE spoken. */
    data class SpeechState(
        override val epoch: Int, override val timestamp: Double,
        val state: String, val utteranceId: String, val reason: String?, val text: String,
        val spokenText: String?, val spokenMs: Double?,
    ) : AgentAction

    /** The agent wrote a file for the user (export_artifact). */
    data class FileExported(
        override val epoch: Int, override val timestamp: Double,
        val filename: String, val path: String, val bytes: Int, val language: String,
        val editorUri: String, val downloadPath: String, val openedWith: String?, val preview: String,
    ) : AgentAction

    data class Unknown(override val epoch: Int, override val timestamp: Double, val actionType: String) : AgentAction
}

object Protocol {
    val json = Json { ignoreUnknownKeys = true; isLenient = true }

    /** Parses one text frame. Returns null for something that is not a JSON object (never throws). */
    fun parse(frame: String): ServerMessage? {
        val obj = try {
            json.parseToJsonElement(frame).jsonObject
        } catch (e: Exception) {
            return null
        }
        obj.str("action_type")?.let { return ServerMessage.Action(parseAction(it, obj)) }
        return when (obj.str("type")) {
            "error" -> ServerMessage.Error(obj.str("code") ?: "unknown")
            "tts_status" -> ServerMessage.TtsStatus(obj.bool("enabled"), obj.bool("available"))
            else -> null
        }
    }

    private fun parseAction(type: String, o: JsonObject): AgentAction {
        val epoch = o.int("epoch")
        val ts = o.dbl("timestamp") ?: 0.0
        return when (type) {
            "filler" -> AgentAction.Filler(epoch, ts, o.str("text").orEmpty())
            "spoken_response" -> AgentAction.SpokenResponse(epoch, ts, o.str("text").orEmpty(), o["is_final"]?.asBool() ?: true)
            "clarification" -> AgentAction.Clarification(epoch, ts, o.str("question").orEmpty())
            "tool_call" -> AgentAction.ToolCall(
                epoch, ts, o.str("call_id").orEmpty(), o.str("tool_name").orEmpty(),
                (o["arguments"] as? JsonObject)?.mapValues { it.value.render() } ?: emptyMap(), o.bool("is_state_modifying"),
            )
            "tool_cancel" -> AgentAction.ToolCancel(
                epoch, ts, o.str("call_id").orEmpty(), o.str("tool_name").orEmpty(), o.str("reason") ?: "epoch_stale",
            )
            "state_snapshot" -> AgentAction.StateSnapshot(
                epoch, ts, o.str("intent"),
                (o["slots"] as? JsonObject)?.mapValues { it.value.render() } ?: emptyMap(),
                (o["in_flight_calls"] as? JsonArray)?.mapNotNull { (it as? JsonObject)?.toInFlight() } ?: emptyList(),
            )
            "agent_step" -> AgentAction.AgentStep(
                epoch, ts, o.str("call_id").orEmpty(), o.str("name").orEmpty(), o.str("role").orEmpty(),
                o.int("step"), o.int("total_steps"), o.str("thought").orEmpty(), o.str("status") ?: "working",
                (o["artifact"] as? JsonObject)?.let {
                    AgentAction.Artifact(it.str("title") ?: "Artifact", it.str("language") ?: "text", it.str("content").orEmpty())
                },
            )
            "graph_update" -> AgentAction.GraphUpdate(
                epoch, ts, o.str("op") ?: "append",
                (o["nodes"] as? JsonArray)?.mapNotNull { (it as? JsonObject)?.toNode() } ?: emptyList(),
                (o["edges"] as? JsonArray)?.mapNotNull { (it as? JsonObject)?.toEdge() } ?: emptyList(),
            )
            "transcript" -> AgentAction.Transcript(
                epoch, ts, o.str("text").orEmpty(), o.str("asr_model"), o.dbl("latency_ms"),
                o.bool("is_partial"), o.str("utterance_id"),
            )
            "voice_activity" -> AgentAction.VoiceActivity(epoch, ts, o.str("state").orEmpty(), o.str("utterance_id"), o.str("detail"))
            "audio_out" -> AgentAction.AudioOut(
                epoch, ts, o.str("utterance_id").orEmpty(), o.int("seq"), o.str("text").orEmpty(),
                o.int("sample_rate"), o.dbl("duration_ms") ?: 0.0, o.str("audio_b64").orEmpty(), o.bool("is_last"),
            )
            "speech_state" -> AgentAction.SpeechState(
                epoch, ts, o.str("state").orEmpty(), o.str("utterance_id").orEmpty(), o.str("reason"),
                o.str("text").orEmpty(), o.str("spoken_text"), o.dbl("spoken_ms"),
            )
            "file_exported" -> AgentAction.FileExported(
                epoch, ts, o.str("filename").orEmpty(), o.str("path").orEmpty(), o.int("bytes"), o.str("language").orEmpty(),
                o.str("editor_uri").orEmpty(), o.str("download_path").orEmpty(), o.str("opened_with"), o.str("preview").orEmpty(),
            )
            else -> AgentAction.Unknown(epoch, ts, type)
        }
    }

    private fun JsonObject.toInFlight() = AgentAction.InFlightCall(
        str("call_id").orEmpty(), str("tool").orEmpty(), int("epoch"), str("status") ?: "pending",
    )

    private fun JsonObject.toNode() = AgentAction.GraphNode(str("id").orEmpty(), str("node_type").orEmpty(), str("label").orEmpty())

    private fun JsonObject.toEdge() = AgentAction.GraphEdge(str("source").orEmpty(), str("target").orEmpty(), str("edge_type").orEmpty())

    private fun JsonObject.str(key: String): String? = (this[key] as? JsonPrimitive)?.takeIf { it !is JsonNull }?.contentOrNull

    private fun JsonObject.int(key: String): Int = (this[key] as? JsonPrimitive)?.intOrNull ?: (this[key] as? JsonPrimitive)?.doubleOrNull?.toInt() ?: 0

    private fun JsonObject.dbl(key: String): Double? = (this[key] as? JsonPrimitive)?.doubleOrNull

    private fun JsonObject.bool(key: String): Boolean = this[key]?.asBool() ?: false

    private fun JsonElement.asBool(): Boolean? = (this as? JsonPrimitive)?.booleanOrNull

    /** Slot/argument values are shown to people, so numbers and strings print bare and nested values as compact JSON. */
    private fun JsonElement.render(): String = if (this is JsonPrimitive) (contentOrNull ?: "null") else toString()
}

/** Client -> server messages (text frames). */
object Outgoing {
    fun userText(text: String) = frame("user_text") { put("text", text) }

    fun interrupt(reason: String = "ui_barge_in") = frame("interrupt") { put("reason", reason) }

    fun tts(enabled: Boolean) = frame("tts") { put("enabled", enabled) }

    /** `action` is "start" or "stop"; between them binary frames are raw 16 kHz mono PCM16. */
    fun voiceStream(action: String) = frame("voice_stream") { put("action", action) }

    fun videoFrame(jpegBase64: String, source: String, width: Int, height: Int) = frame("video_frame") {
        put("mime", "image/jpeg")
        put("data", jpegBase64)
        put("source", source)
        put("width", width)
        put("height", height)
    }

    private inline fun frame(type: String, fill: kotlinx.serialization.json.JsonObjectBuilder.() -> Unit): String =
        buildJsonObject {
            put("type", type)
            fill()
        }.toString()
}
