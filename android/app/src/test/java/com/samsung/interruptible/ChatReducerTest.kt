package com.samsung.interruptible

import com.samsung.interruptible.data.AgentAction
import com.samsung.interruptible.data.AgentAction.Artifact
import com.samsung.interruptible.data.ConnState
import com.samsung.interruptible.data.ServerMessage
import com.samsung.interruptible.state.ChatReducer
import com.samsung.interruptible.state.ChatState
import com.samsung.interruptible.state.Role
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ChatReducerTest {
    private var now = 1_000L
    private fun act(a: AgentAction) = ServerMessage.Action(a)
    private fun ChatState.then(a: AgentAction) = ChatReducer.reduce(this, act(a), now++)

    private fun filler(t: String, e: Int = 1) = AgentAction.Filler(e, 0.0, t)
    private fun reply(t: String, e: Int = 1) = AgentAction.SpokenResponse(e, 0.0, t, true)
    private fun transcript(t: String, partial: Boolean, uid: String? = "utt_1") =
        AgentAction.Transcript(1, 0.0, t, "base.en", 250.0, partial, uid)
    private fun voice(state: String, uid: String? = "utt_1", detail: String? = null) = AgentAction.VoiceActivity(1, 0.0, state, uid, detail)
    private fun speech(state: String, text: String = "", spoken: String? = null, reason: String? = null) =
        AgentAction.SpeechState(1, 0.0, state, "tts_1", reason, text, spoken, null)

    @Test fun fillerAndReplyBecomeBubblesInOrderWithTheirOwnRoles() {
        val s = ChatState().then(filler("Looking that up...")).then(reply("It's sunny."))
        assertEquals(listOf(Role.FILLER, Role.AGENT), s.messages.map { it.role })
        assertEquals(listOf("Looking that up...", "It's sunny."), s.messages.map { it.text })
        assertEquals(setOf(1L, 3L), s.messages.map { it.id }.toSet())          // unique ids (the trace shares the counter)
    }

    @Test fun aClarificationIsMarkedAsAQuestion() {
        val s = ChatState().then(AgentAction.Clarification(1, 0.0, "Which city?"))
        assertTrue(s.messages.single().isQuestion)
    }

    @Test fun typedMessageAppearsImmediately() {
        val s = ChatReducer.userSent(ChatState(), "hello")
        assertEquals(Role.USER to "hello", s.messages.single().let { it.role to it.text })
    }

    @Test fun snapshotReplacesTheAgentsViewAndEpochOnlyGoesUp() {
        var s = ChatState().then(AgentAction.StateSnapshot(3, 0.0, "book_flight", mapOf("destination" to "Goa"),
            listOf(AgentAction.InFlightCall("c1", "book_flight", 3, "running"))))
        assertEquals(3, s.epoch)
        assertEquals("book_flight", s.intent)
        assertEquals(1, s.inFlight.size)
        s = s.then(filler("late action from epoch 1", e = 1))
        assertEquals("a stale action must not move the epoch backwards", 3, s.epoch)
        s = s.then(AgentAction.StateSnapshot(4, 0.0, null, emptyMap(), emptyList()))
        assertEquals(4, s.epoch)
        assertTrue(s.slots.isEmpty() && s.inFlight.isEmpty() && s.intent == null)
    }

    // ------------------------------------------------------------------------------------------ voice
    @Test fun streamingVoiceBubbleIsRefinedByPartialsAndSettledByTheFinal() {
        var s = ChatState().then(voice("speech_start"))
        assertTrue(s.userSpeaking)
        assertTrue(s.messages.single().pendingVoice)
        s = s.then(transcript("no wait", partial = true))
        assertEquals("no wait…", s.messages.single().text)
        assertEquals("no wait", s.livePartial)
        s = s.then(transcript("no wait make it Goa", partial = true)).then(voice("speech_end"))
        assertFalse(s.userSpeaking)
        s = s.then(transcript("No wait, make it Goa.", partial = false))
        val bubble = s.messages.single()
        assertEquals("No wait, make it Goa.", bubble.text)
        assertFalse(bubble.pendingVoice)
        assertEquals("", s.livePartial)
        assertEquals("audio", s.trace.last().type)
    }

    @Test fun anEmptyFinalTranscriptDropsTheNoiseBubble() {
        val s = ChatState().then(voice("speech_start")).then(voice("speech_end")).then(transcript("", partial = false))
        assertTrue(s.messages.isEmpty())
        assertTrue(s.trace.last().text.contains("noise"))
    }

    @Test fun aSecondSpeechStartForTheSameUtteranceDoesNotDuplicateTheBubble() {
        val s = ChatState().then(voice("speech_start")).then(voice("speech_start"))
        assertEquals(1, s.messages.size)
    }

    @Test fun aTranscriptWithoutAStreamingBubbleBecomesAUserMessage() {
        val s = ChatState().then(transcript("book a flight", partial = false, uid = null))
        assertEquals(Role.USER to "book a flight", s.messages.single().let { it.role to it.text })
        assertTrue(ChatState().then(transcript("  ", partial = false, uid = null)).messages.isEmpty())
    }

    @Test fun bargeInIsTraced() {
        val s = ChatState().then(voice("barge_in", detail = "No wait"))
        assertEquals("tool_cancel", s.trace.last().type)
        assertTrue(s.trace.last().text.contains("No wait"))
    }

    // ------------------------------------------------------------------------------- the agent's voice
    @Test fun speechLifecycleDrivesTheSpeakingAndDuckedFlags() {
        var s = ChatState().then(speech("started"))
        assertTrue(s.agentSpeaking)
        s = s.then(speech("ducked"))
        assertTrue(s.ducked)
        s = s.then(speech("resumed"))
        assertFalse(s.ducked)
        s = s.then(speech("ducked")).then(speech("finished"))
        assertFalse(s.agentSpeaking || s.ducked)
    }

    @Test fun aCutOffReplyRemembersWhatWasActuallyHeard() {
        val full = "I found two flights. The cheapest is IndiGo."
        var s = ChatState().then(reply(full)).then(speech("started"))
        s = s.then(speech("stopped", text = full, spoken = "I found two", reason = "user_spoke"))
        assertFalse(s.agentSpeaking)
        assertEquals("the bubble keeps the full text", full, s.messages.single().text)
        assertEquals("I found two", s.messages.single().heard)
        assertTrue(s.trace.last().text.contains("user_spoke") && s.trace.last().text.contains("I found two"))
    }

    @Test fun onlyTheMatchingReplyIsMarkedNotAnEarlierIdenticalOneOrTheUsersOwnText() {
        var s = ChatReducer.userSent(ChatState(), "Hello there")
        s = s.then(reply("Hello there")).then(reply("Another")).then(reply("Hello there"))
        s = s.then(speech("stopped", text = "Hello there", spoken = "Hello"))
        val marked = s.messages.filter { it.heard != null }
        assertEquals(1, marked.size)
        assertEquals(s.messages.last().id, marked.single().id)             // the latest agent reply with that text
        assertNull(s.messages.first().heard)                              // the user's own message is never touched
    }

    @Test fun audioOutMarksTheAgentAsSpeaking() {
        val s = ChatState().then(AgentAction.AudioOut(1, 0.0, "tts_1", 0, "Hi.", 22050, 500.0, "AAAA", true))
        assertTrue(s.agentSpeaking)
    }

    // --------------------------------------------------------------------------------------- tools/agents
    @Test fun toolCallsAndCancellationsAreTraced() {
        val s = ChatState()
            .then(AgentAction.ToolCall(1, 0.0, "c1", "book_flight", mapOf("destination" to "Mumbai"), true))
            .then(AgentAction.ToolCancel(2, 0.0, "c1", "book_flight", "user_correction"))
        assertEquals(listOf("tool_call", "tool_cancel"), s.trace.map { it.type })
        assertTrue(s.trace[0].text.contains("book_flight(destination=Mumbai)") && s.trace[0].text.contains("state-changing"))
    }

    @Test fun spawnedAgentLifecycleAndArtifact() {
        var s = ChatState().then(AgentAction.ToolCall(1, 0.0, "w1", "spawn_agent", mapOf("name" to "vector_craft", "role" to "SVG Artist"), false))
        assertTrue(s.artifactLoading)
        assertEquals("vector_craft", s.agents.single().name)
        s = s.then(AgentAction.AgentStep(1, 0.0, "w1", "vector_craft", "SVG Artist", 2, 3, "Drawing", "working", null))
        assertEquals(2, s.agents.single().step)
        assertTrue(s.artifactLoading)
        val art = Artifact("logo.svg", "svg", "<svg/>")
        s = s.then(AgentAction.AgentStep(1, 0.0, "w1", "vector_craft", "SVG Artist", 3, 3, "Done", "completed", art))
        assertEquals(art, s.artifact)
        assertEquals("vector_craft", s.artifactAuthor)
        assertFalse(s.artifactLoading)
        assertEquals(1, s.agents.size)
    }

    @Test fun cancellingTheWorkerStopsTheArtifactSpinnerButKeepsAnExistingArtifact() {
        val spawn = AgentAction.ToolCall(1, 0.0, "w1", "spawn_agent", mapOf("name" to "bob"), false)
        var s = ChatState().then(spawn).then(AgentAction.ToolCancel(2, 0.0, "w1", "spawn_agent", "epoch_stale"))
        assertFalse(s.artifactLoading)
        assertEquals("cancelled", s.agents.single().status)
        assertEquals("epoch_stale", s.agents.single().cancelReason)
        val art = Artifact("a", "kt", "x")
        s = ChatState(artifact = art, artifactLoading = true).then(AgentAction.ToolCancel(2, 0.0, "other", "t", "r"))
        assertEquals(art, s.artifact)
        assertTrue("a cancelled unrelated call must not stop a build that still has no result", s.artifactLoading)
    }

    // ------------------------------------------------------------------------------------------- graph
    @Test fun graphAppendDedupesNodesAndFullReplaces() {
        fun node(id: String) = AgentAction.GraphNode(id, "turn", id)
        var s = ChatState().then(AgentAction.GraphUpdate(1, 0.0, "append", listOf(node("a"), node("b")), listOf(AgentAction.GraphEdge("a", "b", "NEXT_TURN"))))
        s = s.then(AgentAction.GraphUpdate(1, 0.0, "append", listOf(node("b"), node("c")), listOf(AgentAction.GraphEdge("b", "c", "NEXT_TURN"))))
        assertEquals(listOf("a", "b", "c"), s.graphNodes.map { it.id })
        assertEquals(2, s.graphEdges.size)
        s = s.then(AgentAction.GraphUpdate(2, 0.0, "full", listOf(node("z")), emptyList()))
        assertEquals(listOf("z"), s.graphNodes.map { it.id })
        assertTrue(s.graphEdges.isEmpty())
    }

    @Test fun anExportShowsWhatWasWrittenAndListsTheFile() {
        val s = ChatState().then(AgentAction.FileExported(2, 0.0, "reverse.py", "/h/reverse.py", 52, "python", "vscode://x", "/exports/reverse.py", "code",
            "def reverse(s):\n    return s[::-1]\n"))
        assertEquals(listOf("reverse.py"), s.exports.map { it.filename })
        assertEquals("/exports/reverse.py", s.exports.single().downloadPath)
        assertEquals(AgentAction.Artifact("reverse.py", "python", "def reverse(s):\n    return s[::-1]\n"), s.artifact)
        assertEquals("Kairos", s.artifactAuthor)
        assertTrue(s.trace.last().text.contains("reverse.py"))
        val again = s.then(AgentAction.FileExported(2, 0.0, "b.py", "/h/b.py", 1, "python", "", "/exports/b.py", null, ""))
        assertEquals(2, again.exports.size)
        assertEquals("an export without a preview leaves the artifact alone", "reverse.py", again.artifact!!.title)
    }

    // ------------------------------------------------------------------------ connection and server errors
    @Test fun connectionChangesAreTracedAndClearTransientVoiceState() {
        var s = ChatState(userSpeaking = true, livePartial = "no w", agentSpeaking = true, ducked = true)
        s = ChatReducer.connectionChanged(s, ConnState.Connected, now++)
        assertTrue("connecting must not wipe a live utterance", s.userSpeaking)
        s = ChatReducer.connectionChanged(s, ConnState.Retrying(500, "timeout"), now++)
        assertFalse(s.userSpeaking || s.agentSpeaking || s.ducked)
        assertEquals("", s.livePartial)
        assertTrue(s.trace.last().text.contains("retrying"))
        assertEquals(s, ChatReducer.connectionChanged(s, s.connection, now++))   // no change, no churn
    }

    @Test fun reconnectingClearsTheErrorBanner() {
        var s = ChatReducer.reduce(ChatState(), ServerMessage.Error("rate_limit"), now++)
        assertEquals(ChatReducer.describeError("rate_limit"), s.error)
        s = ChatReducer.connectionChanged(s.copy(connection = ConnState.Disconnected), ConnState.Connected, now++)
        assertNull(s.error)
        assertNull(ChatReducer.dismissError(ChatReducer.reduce(ChatState(), ServerMessage.Error("too_large"), now++)).error)
    }

    @Test fun ttsStatusDecidesWhetherTheSpeakerButtonExists() {
        var s = ChatReducer.reduce(ChatState(), ServerMessage.TtsStatus(enabled = true, available = true), now++)
        assertTrue(s.ttsAvailable && s.speakReplies)
        s = ChatReducer.reduce(s, ServerMessage.TtsStatus(enabled = false, available = false), now++)
        assertFalse(s.ttsAvailable || s.speakReplies)
        assertFalse("enabled but unavailable can never mean speaking",
            ChatReducer.reduce(ChatState(), ServerMessage.TtsStatus(enabled = true, available = false), now++).speakReplies)
    }

    @Test fun traceIsBounded() {
        var s = ChatState()
        repeat(900) { s = s.then(filler("f$it")) }
        assertEquals(400, s.trace.size)
        assertEquals("f899", s.trace.last().text)
        assertEquals(900, s.messages.size)                       // the conversation itself is never truncated
    }

    @Test fun sharingIsTraced() {
        val s = ChatReducer.setSharing(ChatState(), true, now++)
        assertTrue(s.sharing)
        assertEquals("vision", s.trace.last().type)
        assertFalse(ChatReducer.setSharing(s, false, now++).sharing)
    }

    @Test fun unknownActionsChangeNothingExceptTheEpochHighWaterMark() {
        val s = ChatState().then(AgentAction.Unknown(5, 0.0, "new"))
        assertEquals(5, s.epoch)
        assertTrue(s.messages.isEmpty() && s.trace.isEmpty())
    }
}
