package com.samsung.interruptible

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
import com.samsung.interruptible.data.Outgoing
import com.samsung.interruptible.data.Protocol
import com.samsung.interruptible.data.ServerMessage
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Fixtures come from the REAL server classes (scripts/dump_android_fixtures.py), so a schema change breaks this test. */
class ProtocolTest {
    private val messages: List<ServerMessage> = resource("server_messages.jsonl").lines().filter { it.isNotBlank() }.map {
        Protocol.parse(it) ?: error("fixture did not parse: $it")
    }

    private fun resource(name: String) = javaClass.classLoader!!.getResourceAsStream(name)!!.bufferedReader().readText()

    private inline fun <reified T : AgentAction> all(): List<T> =
        messages.filterIsInstance<ServerMessage.Action>().map { it.action }.filterIsInstance<T>()

    @Test fun everyServerMessageInTheFixtureParses() {
        assertEquals(17, messages.size)
        assertTrue("no fixture may degrade to Unknown", messages.none { it is ServerMessage.Action && it.action is AgentAction.Unknown })
    }

    @Test fun fillerAndSpokenResponseAndClarification() {
        assertEquals("Looking that up right now...", all<Filler>().single().text)
        val reply = all<SpokenResponse>().single()
        assertTrue(reply.text.startsWith("I found **2 flights**") && reply.isFinal)
        assertEquals("Do you want me to change what I'm working on?", all<Clarification>().single().question)
        assertEquals(2, all<Clarification>().single().epoch)
    }

    @Test fun toolCallKeepsArgumentsAsReadableText() {
        val call = all<ToolCall>().single()
        assertEquals("search_flights", call.toolName)
        assertEquals(mapOf("origin" to "Delhi", "destination" to "Mumbai", "nights" to "2"), call.arguments)   // 2 prints as 2, not 2.0
        assertEquals(false, call.isStateModifying)
    }

    @Test fun toolCancel() {
        val c = all<ToolCancel>().single()
        assertEquals(Triple("call_1", "search_flights", "user_correction"), Triple(c.callId, c.toolName, c.reason))
    }

    @Test fun stateSnapshotSlotsAndInFlight() {
        val s = all<StateSnapshot>().single()
        assertEquals("search_flights", s.intent)
        assertEquals(mapOf("origin" to "Delhi", "destination" to "Goa", "nights" to "3"), s.slots)
        assertEquals(listOf(AgentAction.InFlightCall("call_2", "search_flights", 2, "running")), s.inFlight)
    }

    @Test fun agentStepCarriesTheArtifact() {
        val a = all<AgentStep>().single()
        assertEquals("vector_craft", a.name)
        assertEquals(3, a.step)
        assertEquals(AgentAction.Artifact("logo.svg", "svg", "<svg/>"), a.artifact)
    }

    @Test fun graphUpdate() {
        val g = all<GraphUpdate>().single()
        assertEquals("append", g.op)
        assertEquals(listOf("turn_1" to "turn", "ent_1" to "entity"), g.nodes.map { it.id to it.nodeType })
        assertEquals(AgentAction.GraphEdge("turn_1", "ent_1", "REFERENCES"), g.edges.single())
    }

    @Test fun partialAndFinalTranscripts() {
        val (partial, final) = all<Transcript>()
        assertTrue(partial.isPartial && !final.isPartial)
        assertEquals("utt_1", partial.utteranceId)
        assertEquals(312.5, partial.latencyMs!!, 0.0)
        assertEquals("No wait, make it Goa.", final.text)
        assertEquals("base.en", final.asrModel)
    }

    @Test fun voiceActivity() {
        val (start, barge) = all<VoiceActivity>()
        assertEquals("speech_start", start.state)
        assertEquals("barge_in" to "No wait", barge.state to barge.detail)
    }

    @Test fun audioOutIsDecodableAudio() {
        val a = all<AudioOut>().single()
        assertEquals(22050, a.sampleRate)
        assertEquals(0, a.seq)
        assertTrue(a.isLast)
        val pcm = com.samsung.interruptible.audio.Pcm.decodeBase64(a.audioB64)
        assertEquals(160, pcm.size)                       // 40 samples of 4 bytes in the fixture
        assertEquals(1.toByte(), pcm[0])
    }

    @Test fun speechStateStoppedReportsWhatWasHeard() {
        val (ducked, stopped) = all<SpeechState>()
        assertEquals("ducked", ducked.state)
        assertEquals("user_spoke", stopped.reason)
        assertEquals("I found two", stopped.spokenText)
        assertEquals(900.0, stopped.spokenMs!!, 0.0)
        assertEquals("I found two flights. The cheapest is IndiGo.", stopped.text)
    }

    @Test fun controlMessages() {
        assertTrue(messages.contains(ServerMessage.TtsStatus(enabled = true, available = true)))
        assertTrue(messages.contains(ServerMessage.Error("rate_limit")))
    }

    @Test fun anUnknownActionTypeNeverCrashesAnOlderApp() {
        val m = Protocol.parse("""{"action_type":"brand_new_thing","epoch":4,"timestamp":1.0,"extra":{"x":1}}""")
        assertEquals(AgentAction.Unknown(4, 1.0, "brand_new_thing"), (m as ServerMessage.Action).action)
    }

    @Test fun garbageAndNonObjectsAreIgnoredNotThrown() {
        assertNull(Protocol.parse("not json"))
        assertNull(Protocol.parse("[1,2,3]"))
        assertNull(Protocol.parse(""))
        assertNull(Protocol.parse("""{"nothing":"useful"}"""))
        assertNull(Protocol.parse("""{"type":"mystery"}"""))
    }

    @Test fun missingFieldsFallBackToSafeDefaults() {
        val m = Protocol.parse("""{"action_type":"spoken_response"}""") as ServerMessage.Action
        assertEquals(SpokenResponse(0, 0.0, "", true), m.action)
        val s = (Protocol.parse("""{"action_type":"state_snapshot","epoch":3}""") as ServerMessage.Action).action as StateSnapshot
        assertTrue(s.slots.isEmpty() && s.inFlight.isEmpty() && s.intent == null)
    }

    @Test fun nullsAndNestedValuesRender() {
        val call = (Protocol.parse("""{"action_type":"tool_call","epoch":1,"call_id":"c","tool_name":"t","arguments":{"a":null,"b":{"x":1},"c":true}}""")
            as ServerMessage.Action).action as ToolCall
        assertEquals(mapOf("a" to "null", "b" to """{"x":1}""", "c" to "true"), call.arguments)
    }

    // ------------------------------------------------------------------------------------ what the app sends
    @Test fun outgoingFramesMatchTheContractTheServerIsTestedAgainst() {
        // tests/test_android_fixtures.py replays this same file against the real server, so both sides agree.
        val expected = Json.parseToJsonElement(resource("client_frames.json")).jsonArray.associate {
            val o = it.jsonObject
            o["name"]!!.let { n -> (n as kotlinx.serialization.json.JsonPrimitive).content } to o["frame"]!!.jsonObject
        }
        val built = mapOf(
            "user_text" to Outgoing.userText("Find flights from Delhi to Mumbai"),
            "interrupt" to Outgoing.interrupt(),
            "tts_on" to Outgoing.tts(true),
            "tts_off" to Outgoing.tts(false),
            "voice_start" to Outgoing.voiceStream("start"),
            "voice_stop" to Outgoing.voiceStream("stop"),
            "video_frame" to Outgoing.videoFrame("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBD", "camera", 640, 480),
        )
        assertEquals(expected.keys, built.keys)
        for ((name, frame) in built) {
            val actual: JsonObject = Json.parseToJsonElement(frame).jsonObject
            assertEquals("frame '$name'", expected[name], actual)
        }
        assertNotNull(built["user_text"])
    }

    @Test fun userTextEscapesQuotesAndUnicode() {
        val frame = Outgoing.userText("say \"hi\"\nand 日本語 🙂")
        val text = (Json.parseToJsonElement(frame).jsonObject["text"] as kotlinx.serialization.json.JsonPrimitive).content
        assertEquals("say \"hi\"\nand 日本語 🙂", text)
    }
}
