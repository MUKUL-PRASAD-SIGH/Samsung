package com.samsung.interruptible

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.activity.ComponentActivity
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import com.samsung.interruptible.data.AgentAction
import com.samsung.interruptible.data.ConnState
import com.samsung.interruptible.data.Protocol
import com.samsung.interruptible.data.Settings
import com.samsung.interruptible.state.AgentController
import com.samsung.interruptible.state.ChatMessage
import com.samsung.interruptible.state.ChatState
import com.samsung.interruptible.state.Role
import com.samsung.interruptible.ui.AgentApp
import com.samsung.interruptible.ui.AgentTheme
import com.samsung.interruptible.ui.ChatScreen
import com.samsung.interruptible.ui.LoginScreen
import com.samsung.interruptible.ui.SettingsForm
import androidx.compose.foundation.layout.padding
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File
import java.io.FileOutputStream

/**
 * Compose UI tests on the JVM (Robolectric): the real composables, rendered and clicked, no emulator. They also write PNG
 * screenshots of the main screens to app/build/screenshots/ so the UI can be looked at without a device.
 */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [34], qualifiers = "w411dp-h891dp-xxhdpi")
class UiTest {
    @get:Rule val rule = createAndroidComposeRule<ComponentActivity>()

    /** Draws the whole window into a PNG. (captureToImage waits for a hardware draw pass Robolectric never delivers.) */
    private fun shot(name: String) {
        rule.waitForIdle()
        val dir = File("build/screenshots").apply { mkdirs() }
        val view = rule.activity.window.decorView
        val bitmap = android.graphics.Bitmap.createBitmap(view.width.coerceAtLeast(1), view.height.coerceAtLeast(1), android.graphics.Bitmap.Config.ARGB_8888)
        view.draw(android.graphics.Canvas(bitmap))
        FileOutputStream(File(dir, "$name.png")).use { bitmap.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, it) }
    }

    private fun msg(id: Long, role: Role, text: String, heard: String? = null, q: Boolean = false) = ChatMessage(id, role, text, heard = heard, isQuestion = q)

    private fun chat(state: ChatState, onSend: (String) -> Boolean = { true }, onStop: () -> Unit = {}, onMic: () -> Unit = {},
                     onSpeak: () -> Unit = {}, onCamera: () -> Unit = {}) {
        rule.setContent {
            AgentTheme {
                ChatScreen(state, onSend, onStop, onMic, onSpeak, onCamera, { _, _, _ -> true }, {})
            }
        }
    }

    private val conversation = ChatState(
        connection = ConnState.Connected, epoch = 3,
        messages = listOf(
            msg(1, Role.USER, "Find flights from Delhi to Mumbai"),
            msg(2, Role.FILLER, "Looking that up right now..."),
            msg(3, Role.AGENT, "I found **2 flights** from DEL to BOM.\n- cheapest: IndiGo 6E-455 at \$145\n- fastest: Air India AI-102", heard = "I found two flights"),
            msg(4, Role.USER, "No wait, make it Goa"),
            msg(5, Role.AGENT, "Do you want me to change what I'm working on?", q = true),
        ),
    )

    @Test fun conversationRendersEveryRoleAndTheInterruptedNote() {
        chat(conversation)
        rule.onNodeWithText("Find flights from Delhi to Mumbai").assertIsDisplayed()
        rule.onNodeWithText("Looking that up right now...").assertIsDisplayed()
        rule.onNodeWithText("No wait, make it Goa").assertIsDisplayed()
        rule.onNodeWithText("Do you want me to change what I'm working on?").assertIsDisplayed()
        rule.onNodeWithText("You heard: “I found two flights” … then interrupted").assertIsDisplayed()
        shot("chat_conversation")
    }

    @Test fun anEmptyChatShowsTheBrandAndOneTapSuggestions() {
        val picked = mutableListOf<String>()
        rule.setContent { AgentTheme { ChatScreen(ChatState(), { true }, {}, {}, {}, {}, { _, _, _ -> true }, {}, onSuggestion = { picked += it }) } }
        rule.onNodeWithText("KAIROS").assertIsDisplayed()
        rule.onNodeWithText("ΚΑΙΡΟΣ").assertIsDisplayed()
        rule.onNodeWithText("Waiting for the server…").assertIsDisplayed()
        rule.onNodeWithText("Open in VS Code").performClick()
        assertTrue(picked.single().contains("reverse.py") && picked.single().contains("VS Code"))
        shot("chat_empty")
    }

    @Test fun anExportedFileAppearsWithItsActions() {
        chat(conversation.copy(exports = listOf(com.samsung.interruptible.state.ExportItem(9, "reverse.py", "/home/me/kairos-exports/reverse.py", 52, "/exports/reverse.py", "code"))))
        rule.onNodeWithText("reverse.py").assertIsDisplayed()
        rule.onNodeWithText("/home/me/kairos-exports/reverse.py").assertIsDisplayed()
        rule.onNodeWithText("Copy path").assertIsDisplayed()
        rule.onNodeWithText("Download").assertIsDisplayed()
        shot("chat_export")
    }

    // ------------------------------------------------------------------------------------- sign in
    private fun login(notice: String = "", onSignIn: suspend (String, String) -> String? = { _, _ -> null }) {
        rule.setContent { AgentTheme { LoginScreen("ws://10.0.2.2:8000", notice, onSignIn = onSignIn) } }
    }

    @Test fun loginShowsTheBrandAndDisablesContinueUntilThereIsAKey() {
        login()
        rule.onNodeWithText("KAIROS").assertIsDisplayed()
        rule.onNodeWithText("Welcome back").assertIsDisplayed()
        rule.onNodeWithText("Continue").assertIsNotEnabled()
        shot("login")
    }

    @Test fun loginSubmitsServerAndKeyAndShowsTheServersObjection() {
        val calls = mutableListOf<Pair<String, String>>()
        login(onSignIn = { server, key -> calls += server to key; "That key was not accepted." })
        rule.onNodeWithText("Access key").performTextInput("wrong")
        rule.onNodeWithText("Continue").assertIsEnabled().performClick()
        rule.waitForIdle()
        assertEquals(listOf("ws://10.0.2.2:8000" to "wrong"), calls)
        rule.onNodeWithText("That key was not accepted.").assertIsDisplayed()
        shot("login_error")
    }

    @Test fun loginExplainsWhyItIsAskingAndWarnsAboutPlaintextRemotes() {
        rule.setContent { AgentTheme { LoginScreen("ws://192.168.1.5:8000", "Your saved key is no longer valid. Please sign in again.") { _, _ -> null } } }
        rule.onNodeWithText("Your saved key is no longer valid. Please sign in again.").assertIsDisplayed()
        rule.onNodeWithText("This address is not encrypted", substring = true).assertIsDisplayed()
    }

    @Test fun sendIsDisabledUntilThereIsTextThenSendsAndClears() {
        val sent = mutableListOf<String>()
        chat(ChatState(), onSend = { sent += it; true })
        rule.onNodeWithContentDescription("Send").assertIsNotEnabled()
        rule.onNodeWithText("Ask, or interrupt…").performTextInput("hello agent")
        rule.onNodeWithContentDescription("Send").assertIsEnabled().performClick()
        assertEquals(listOf("hello agent"), sent)
        rule.onNodeWithText("hello agent").assertDoesNotExist()                 // the draft was cleared after sending
    }

    @Test fun aFailedSendKeepsTheDraft() {
        chat(ChatState(), onSend = { false })
        rule.onNodeWithText("Ask, or interrupt…").performTextInput("keep me")
        rule.onNodeWithContentDescription("Send").performClick()
        rule.onNodeWithText("keep me").assertIsDisplayed()
    }

    @Test fun stopReplacesSendWhileTheAgentIsBusyAndInterrupts() {
        var stopped = 0
        chat(conversation.copy(inFlight = listOf(AgentAction.InFlightCall("c1", "search_flights", 3, "running"))), onStop = { stopped++ })
        rule.onNodeWithContentDescription("Send").assertDoesNotExist()
        rule.onNodeWithContentDescription("Stop").assertIsDisplayed().performClick()
        assertEquals(1, stopped)
        rule.onNodeWithText("Ask, or interrupt…").performTextInput("actually")
        rule.onNodeWithContentDescription("Send").assertIsDisplayed()           // typing brings Send back
    }

    @Test fun theSpeakerButtonExistsOnlyWhenTheServerCanSpeak() {
        chat(ChatState())
        rule.onNodeWithContentDescription("Speak replies").assertDoesNotExist()
    }

    @Test fun micSpeakerAndCameraTogglesCallBack() {
        val calls = mutableListOf<String>()
        chat(ChatState(ttsAvailable = true), onMic = { calls += "mic" }, onSpeak = { calls += "speak" }, onCamera = { calls += "camera" })
        rule.onNodeWithContentDescription("Hands-free voice").performClick()
        rule.onNodeWithContentDescription("Speak replies").performClick()
        rule.onNodeWithContentDescription("Share camera").performClick()
        assertEquals(listOf("mic", "speak", "camera"), calls)
    }

    @Test fun theListeningBarReflectsWhoIsTalking() {
        chat(ChatState(handsFree = true))
        rule.onNodeWithText("Listening — speak any time to interrupt", substring = true).assertIsDisplayed()
    }

    @Test fun listeningBarWhileTheUserTalksOverTheAgent() {
        chat(ChatState(handsFree = true, agentSpeaking = true, ducked = true))
        rule.onNodeWithText("Hearing you over the agent…", substring = true).assertIsDisplayed()
        shot("chat_barge_in")
    }

    @Test fun livePartialTranscriptShowsWhileSpeaking() {
        chat(ChatState(handsFree = true, userSpeaking = true, livePartial = "no wait make it"))
        rule.onNodeWithText("Hearing you…  no wait make it", substring = true).assertIsDisplayed()
    }

    @Test fun spawnedAgentAndArtifactCards() {
        chat(conversation.copy(
            agents = listOf(com.samsung.interruptible.state.AgentCard("w1", "vector_craft", "SVG Artist", "completed", "Done.", 3, 3)),
            artifact = AgentAction.Artifact("logo.svg", "svg", "<svg width=\"10\"/>"), artifactAuthor = "vector_craft",
        ))
        assertTrue(rule.onAllNodesWithText("vector_craft", substring = true).fetchSemanticsNodes().size >= 1)
        rule.onNodeWithText("logo.svg").assertIsDisplayed()
        rule.onNodeWithText("Copy").assertIsDisplayed()
    }

    // -------------------------------------------------------------------------------------- the whole app
    private fun app(): Pair<AgentController, Rig> {
        val rig = Rig()
        rule.setContent { AgentTheme { AgentApp(rig.controller) } }
        return rig.controller to rig
    }

    private class Rig {
        val scope = TestScope(UnconfinedTestDispatcher())
        val transport = FakeTransport()
        val speech = FakeSpeech()
        val mic = FakeMic()
        val store = FakeStore()
        val controller = AgentController(transport, speech, mic, store, scope.backgroundScope, nowMs = { 1_760_000_000_000L }).also {
            it.start()
            it.connect()
        }
        fun receive(json: String) { transport.incomingFlow.tryEmit(Protocol.parse(json)!!) }
    }

    private fun a(type: String, body: String) = """{"action_type":"$type","epoch":2,"timestamp":1.0,$body}"""

    @Test fun anIncomingConversationAppearsAndTheTabsShowTheAgentsView() {
        val (_, rig) = app()
        rule.onNodeWithText("connected", substring = true).assertIsDisplayed()
        rig.receive(a("filler", """"text":"Looking that up right now...""""))
        rig.receive(a("tool_call", """"call_id":"c1","tool_name":"search_flights","arguments":{"origin":"Delhi","destination":"Goa"},"is_state_modifying":false"""))
        rig.receive(a("state_snapshot", """"intent":"search_flights","slots":{"origin":"Delhi","destination":"Goa"},"in_flight_calls":[{"call_id":"c1","tool":"search_flights","epoch":2,"status":"running"}],"last_updated":"x""""))
        rig.receive(a("tool_cancel", """"call_id":"c0","tool_name":"search_flights","reason":"user_correction""""))
        rule.waitForIdle()
        rule.onNodeWithText("Looking that up right now...").assertIsDisplayed()
        shot("app_chat")

        rule.onNodeWithText("State").performClick()
        rule.onNodeWithText("#2").assertIsDisplayed()
        rule.onNodeWithText("search_flights").assertIsDisplayed()                   // intent / in-flight
        rule.onNodeWithText("Goa").assertIsDisplayed()                              // a slot
        shot("app_state")

        rule.onNodeWithText("Trace").performClick()
        rule.onNodeWithText("tool_cancel").assertIsDisplayed()
        shot("app_trace")

        rule.onNodeWithText("Graph").performClick()
        rule.onNodeWithText("The graph appears after the first completed turn.").assertIsDisplayed()
        rig.receive(a("graph_update", """"op":"append","nodes":[{"id":"t1","node_type":"turn","label":"find flights","data":{}},{"id":"e1","node_type":"entity","label":"destination: Goa","data":{}}],"edges":[{"source":"t1","target":"e1","edge_type":"REFERENCES"}]"""))
        rule.waitForIdle()
        rule.onNodeWithText("destination: Goa").assertIsDisplayed()
        shot("app_graph")
    }

    private fun form(server: String, onSave: (Settings) -> Unit = {}, onCancel: () -> Unit = {}) {
        rule.setContent {
            AgentTheme {
                androidx.compose.foundation.layout.Box(androidx.compose.ui.Modifier.padding(16.dp)) {
                    SettingsForm(Settings(serverUrl = server, token = "tok", sessionId = "sess_1"), onSave, onCancel)
                }
            }
        }
    }

    @Test fun settingsFormSavesTrimmedValuesAndStaysQuietForLocalAddresses() {
        var saved: Settings? = null
        form("ws://10.0.2.2:8000", onSave = { saved = it })
        rule.onNodeWithText("Connection").assertIsDisplayed()
        rule.onNodeWithText("This address is not encrypted", substring = true).assertDoesNotExist()   // the emulator host is fine
        shot("settings_form")
        rule.onNodeWithText("Save & reconnect").performClick()
        assertEquals(Settings(serverUrl = "ws://10.0.2.2:8000", token = "tok", sessionId = "sess_1"), saved)
    }

    @Test fun anInsecureRemoteAddressShowsTheWarningAsYouType() {
        form("ws://192.168.1.5:8000")
        rule.onNodeWithText("This address is not encrypted", substring = true).assertIsDisplayed()
    }

    @Test fun cancelDoesNotSave() {
        var saved = false
        var cancelled = false
        form("ws://10.0.2.2:8000", onSave = { saved = true }, onCancel = { cancelled = true })
        rule.onNodeWithText("Cancel").performClick()
        assertTrue(cancelled && !saved)
    }

    // Not tested here: opening the Settings DIALOG window. Robolectric's idle loop never settles on a Compose Dialog window
    // (AppNotIdleException), so the form is tested on its own above and the save path in AgentControllerTest.applySettings.

    @Test fun refusedConnectionsTellYouWhereToLook() {
        val (_, rig) = app()
        rig.transport.state.value = ConnState.Refused("refused by the server (HTTP 403)")
        rule.waitForIdle()
        rule.onNodeWithText("refused — check Settings", substring = true).assertIsDisplayed()
    }
}
