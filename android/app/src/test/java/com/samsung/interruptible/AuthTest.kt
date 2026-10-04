package com.samsung.interruptible

import com.samsung.interruptible.data.AuthProbe
import com.samsung.interruptible.data.ConnState
import com.samsung.interruptible.data.HttpAuthApi
import com.samsung.interruptible.data.Settings
import com.samsung.interruptible.state.AgentController
import com.samsung.interruptible.state.AuthStatus
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The real HTTP probe against a real (mock) server. */
class HttpAuthApiTest {
    private fun server(authRequired: Boolean, key: String = "k3y"): MockWebServer = MockWebServer().also {
        it.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when (request.path) {
                "/health" -> MockResponse().setBody("""{"status":"ok","auth_required":$authRequired}""")
                "/auth/check" -> if (request.getHeader("Authorization") == "Bearer $key") MockResponse().setBody("""{"ok":true}""") else MockResponse().setResponseCode(401)
                else -> MockResponse().setResponseCode(404)
            }
        }
        it.start()
    }

    private fun ws(s: MockWebServer) = "ws://${s.hostName}:${s.port}"

    @Test fun anOpenServerNeedsNoKey() = runBlocking {
        val s = server(authRequired = false)
        assertEquals(AuthProbe.Open, HttpAuthApi().probe(ws(s), ""))
        s.shutdown()
    }

    @Test fun aProtectedServerWithNoKeyAsksForOne() = runBlocking {
        val s = server(authRequired = true)
        assertEquals(AuthProbe.NeedsKey(hadKey = false), HttpAuthApi().probe(ws(s), ""))
        s.shutdown()
    }

    @Test fun theRightKeyIsValidAndTheWrongOneIsRejected() = runBlocking {
        val s = server(authRequired = true)
        assertEquals(AuthProbe.Valid, HttpAuthApi().probe(ws(s), "  k3y  "))          // pasted whitespace is tolerated
        assertEquals(AuthProbe.NeedsKey(hadKey = true), HttpAuthApi().probe(ws(s), "wrong"))
        s.shutdown()
    }

    @Test fun anUnreachableServerIsReportedNotThrown() = runBlocking {
        val s = server(authRequired = true)
        val address = ws(s)
        s.shutdown()
        assertTrue(HttpAuthApi().probe(address, "k3y") is AuthProbe.Unreachable)
    }

    @Test fun aServerErrorIsUnreachableNotAFakeSuccess() = runBlocking {
        val s = MockWebServer().also { it.enqueue(MockResponse().setResponseCode(500)); it.start() }
        assertTrue(HttpAuthApi().probe(ws(s), "k") is AuthProbe.Unreachable)
        s.shutdown()
    }

    @Test fun garbageFromTheServerIsUnreachableNotACrash() = runBlocking {
        val s = MockWebServer().also { it.enqueue(MockResponse().setBody("<html>not json</html>")); it.start() }
        assertTrue(HttpAuthApi().probe(ws(s), "k") is AuthProbe.Unreachable)
        s.shutdown()
    }
}

@OptIn(ExperimentalCoroutinesApi::class)
class SignInFlowTest {
    private class Rig(answer: (String, String) -> AuthProbe, token: String = "") {
        val scope = TestScope(UnconfinedTestDispatcher())
        val transport = FakeTransport()
        val api = FakeAuthApi(answer)
        val store = FakeStore(Settings(serverUrl = "ws://10.0.2.2:8000", token = token, sessionId = "sess_1"))
        val controller = AgentController(transport, FakeSpeech(), FakeMic(), store, scope.backgroundScope, authApi = api).also { it.start() }
    }

    @Test fun anOpenServerGoesStraightInAndConnects() = runTest {
        val r = Rig({ _, _ -> AuthProbe.Open })
        assertEquals(AuthStatus.Checking, r.controller.auth.value)
        r.controller.begin()
        assertEquals(AuthStatus.Ready, r.controller.auth.value)
        assertEquals("ws://10.0.2.2:8000/ws/sess_1", r.transport.connectedTo)
    }

    @Test fun aProtectedServerWithoutAKeyShowsTheLoginAndDoesNotConnect() = runTest {
        val r = Rig({ _, _ -> AuthProbe.NeedsKey(hadKey = false) })
        r.controller.begin()
        assertEquals(AuthStatus.Login(""), r.controller.auth.value)
        assertEquals(null, r.transport.connectedTo)
    }

    @Test fun aSavedKeyThatNoLongerWorksExplainsWhyItIsAsking() = runTest {
        val r = Rig({ _, _ -> AuthProbe.NeedsKey(hadKey = true) }, token = "old")
        r.controller.begin()
        assertTrue((r.controller.auth.value as AuthStatus.Login).notice.contains("no longer valid"))
    }

    @Test fun aSavedValidKeySkipsTheLogin() = runTest {
        val r = Rig({ _, token -> if (token == "good") AuthProbe.Valid else AuthProbe.NeedsKey(true) }, token = "good")
        r.controller.begin()
        assertEquals(AuthStatus.Ready, r.controller.auth.value)
        assertEquals("ws://10.0.2.2:8000/ws/sess_1?token=good", r.transport.connectedTo)
    }

    @Test fun anUnreachableServerStillOpensTheAppWithItsReconnectState() = runTest {
        val r = Rig({ _, _ -> AuthProbe.Unreachable("timeout") })
        r.controller.begin()
        assertEquals(AuthStatus.Ready, r.controller.auth.value)
        assertTrue(r.transport.connectedTo != null)
    }

    @Test fun signingInVerifiesBeforeStoringAndThenConnects() = runTest {
        val r = Rig({ _, token -> if (token == "k3y") AuthProbe.Valid else AuthProbe.NeedsKey(true) })
        r.controller.begin()
        assertEquals("That key was not accepted.", r.controller.signIn("ws://192.168.1.5:8000", "nope"))
        assertEquals("a wrong key must not be stored", "", r.store.settings.token)
        assertEquals(null, r.transport.connectedTo)
        assertNull(r.controller.signIn("ws://192.168.1.5:8000", " k3y "))
        assertEquals(AuthStatus.Ready, r.controller.auth.value)
        assertEquals("k3y", r.store.settings.token)
        assertEquals("ws://192.168.1.5:8000", r.store.settings.serverUrl)
        assertEquals("ws://192.168.1.5:8000/ws/sess_1?token=k3y", r.transport.connectedTo)
    }

    @Test fun signInReportsAnUnreachableServerClearly() = runTest {
        val r = Rig({ _, _ -> AuthProbe.Unreachable("Connection refused") })
        r.controller.begin()
        r.controller.auth.value.let { }
        val message = r.controller.signIn("ws://10.0.2.2:9", "k")!!
        assertTrue(message.contains("Cannot reach the server") && message.contains("Connection refused"))
    }

    @Test fun aRefusedConnectionSendsYouBackToTheLogin() = runTest {
        val r = Rig({ _, _ -> AuthProbe.Valid }, token = "revoked")
        r.controller.begin()
        r.transport.state.value = ConnState.Refused("refused by the server (HTTP 403)")
        assertTrue((r.controller.auth.value as AuthStatus.Login).notice.contains("did not accept your key"))
    }

    @Test fun signingOutForgetsTheKeyAndShowsTheLogin() = runTest {
        val r = Rig({ _, _ -> AuthProbe.Valid }, token = "k3y")
        r.controller.begin()
        r.controller.signOut()
        assertEquals("", r.store.settings.token)
        assertEquals(ConnState.Disconnected, r.transport.state.value)
        assertEquals(AuthStatus.Login(), r.controller.auth.value)
    }

    @Test fun withoutAnAuthApiTheControllerBehavesAsBefore() = runTest {
        val r = Rig({ _, _ -> AuthProbe.Open })
        val plain = AgentController(r.transport, FakeSpeech(), FakeMic(), r.store, r.scope.backgroundScope)
        assertEquals(AuthStatus.Ready, plain.auth.value)
        plain.begin()
        assertEquals("ws://10.0.2.2:8000/ws/sess_1", r.transport.connectedTo)
    }
}
