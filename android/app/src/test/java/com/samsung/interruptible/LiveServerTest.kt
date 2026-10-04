package com.samsung.interruptible

import com.samsung.interruptible.audio.Pcm
import com.samsung.interruptible.data.AgentAction
import com.samsung.interruptible.data.Backoff
import com.samsung.interruptible.data.ConnState
import com.samsung.interruptible.data.OkHttpTransport
import com.samsung.interruptible.data.Outgoing
import com.samsung.interruptible.data.ServerMessage
import com.samsung.interruptible.data.Urls
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.CopyOnWriteArrayList

/**
 * The real client code against a REAL running server. Skipped unless AGENT_SERVER_URL is set:
 *
 *   AGENT_SERVER_URL=ws://127.0.0.1:8000 [AGENT_TOKEN=...] ./gradlew testDebugUnitTest --tests '*LiveServerTest'
 *
 * (From the Docker build image add --network host to reach a server on the host.) It exercises exactly what the app does:
 * the same OkHttpTransport, Protocol parser and Outgoing frames, with real speech fed in as 100 ms PCM frames.
 */
class LiveServerTest {
    private val serverUrl = System.getenv("AGENT_SERVER_URL")
    private val token = System.getenv("AGENT_TOKEN")
    private lateinit var scope: CoroutineScope
    private lateinit var transport: OkHttpTransport
    private val seen = CopyOnWriteArrayList<ServerMessage>()

    @Before fun setUp() {
        assumeTrue("AGENT_SERVER_URL not set: skipping the live-server test", !serverUrl.isNullOrBlank())
        scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        transport = OkHttpTransport(scope, backoff = Backoff(baseMs = 200, maxMs = 1000))
        scope.launch { transport.incoming.collect { seen += it } }
        transport.connect(Urls.webSocketUrl(serverUrl!!, Urls.newSessionId(), token))
        waitFor("connection", 10_000) { transport.state.value == ConnState.Connected }
    }

    @After fun tearDown() {
        if (this::transport.isInitialized) transport.disconnect()
        if (this::scope.isInitialized) scope.cancel()
    }

    private fun waitFor(what: String, timeoutMs: Long = 20_000, cond: () -> Boolean) {
        val end = System.currentTimeMillis() + timeoutMs
        while (!cond()) {
            if (System.currentTimeMillis() > end) error("timed out waiting for $what; saw ${seen.map { describe(it) }}")
            Thread.sleep(25)
        }
    }

    private fun describe(m: ServerMessage) = when (m) {
        is ServerMessage.Action -> m.action.javaClass.simpleName + (if (m.action is AgentAction.Transcript) "(${(m.action as AgentAction.Transcript).text})" else "")
        else -> m.toString()
    }

    private inline fun <reified T : AgentAction> actions(): List<T> = seen.filterIsInstance<ServerMessage.Action>().map { it.action }.filterIsInstance<T>()

    @Test fun aTypedRequestGetsAnImmediateAcknowledgementAndAnAnswer() {
        assertTrue(transport.sendText(Outgoing.userText("What's the weather like in Paris?")))
        waitFor("a filler acknowledgement", 5_000) { actions<AgentAction.Filler>().isNotEmpty() }
        waitFor("a reply", 30_000) { actions<AgentAction.SpokenResponse>().isNotEmpty() || actions<AgentAction.Clarification>().isNotEmpty() }
        waitFor("a state snapshot") { actions<AgentAction.StateSnapshot>().isNotEmpty() }
        assertTrue(actions<AgentAction.StateSnapshot>().last().epoch >= 1)
    }

    @Test fun speakingIsOptInAndDeliversPlayableAudio() {
        transport.sendText(Outgoing.tts(true))
        waitFor("tts_status") { seen.any { it is ServerMessage.TtsStatus } }
        val status = seen.filterIsInstance<ServerMessage.TtsStatus>().first()
        assumeTrue("this server has no TTS backend", status.available)
        assertTrue(status.enabled)
        transport.sendText(Outgoing.userText("What's the weather like in Paris?"))
        waitFor("spoken audio", 40_000) { actions<AgentAction.AudioOut>().isNotEmpty() }
        val chunk = actions<AgentAction.AudioOut>().first()
        val pcm = Pcm.decodeBase64(chunk.audioB64)
        assertTrue("real audio bytes", pcm.size > 1_000 && pcm.size % 2 == 0)
        assertEquals("the declared duration matches the samples", chunk.durationMs, Pcm.durationMs(pcm.size, chunk.sampleRate), 2.0)
        assertTrue(actions<AgentAction.SpeechState>().any { it.state == "started" })
    }

    @Test fun typingWhileTheAgentTalksStopsItAndReportsWhatWasHeard() {
        transport.sendText(Outgoing.tts(true))
        waitFor("tts_status") { seen.any { it is ServerMessage.TtsStatus } }
        assumeTrue("this server has no TTS backend", seen.filterIsInstance<ServerMessage.TtsStatus>().first().available)
        transport.sendText(Outgoing.userText("Find flights from Delhi to Mumbai"))
        waitFor("the agent starts speaking", 40_000) { actions<AgentAction.AudioOut>().isNotEmpty() }
        transport.sendText(Outgoing.userText("Stop, never mind"))
        waitFor("speech stopped", 10_000) { actions<AgentAction.SpeechState>().any { it.state == "stopped" } }
        val stopped = actions<AgentAction.SpeechState>().first { it.state == "stopped" }
        assertTrue(stopped.spokenText != null && stopped.spokenText!!.length < stopped.text.length)
    }

    @Test fun realSpeechStreamedAs100msFramesIsTranscribed() {
        val pcm = javaClass.classLoader!!.getResourceAsStream("book_flight_16k.pcm")!!.readBytes()
        transport.sendText(Outgoing.voiceStream("start"))
        waitFor("the server to say it is listening") { actions<AgentAction.VoiceActivity>().any { it.state == "listening" } }
        val frames = (ByteArray(Pcm.FRAME_BYTES * 3) + pcm + ByteArray(Pcm.FRAME_BYTES * 12)).toList().chunked(Pcm.FRAME_BYTES)
        for (f in frames) {
            assertTrue(transport.sendBinary(f.toByteArray()))
            Thread.sleep(100)                                           // real-time pace, like the microphone
        }
        waitFor("a final transcript", 30_000) { actions<AgentAction.Transcript>().any { !it.isPartial } }
        val final = actions<AgentAction.Transcript>().first { !it.isPartial }
        assertEquals("book a flight from delhi to mumbai", final.text.lowercase().replace(Regex("[^a-z ]"), "").trim())
        assertTrue(actions<AgentAction.VoiceActivity>().map { it.state }.containsAll(listOf("listening", "speech_start", "speech_end")))
        transport.sendText(Outgoing.voiceStream("stop"))
    }
}
