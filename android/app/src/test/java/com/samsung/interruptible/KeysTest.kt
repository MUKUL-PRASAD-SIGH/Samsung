package com.samsung.interruptible

import androidx.activity.ComponentActivity
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import com.samsung.interruptible.data.HttpKeysApi
import com.samsung.interruptible.data.KeysApi
import com.samsung.interruptible.data.KeysResult
import com.samsung.interruptible.data.KeysStatus
import com.samsung.interruptible.data.Settings
import com.samsung.interruptible.data.Urls
import com.samsung.interruptible.state.AgentController
import com.samsung.interruptible.ui.AgentTheme
import com.samsung.interruptible.ui.KeysForm
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
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

/** The provider-key client against a real (mock) Kairos server: it speaks the same `/settings/keys` contract as agent/server.py. */
class HttpKeysApiTest {
    private val statusBody = """{"groq":{"configured":true,"hint":"gsk_…cdef"},"openrouter":{"configured":false,"hint":""},"backend":"groq","configured":true}"""

    private fun server(handler: (RecordedRequest) -> MockResponse): MockWebServer = MockWebServer().also {
        it.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest) = handler(request)
        }
        it.start()
    }

    private fun ws(s: MockWebServer) = "ws://${s.hostName}:${s.port}"

    @Test fun statusIsParsedAndSendsTheAccessKey() = runBlocking {
        var auth: String? = null
        val s = server { auth = it.getHeader("Authorization"); MockResponse().setBody(statusBody) }
        val st = HttpKeysApi().status(ws(s), " tok ")
        assertEquals(KeysStatus(true, "gsk_…cdef", false, "", "groq", true), st)
        assertEquals("Bearer tok", auth)
        s.shutdown()
    }

    @Test fun statusIsNullWhenTheServerIsUnreachableOrOld() = runBlocking {
        val old = server { MockResponse().setResponseCode(404) }
        assertNull(HttpKeysApi().status(ws(old), ""))
        val address = ws(old)
        old.shutdown()
        assertNull(HttpKeysApi().status(address, ""))
    }

    @Test fun saveSendsATrimmedPutAndOnlyTheKeysGiven() = runBlocking {
        var method = ""; var body = ""; var type: String? = null
        val s = server { method = it.method.orEmpty(); body = it.body.readUtf8(); type = it.getHeader("Content-Type"); MockResponse().setBody(statusBody.dropLast(1) + ""","warnings":["slow provider"]}""") }
        val r = HttpKeysApi().save(ws(s), "", "  gsk_abc  ", null)
        assertEquals("PUT", method)
        assertEquals("""{"groq_api_key":"gsk_abc"}""", body)
        assertTrue(type!!.startsWith("application/json"))
        assertEquals(KeysResult.Saved(KeysStatus(true, "gsk_…cdef", false, "", "groq", true), listOf("slow provider")), r)
        s.shutdown()
    }

    @Test fun anEmptyStringRemovesAKey() = runBlocking {
        var body = ""
        val s = server { body = it.body.readUtf8(); MockResponse().setBody(statusBody) }
        HttpKeysApi().save(ws(s), "", "", "sk-or-v1-x")
        assertEquals("""{"groq_api_key":"","openrouter_api_key":"sk-or-v1-x"}""", body)
        s.shutdown()
    }

    @Test fun aRejectedKeyCarriesTheServersMessageAndField() = runBlocking {
        val s = server { MockResponse().setResponseCode(400).setBody("""{"error":"Groq did not accept that key.","field":"groq"}""") }
        assertEquals(KeysResult.Rejected("Groq did not accept that key.", "groq"), HttpKeysApi().save(ws(s), "", "gsk_x", null))
        s.shutdown()
    }

    @Test fun unauthorizedOldServerAndOutagesAreDistinguished() = runBlocking {
        val s401 = server { MockResponse().setResponseCode(401) }
        assertEquals(KeysResult.Unauthorized, HttpKeysApi().save(ws(s401), "bad", "gsk_x", null))
        val s404 = server { MockResponse().setResponseCode(404) }
        assertEquals(KeysResult.NotSupported, HttpKeysApi().save(ws(s404), "", "gsk_x", null))
        val s500 = server { MockResponse().setResponseCode(500) }
        assertTrue(HttpKeysApi().save(ws(s500), "", "gsk_x", null) is KeysResult.Unreachable)
        val address = ws(s500)
        s401.shutdown(); s404.shutdown(); s500.shutdown()
        assertTrue(HttpKeysApi().save(address, "", "gsk_x", null) is KeysResult.Unreachable)
    }
}

class FakeKeysApi(var status: KeysStatus? = KeysStatus(), var result: KeysResult = KeysResult.Saved(KeysStatus(true, "gsk_…1234", false, "", "groq", true), emptyList())) : KeysApi {
    val saves = mutableListOf<List<String?>>()
    override suspend fun status(serverUrl: String, token: String) = status
    override suspend fun save(serverUrl: String, token: String, groqKey: String?, openrouterKey: String?): KeysResult {
        saves += listOf(serverUrl, token, groqKey, openrouterKey)
        return result
    }
}

@OptIn(ExperimentalCoroutinesApi::class)
class KeysControllerTest {
    private fun controller(api: KeysApi?, server: String = "ws://192.168.1.20:8000", scope: TestScope) =
        AgentController(
            FakeTransport(), FakeSpeech(), FakeMic(),
            FakeStore(Settings(serverUrl = server, token = "tok", sessionId = "sess_1")), scope.backgroundScope, keysApi = api,
        ).also { it.start() }

    @Test fun savingSendsTheKeysToTheServerWithTheAccessKeyAndUpdatesTheStatus() = runTest {
        val api = FakeKeysApi()
        val c = controller(api, scope = TestScope(UnconfinedTestDispatcher()))
        assertNull(c.saveKeys("gsk_abc", null))
        assertEquals(listOf("ws://192.168.1.20:8000", "tok", "gsk_abc", null), api.saves.single())
        assertTrue(c.keys.value!!.configured && c.keys.value!!.groqConfigured)
    }

    @Test fun keysAreNeverSentInClearTextToAPublicAddress() = runTest {
        val api = FakeKeysApi()
        val c = controller(api, server = "ws://example.com:8000", scope = TestScope(UnconfinedTestDispatcher()))
        assertTrue(c.saveKeys("gsk_abc", null)!!.contains("unencrypted"))
        assertTrue(api.saves.isEmpty())
        val secure = controller(api, server = "wss://example.com", scope = TestScope(UnconfinedTestDispatcher()))
        assertNull(secure.saveKeys("gsk_abc", null))
    }

    @Test fun failuresBecomeReadableMessages() = runTest {
        val api = FakeKeysApi()
        val c = controller(api, scope = TestScope(UnconfinedTestDispatcher()))
        api.result = KeysResult.Rejected("Groq did not accept that key.", "groq")
        assertEquals("Groq did not accept that key.", c.saveKeys("gsk_bad", null))
        api.result = KeysResult.Unauthorized
        assertTrue(c.saveKeys("gsk_x", null)!!.contains("access key"))
        api.result = KeysResult.NotSupported
        assertTrue(c.saveKeys("gsk_x", null)!!.contains("too old"))
        api.result = KeysResult.Unreachable("timeout")
        assertTrue(c.saveKeys("gsk_x", null)!!.contains("timeout"))
        assertNull(c.keys.value)        // a failed save never claims keys are configured
    }

    @Test fun refreshAsksTheServerAndAServerWithoutKeysIsVisible() = runTest {
        val c = controller(FakeKeysApi(status = KeysStatus()), scope = TestScope(UnconfinedTestDispatcher()))
        assertNull(c.keys.value)
        c.refreshKeys()
        assertFalse(c.keys.value!!.configured)
    }

    @Test fun aBuildWithoutKeysSupportSaysSo() = runTest {
        val c = controller(null, scope = TestScope(UnconfinedTestDispatcher()))
        assertTrue(c.saveKeys("gsk_x", null)!!.contains("cannot manage"))
        c.refreshKeys()
        assertNull(c.keys.value)
    }
}

class PrivateAddressTest {
    @Test fun privateNetworksAndLocalhostCount() {
        for (a in listOf("ws://192.168.1.20:8000", "ws://10.0.0.5:8000", "http://172.16.4.2:8000", "ws://172.31.255.1", "ws://169.254.1.1",
            "ws://localhost:8000", "ws://10.0.2.2:8000", "ws://my-pc.local:8000", "192.168.0.9:8000")) assertTrue(a, Urls.isPrivateOrLocal(a))
    }

    @Test fun publicAddressesDoNot() {
        for (a in listOf("ws://example.com", "ws://8.8.8.8:8000", "ws://172.32.0.1", "ws://172.15.0.1", "ws://192.169.1.1", "ws://11.0.0.1", "ws://1.2.3.4.5"))
            assertFalse(a, Urls.isPrivateOrLocal(a))
    }
}

@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [34], qualifiers = "w411dp-h891dp-xxhdpi")
class KeysFormUiTest {
    @get:Rule val rule = createAndroidComposeRule<ComponentActivity>()

    private fun show(status: KeysStatus?, server: String = "wss://k.example", onSave: suspend (String?, String?) -> String?) =
        rule.setContent { AgentTheme { KeysForm(status, server, onSave, onClose = {}) } }

    @Test fun savingIsDisabledUntilAKeyIsTypedThenSendsOnlyThatKey() {
        val sent = mutableListOf<Pair<String?, String?>>()
        show(KeysStatus()) { g, o -> sent += g to o; null }
        rule.onNodeWithText("Save keys").assertIsDisplayed()
        rule.onNodeWithText("Groq key").performTextInput("gsk_typed")
        rule.onNodeWithText("Save keys").performClick()
        rule.waitForIdle()
        assertEquals(listOf<Pair<String?, String?>>("gsk_typed" to null), sent)
        rule.onNodeWithText("Saved. Kairos is now using your key.").assertIsDisplayed()
    }

    @Test fun aRejectedKeyShowsTheServersMessage() {
        show(KeysStatus()) { _, _ -> "Groq did not accept that key." }
        rule.onNodeWithText("OpenRouter key").performTextInput("sk-or-v1-bad")
        rule.onNodeWithText("Save keys").performClick()
        rule.waitForIdle()
        rule.onNodeWithText("Groq did not accept that key.").assertIsDisplayed()
    }

    @Test fun savedKeysShowAMaskedHintAndNeverTheKey() {
        show(KeysStatus(groqConfigured = true, groqHint = "gsk_…cdef", backend = "groq", configured = true)) { _, _ -> null }
        rule.onNodeWithText("Saved: gsk_…cdef").assertIsDisplayed()
    }

    @Test fun anUnencryptedServerGetsAWarning() {
        show(KeysStatus(), server = "ws://192.168.1.20:8000") { _, _ -> null }
        rule.onNodeWithText("Your server address is not encrypted", substring = true).assertIsDisplayed()
    }
}

/** A fresh install on a phone must ask where the server is instead of retrying the emulator's address forever. */
@OptIn(ExperimentalCoroutinesApi::class)
class FirstRunTest {
    private class FreshStore : com.samsung.interruptible.data.SettingsStore {
        var saved: Settings? = null
        override fun load() = saved ?: Settings(serverUrl = "ws://10.0.2.2:8000", sessionId = "sess_1")
        override fun save(settings: Settings) { saved = settings }
        override fun hasSavedServer() = saved != null
    }

    private fun rig(answer: (String, String) -> com.samsung.interruptible.data.AuthProbe): Triple<AgentController, FreshStore, FakeTransport> {
        val scope = TestScope(UnconfinedTestDispatcher())
        val store = FreshStore()
        val t = FakeTransport()
        return Triple(AgentController(t, FakeSpeech(), FakeMic(), store, scope.backgroundScope, authApi = FakeAuthApi(answer)).also { it.start() }, store, t)
    }

    @Test fun aFreshInstallAsksForTheServerAndDoesNotConnect() = runTest {
        val (c, _, t) = rig { _, _ -> com.samsung.interruptible.data.AuthProbe.Open }
        c.begin()
        assertEquals(com.samsung.interruptible.state.AuthStatus.Login("", firstRun = true), c.auth.value)
        assertNull(t.connectedTo)
    }

    @Test fun anOpenServerConnectsWithoutAnyKeyAndIsRemembered() = runTest {
        val (c, store, t) = rig { _, _ -> com.samsung.interruptible.data.AuthProbe.Open }
        c.begin()
        assertNull(c.signIn("ws://192.168.1.20:8000", ""))
        assertEquals("ws://192.168.1.20:8000", store.saved!!.serverUrl)
        assertEquals("ws://192.168.1.20:8000/ws/sess_1", t.connectedTo)
        assertEquals(com.samsung.interruptible.state.AuthStatus.Ready, c.auth.value)
    }

    @Test fun aServerThatNeedsAKeyExplainsWhereToFindIt() = runTest {
        val (c, store, _) = rig { _, _ -> com.samsung.interruptible.data.AuthProbe.NeedsKey(hadKey = false) }
        c.begin()
        assertTrue(c.signIn("ws://192.168.1.20:8000", "")!!.contains("needs an access key"))
        assertNull(store.saved)
    }

    @Test fun anUnreachableAddressIsReportedAndNothingIsSaved() = runTest {
        val (c, store, _) = rig { _, _ -> com.samsung.interruptible.data.AuthProbe.Unreachable("timeout") }
        c.begin()
        assertTrue(c.signIn("ws://192.168.1.99:8000", "")!!.contains("Cannot reach"))
        assertNull(store.saved)
    }
}

@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [34], qualifiers = "w411dp-h891dp-xxhdpi")
class FirstRunUiTest {
    @get:Rule val rule = createAndroidComposeRule<ComponentActivity>()

    @Test fun theFirstRunScreenAcceptsAnAddressWithoutAKey() {
        var asked: Pair<String, String>? = null
        rule.setContent { AgentTheme { com.samsung.interruptible.ui.LoginScreen("ws://10.0.2.2:8000", "", firstRun = true) { s, k -> asked = s to k; null } } }
        rule.onNodeWithText("Connect to Kairos").assertIsDisplayed()
        rule.onNodeWithText("Server").performTextInput("ws://192.168.1.20:8000")
        rule.onNodeWithText("Connect").performClick()
        rule.waitForIdle()
        assertEquals("ws://192.168.1.20:8000" to "", asked)
    }
}
