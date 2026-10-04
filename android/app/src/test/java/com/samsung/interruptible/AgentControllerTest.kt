package com.samsung.interruptible

import com.samsung.interruptible.audio.MicInput
import com.samsung.interruptible.audio.SpeechOutput
import com.samsung.interruptible.data.AgentAction
import com.samsung.interruptible.data.ConnState
import com.samsung.interruptible.data.Protocol
import com.samsung.interruptible.data.ServerMessage
import com.samsung.interruptible.data.Settings
import com.samsung.interruptible.data.SettingsStore
import com.samsung.interruptible.data.Transport
import com.samsung.interruptible.state.AgentController
import com.samsung.interruptible.state.Role
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class AgentControllerTest {
    private class Rig(opens: Boolean = true, settings: Settings = Settings(serverUrl = "ws://10.0.2.2:8000", sessionId = "sess_1")) {
        val scope = TestScope(UnconfinedTestDispatcher())
        val transport = FakeTransport()
        val speech = FakeSpeech()
        val mic = FakeMic(opens)
        val store = FakeStore(settings)
        val controller = AgentController(transport, speech, mic, store, scope.backgroundScope, nowMs = { 5L })
        init { controller.start() }
        fun receive(json: String) { transport.incomingFlow.tryEmit(Protocol.parse(json)!!) }
        val state get() = controller.state.value
    }

    private fun action(type: String, body: String) = """{"action_type":"$type","epoch":1,"timestamp":1.0,$body}"""

    @Test fun connectsToTheConfiguredServerWithTokenAndSession() = runTest {
        val r = Rig(settings = Settings(serverUrl = "http://10.0.2.2:8000/", token = "s3cret", sessionId = "sess_9"))
        r.controller.connect()
        assertEquals("ws://10.0.2.2:8000/ws/sess_9?token=s3cret", r.transport.connectedTo)
        assertEquals(ConnState.Connected, r.state.connection)
    }

    @Test fun sendingTextShowsItAndSendsTheFrameAndSilencesTheAgentsVoiceFirst() = runTest {
        val r = Rig()
        r.controller.connect()
        assertTrue(r.controller.sendText("  find flights  "))
        assertEquals(listOf("user_text"), r.transport.sentTypes())
        assertEquals("find flights", (Json.parseToJsonElement(r.transport.sentText.single()).jsonObject["text"] as JsonPrimitive).content)
        assertEquals(Role.USER, r.state.messages.single().role)
        assertEquals("typing over the agent must silence it without waiting for the server", listOf("flush"), r.speech.events)
    }

    @Test fun blankTextIsNeverSent() = runTest {
        val r = Rig()
        r.controller.connect()
        assertFalse(r.controller.sendText("   "))
        assertTrue(r.transport.sentText.isEmpty() && r.state.messages.isEmpty())
    }

    @Test fun whenTheSocketIsDownTheMessageIsNotFakedAsSent() = runTest {
        val r = Rig()
        r.controller.connect()
        r.transport.open = false
        assertFalse(r.controller.sendText("hello"))
        assertTrue("no phantom bubble for a message that never left", r.state.messages.isEmpty())
        assertTrue(r.state.error!!.contains("not sent"))
    }

    @Test fun theStopButtonSilencesLocallyAndTellsTheServer() = runTest {
        val r = Rig()
        r.controller.connect()
        r.controller.interrupt()
        assertEquals(listOf("flush"), r.speech.events)
        assertEquals(listOf("interrupt"), r.transport.sentTypes())
    }

    // ----------------------------------------------------------------------------- the agent's voice
    @Test fun spokenAudioIsPlayedOnlyWhenRepliesAreSwitchedOn() = runTest {
        val r = Rig()
        r.controller.connect()
        val audio = action("audio_out", """"utterance_id":"tts_1","seq":0,"text":"Hi.","sample_rate":22050,"duration_ms":10.0,"audio_b64":"AQD//w==","is_last":true""")
        r.receive(audio)
        assertTrue("replies are off: server audio must be ignored", r.speech.played.isEmpty())
        r.controller.setSpeakReplies(true)
        r.receive(audio)
        assertEquals(listOf(4 to 22050), r.speech.played)
        assertTrue(r.state.agentSpeaking)
    }

    @Test fun duckThenStopFlushesImmediately() = runTest {
        val r = Rig()
        r.controller.connect()
        r.receive(action("speech_state", """"state":"ducked","utterance_id":"tts_1","reason":"user_speech_start","text":"-","spoken_text":null,"spoken_ms":null"""))
        r.receive(action("speech_state", """"state":"stopped","utterance_id":"tts_1","reason":"user_spoke","text":"Full reply.","spoken_text":"Full","spoken_ms":300.0"""))
        assertEquals(listOf("duck=true", "flush"), r.speech.events)
        r.receive(action("speech_state", """"state":"resumed","utterance_id":"tts_2","text":"""""))
        assertEquals("duck=false", r.speech.events.last())
    }

    @Test fun finishingRestoresFullVolume() = runTest {
        val r = Rig()
        r.controller.connect()
        r.receive(action("speech_state", """"state":"ducked","utterance_id":"t","text":"-""""))
        r.receive(action("speech_state", """"state":"finished","utterance_id":"t","text":"x","spoken_text":"x","spoken_ms":1.0"""))
        assertEquals(listOf("duck=true", "duck=false"), r.speech.events)
    }

    @Test fun turningRepliesOffGoesQuietAndTellsTheServer() = runTest {
        val r = Rig()
        r.controller.connect()
        r.controller.setSpeakReplies(true)
        r.controller.setSpeakReplies(false)
        assertEquals(listOf("tts", "tts"), r.transport.sentTypes())
        assertEquals(listOf("flush"), r.speech.events)
        assertFalse(r.store.settings.speakReplies)
    }

    // ----------------------------------------------------------------------------- hands-free voice
    @Test fun handsFreeStreamsMicFramesAndAnnouncesStartAndStop() = runTest {
        val r = Rig()
        r.controller.connect()
        assertTrue(r.controller.setHandsFree(true))
        assertTrue(r.state.handsFree && r.mic.running)
        assertEquals(listOf("voice_stream"), r.transport.sentTypes())
        val frame = ByteArray(3200) { it.toByte() }
        r.mic.onFrame!!(frame)
        assertEquals(1, r.transport.sentBinary.size)
        assertTrue(r.transport.sentBinary.single().contentEquals(frame))
        assertTrue(r.controller.setHandsFree(false))
        assertFalse(r.mic.running || r.state.handsFree)
        assertEquals(listOf("voice_stream", "voice_stream"), r.transport.sentTypes())
        assertEquals("stop", (Json.parseToJsonElement(r.transport.sentText.last()).jsonObject["action"] as JsonPrimitive).content)
    }

    @Test fun anUnavailableMicrophoneIsReportedAndNothingIsAnnounced() = runTest {
        val r = Rig(opens = false)
        r.controller.connect()
        assertFalse(r.controller.setHandsFree(true))
        assertFalse(r.state.handsFree)
        assertTrue(r.transport.sentText.isEmpty())
        assertTrue(r.state.error!!.contains("microphone"))
    }

    @Test fun askingForHandsFreeTwiceDoesNotOpenTheMicTwice() = runTest {
        val r = Rig()
        r.controller.connect()
        r.controller.setHandsFree(true)
        r.controller.setHandsFree(true)
        assertEquals(1, r.transport.sentTypes().count { it == "voice_stream" })
    }

    @Test fun reconnectingReAnnouncesWhatTheClientWants() = runTest {
        val r = Rig()
        r.controller.connect()
        r.controller.setSpeakReplies(true)
        r.controller.setHandsFree(true)
        r.transport.sentText.clear()
        r.transport.state.value = ConnState.Retrying(500, "network")
        r.transport.state.value = ConnState.Connected                      // the server forgot us: say it all again
        assertEquals(listOf("tts", "voice_stream"), r.transport.sentTypes())
    }

    @Test fun disconnectingReleasesTheMicAndTheVoice() = runTest {
        val r = Rig()
        r.controller.connect()
        r.controller.setHandsFree(true)
        r.controller.disconnect()
        assertFalse(r.mic.running)
        assertTrue(r.speech.events.contains("flush"))
        assertEquals(ConnState.Disconnected, r.state.connection)
    }

    // -------------------------------------------------------------------------------------- camera
    @Test fun framesAreSentOnlyWhileSharing() = runTest {
        val r = Rig()
        r.controller.connect()
        assertFalse(r.controller.sendFrame(ByteArray(10), 640, 480))
        r.controller.setSharing(true)
        assertTrue(r.controller.sendFrame(byteArrayOf(1, 2, 3), 640, 480))
        val frame = Json.parseToJsonElement(r.transport.sentText.single()).jsonObject
        assertEquals("video_frame", (frame["type"] as JsonPrimitive).content)
        assertEquals("AQID", (frame["data"] as JsonPrimitive).content)      // base64 of 1,2,3
        assertEquals("camera", (frame["source"] as JsonPrimitive).content)
        assertEquals(5L, r.state.lastFrameAtMs)
        r.controller.setSharing(false)
        assertFalse(r.controller.sendFrame(ByteArray(10), 640, 480))
    }

    // ------------------------------------------------------------------------------------ settings
    @Test fun newSettingsAreSanitisedSavedAndReconnected() = runTest {
        val r = Rig()
        r.controller.connect()
        r.controller.applySettings(Settings(serverUrl = "https://agent.example.com", token = "tok", sessionId = "my session!", speakReplies = true))
        assertEquals("wss://agent.example.com/ws/my_session_?token=tok", r.transport.connectedTo)
        assertEquals("my_session_", r.store.settings.sessionId)
        assertTrue(r.state.speakReplies)
    }

    @Test fun anErrorBannerCanBeDismissed() = runTest {
        val r = Rig()
        r.receive("""{"type":"error","code":"rate_limit"}""")
        assertTrue(r.state.error != null)
        r.controller.dismissError()
        assertEquals(null, r.state.error)
    }

    @Test fun theFullConversationFlowEndToEnd() = runTest {
        val r = Rig()
        r.controller.connect()
        r.controller.sendText("Find flights from Delhi to Mumbai")
        r.receive(action("filler", """"text":"Looking that up right now...""""))
        r.receive(action("tool_call", """"call_id":"c1","tool_name":"search_flights","arguments":{"origin":"Delhi","destination":"Mumbai"},"is_state_modifying":false"""))
        r.controller.sendText("No wait, make it Goa")
        r.receive(action("tool_cancel", """"call_id":"c1","tool_name":"search_flights","reason":"user_correction""""))
        val s = r.state
        assertEquals(listOf(Role.USER, Role.FILLER, Role.USER), s.messages.map { it.role })
        assertEquals(listOf("tool_call", "tool_cancel"), s.trace.map { it.type }.filter { it.startsWith("tool") })
        assertTrue(AgentAction.Unknown::class.java.simpleName.isNotEmpty())
    }
}
