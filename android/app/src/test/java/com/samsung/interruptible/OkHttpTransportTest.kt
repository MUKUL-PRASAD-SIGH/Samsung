package com.samsung.interruptible

import com.samsung.interruptible.data.AgentAction
import com.samsung.interruptible.data.Backoff
import com.samsung.interruptible.data.ConnState
import com.samsung.interruptible.data.OkHttpTransport
import com.samsung.interruptible.data.ServerMessage
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicInteger

/** The real OkHttp transport against a real WebSocket server (MockWebServer): handshake, parsing, send, reconnect, refusal. */
class OkHttpTransportTest {
    private lateinit var server: MockWebServer
    private lateinit var scope: CoroutineScope
    private lateinit var transport: OkHttpTransport
    private val received = CopyOnWriteArrayList<String>()
    private val serverSockets = CopyOnWriteArrayList<WebSocket>()
    private val opens = AtomicInteger()

    @Before fun setUp() {
        server = MockWebServer().also { it.start() }
        scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        transport = OkHttpTransport(scope, backoff = Backoff(baseMs = 30, maxMs = 120))
    }

    @After fun tearDown() {
        transport.disconnect()
        scope.cancel()
        serverSockets.forEach { runCatching { it.close(1000, "test over") } }   // MockWebServer waits for open sockets otherwise
        runCatching { server.shutdown() }
    }

    private fun accept(onOpen: (WebSocket) -> Unit = {}) {
        server.enqueue(MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) { opens.incrementAndGet(); serverSockets += webSocket; onOpen(webSocket) }
            override fun onMessage(webSocket: WebSocket, text: String) { received += text }
        }))
    }

    private fun url() = server.url("/ws/sess_1").toString().replace("http://", "ws://")

    private fun waitFor(what: String, timeoutMs: Long = 5_000, cond: () -> Boolean) {
        val end = System.currentTimeMillis() + timeoutMs
        while (!cond()) {
            if (System.currentTimeMillis() > end) error("timed out waiting for $what (state=${transport.state.value})")
            Thread.sleep(10)
        }
    }

    @Test fun deliversMessagesInOrderSkippingUnparseableOnes() {
        val seen = CopyOnWriteArrayList<ServerMessage>()
        val job = scope.launch { transport.incoming.collect { seen += it } }
        accept { ws ->
            Thread.sleep(150)               // let the collector subscribe to the hot flow before anything is sent
            ws.send("not json at all")
            ws.send("""{"action_type":"filler","epoch":1,"timestamp":1.0,"text":"one"}""")
            ws.send("""{"action_type":"spoken_response","epoch":1,"timestamp":1.0,"text":"two"}""")
        }
        transport.connect(url())
        waitFor("two messages") { seen.size >= 2 }
        assertEquals(listOf("one", "two"), seen.map { ((it as ServerMessage.Action).action).let { a -> (a as? AgentAction.Filler)?.text ?: (a as AgentAction.SpokenResponse).text } })
        job.cancel()
    }

    @Test fun sendsTextAndBinaryOnlyWhenConnected() {
        assertFalse("before connecting nothing is sent (and nothing is queued for later)", transport.sendText("early"))
        accept()
        transport.connect(url())
        waitFor("connected") { transport.state.value == ConnState.Connected }
        assertTrue(transport.sendText("""{"type":"interrupt"}"""))
        assertTrue(transport.sendBinary(byteArrayOf(1, 2, 3)))
        waitFor("server saw the text") { received.contains("""{"type":"interrupt"}""") }
        assertFalse(received.contains("early"))
    }

    @Test fun reconnectsAfterTheServerDropsTheConnection() {
        accept()
        accept()
        transport.connect(url())
        waitFor("first connection") { opens.get() == 1 && transport.state.value == ConnState.Connected }
        serverSockets[0].close(1001, "going away")
        waitFor("second connection") { opens.get() == 2 && transport.state.value == ConnState.Connected }
        assertTrue(transport.sendText("again"))
        waitFor("delivery on the new socket") { received.contains("again") }
    }

    @Test fun aServerThatIsDownForAWhileIsRetriedUntilItComesBack() {
        accept()
        repeat(3) { server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START)) }   // connection attempts that die
        accept()
        transport.connect(url())
        waitFor("connected") { transport.state.value == ConnState.Connected }
        serverSockets[0].close(1001, "restarting")
        waitFor("retrying") { transport.state.value is ConnState.Retrying }
        waitFor("reconnected once the server is back") { opens.get() == 2 && transport.state.value == ConnState.Connected }
        assertEquals("1 good + 3 dead + 1 good handshake", 5, server.requestCount)
    }

    @Test fun aRefusedHandshakeIsNotRetriedBecauseRetryingCannotHelp() {
        server.enqueue(MockResponse().setResponseCode(403))
        server.enqueue(MockResponse().setResponseCode(403))
        transport.connect(url())
        waitFor("refused") { transport.state.value is ConnState.Refused }
        assertTrue((transport.state.value as ConnState.Refused).reason.contains("403"))
        Thread.sleep(400)                                          // several backoff periods
        assertEquals("exactly one handshake was attempted", 1, server.requestCount)
    }

    @Test fun aPolicyViolationCloseIsAlsoTerminal() {
        accept { ws -> ws.close(1008, "policy") }
        transport.connect(url())
        waitFor("refused") { transport.state.value is ConnState.Refused }
        Thread.sleep(300)
        assertEquals(1, server.requestCount)
    }

    @Test fun serverBusyIsRetriedWithBackoff() {
        server.enqueue(MockResponse().setResponseCode(503))
        accept()
        transport.connect(url())
        waitFor("connected after a 503") { transport.state.value == ConnState.Connected }
        assertEquals(2, server.requestCount)
    }

    @Test fun disconnectStopsReconnectingForGood() {
        accept()
        transport.connect(url())
        waitFor("connected") { transport.state.value == ConnState.Connected }
        transport.disconnect()
        assertEquals(ConnState.Disconnected, transport.state.value)
        Thread.sleep(400)
        assertEquals(1, server.requestCount)
        assertEquals(ConnState.Disconnected, transport.state.value)
        assertFalse(transport.sendText("nope"))
    }

    @Test fun connectingToANewUrlReplacesTheOldSocket() {
        accept()
        accept()
        transport.connect(url())
        waitFor("first") { opens.get() == 1 && transport.state.value == ConnState.Connected }
        transport.connect(url())
        waitFor("second") { opens.get() == 2 && transport.state.value == ConnState.Connected }
        // a late close callback from the replaced socket must not flip the state to retrying
        Thread.sleep(300)
        assertEquals(ConnState.Connected, transport.state.value)
    }
}
