package com.samsung.interruptible

import android.Manifest
import android.app.Application
import androidx.activity.ComponentActivity
import androidx.compose.ui.test.SemanticsNodeInteraction
import androidx.compose.ui.test.hasSetTextAction
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextClearance
import androidx.compose.ui.test.performTextInput
import androidx.test.core.app.ApplicationProvider
import com.samsung.interruptible.data.AuthProbe
import com.samsung.interruptible.data.Protocol
import com.samsung.interruptible.data.Settings
import com.samsung.interruptible.state.AgentController
import com.samsung.interruptible.ui.AgentApp
import com.samsung.interruptible.ui.AgentTheme
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assume.assumeTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File
import java.io.FileOutputStream

/**
 * Renders the frames for the phone section of the demo video. NOT a normal test: it is skipped unless demo-data/wsraw.json
 * exists (written by scripts/demo/record.py). The UI is the real AgentApp and AgentController, driven through the real
 * composables (typing, tapping); only the network is faked: the server's answers are the REAL frames recorded from a live
 * session of the web client, replayed in order. Output: app/build/demo/NN_name.png and app/build/demo/plan.json.
 */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [34], qualifiers = "w411dp-h891dp-xxhdpi")
class DemoFrames {
    @get:Rule val rule = createAndroidComposeRule<ComponentActivity>()

    private val dataDir = File(System.getenv("DEMO_DATA") ?: "../demo-data")
    private val enabled = System.getenv("DEMO_DATA") != null          // opt-in: a normal test run must not render video frames
    private val out = File("build/demo")
    private val plan = StringBuilder()
    private var n = 0
    private var pendingTap: Pair<Float, Float>? = null
    private val key = "athena-kairos-2026"

    private fun load(name: String): List<Pair<String, String>> {
        val f = File(dataDir, name)
        if (!f.exists()) return emptyList()
        return Json.parseToJsonElement(f.readText()).jsonObject["wsraw"]!!.jsonArray.map {
            val o = it.jsonObject; o["dir"]!!.jsonPrimitive.content to o["data"]!!.jsonPrimitive.content
        }
    }
    private fun typeOf(json: String) = (Json.parseToJsonElement(json) as JsonObject).let { (it["action_type"] ?: it["type"])?.jsonPrimitive?.content ?: "" }
    private fun textOf(json: String) = (Json.parseToJsonElement(json) as JsonObject)["text"]?.jsonPrimitive?.content ?: ""

    private fun shot(name: String, caption: String = "", bullet: Int? = null, hold: Double = 2.0) {
        rule.waitForIdle()
        val view = rule.activity.window.decorView
        val bitmap = android.graphics.Bitmap.createBitmap(view.width.coerceAtLeast(1), view.height.coerceAtLeast(1), android.graphics.Bitmap.Config.ARGB_8888)
        view.draw(android.graphics.Canvas(bitmap))
        val file = "%02d_%s.png".format(n++, name)
        FileOutputStream(File(out, file)).use { bitmap.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, it) }
        val tap = pendingTap?.let { "[${it.first},${it.second}]" } ?: "null"
        pendingTap = null
        plan.append("""{"img":"$file","tap":$tap,"cap":${JsonPrimitive(caption)},"bullet":${bullet ?: "null"},"hold":$hold},""" + "\n")
    }

    private fun tap(node: SemanticsNodeInteraction) {
        val b = node.fetchSemanticsNode().boundsInRoot
        val v = rule.activity.window.decorView
        pendingTap = ((b.left + b.right) / 2 / v.width) to ((b.top + b.bottom) / 2 / v.height)
        node.performClick()
        rule.waitForIdle()
    }

    private class Rig(auth: (String, String) -> AuthProbe) {
        val scope = TestScope(UnconfinedTestDispatcher())
        val transport = FakeTransport()
        val controller = AgentController(transport, FakeSpeech(), FakeMic(), FakeStore(Settings(serverUrl = "wss://kairos.example.com", sessionId = "sess_demo")),
            scope.backgroundScope, nowMs = { 1_760_000_000_000L }, authApi = FakeAuthApi(auth)).also { it.start() }
        fun receive(json: String) { transport.incomingFlow.tryEmit(Protocol.parse(json)!!) }
    }

    /** The server's answers to the user message that starts with [prefix] (frames up to the next thing the client sent). */
    private fun answers(all: List<Pair<String, String>>, prefix: String): List<String> {
        val start = all.indexOfFirst { it.first == "out" && textOf(it.second).startsWith(prefix) }
        if (start < 0) return emptyList()
        return all.drop(start + 1).takeWhile { it.first == "in" || typeOf(it.second) == "interrupt" && false }.map { it.second }
            .filter { typeOf(it) !in setOf("audio_out", "tts_status") }
    }

    private fun feedThrough(rig: Rig, frames: List<String>, from: Int, untilType: String): Int {
        var i = from
        while (i < frames.size) { val t = typeOf(frames[i]); rig.receive(frames[i]); i++; if (t == untilType) break }
        rule.waitForIdle()
        return i
    }

    private fun send(text: String) {
        rule.onNodeWithText("Ask, or interrupt…").performTextInput(text)
        shot("typing", "")
        tap(rule.onNodeWithContentDescription("Send"))
    }

    @Test fun renderThePhoneSection() {
        assumeTrue("set DEMO_DATA to render the demo frames", enabled)
        val all = load("wsraw.json")
        assumeTrue("no recorded session in ${dataDir.absolutePath}", all.isNotEmpty())
        out.deleteRecursively(); out.mkdirs()
        val rig = Rig { _, token -> if (token == key) AuthProbe.Valid else AuthProbe.NeedsKey(false) }
        rule.setContent { AgentTheme { AgentApp(rig.controller) } }
        rig.controller.begin()
        rule.waitForIdle()

        // ---- sign in
        shot("login", "Same sign-in as the desktop: the server's access key", 0, 2.6)
        val keyField = { rule.onAllNodes(hasSetTextAction())[1] }
        keyField().performTextInput("kairos-demo"); shot("login_wrong_typed", "", 0, 1.4)
        tap(rule.onNodeWithText("Continue")); shot("login_wrong", "A wrong key is refused", 0, 2.2)
        keyField().performTextClearance(); keyField().performTextInput(key.take(8)); shot("login_typing", "", 0, 1.0)
        keyField().performTextInput(key.drop(8)); shot("login_typed", "", 0, 1.4)
        tap(rule.onNodeWithText("Continue")); rule.waitForIdle()
        shot("home", "Signed in: type, speak or tap a suggestion", 1, 2.8)

        // ---- flights
        val flights = answers(all, "Find flights from Delhi to Mumbai")
        send("Find flights from Delhi to Mumbai"); shot("sent", "", 1, 0.8)
        var i = feedThrough(rig, flights, 0, "filler"); shot("ack", "Instant acknowledgement while the planner works", 1, 1.6)
        i = feedThrough(rig, flights, i, "spoken_response"); feedThrough(rig, flights, i, "graph_update"); shot("flights", "The answer, rendered as it arrived", 1, 2.4)

        // ---- correction
        val book = answers(all, "Book a flight from Delhi to Mumbai"); val fix = answers(all, "No wait, change it to Goa")
        send("Book a flight from Delhi to Mumbai"); feedThrough(rig, book, 0, "tool_call"); shot("booking", "Booking…", 2, 1.2)
        send("No wait, change it to Goa")
        var j = feedThrough(rig, fix, 0, "tool_cancel"); shot("cancelled", "…corrected mid-flight: the stale call is cancelled", 2, 2.4)
        j = feedThrough(rig, fix, j, "spoken_response"); feedThrough(rig, fix, j, "graph_update"); shot("corrected", "", 2, 2.2)

        // ---- the agent's own view
        tap(rule.onNodeWithText("State")); shot("state", "State: the agent's slots and epoch", 3, 2.4)
        tap(rule.onNodeWithText("Trace")); shot("trace", "Trace of every event", 3, 2.4)
        tap(rule.onNodeWithText("Graph")); shot("graph", "Cognitive graph: what it remembers", 3, 2.4)
        tap(rule.onNodeWithText("Chat"))

        // ---- code export
        val code = answers(all, "Write a Python function")
        send("Write a Python function that checks whether a number is prime. Save it as prime.py and open it in VS Code.")
        var k = feedThrough(rig, code, 0, "file_exported"); shot("artifact", "The worker's code, as an artifact", 4, 2.4)
        k = feedThrough(rig, code, k, "spoken_response"); feedThrough(rig, code, k, "graph_update")
        try { rule.onNodeWithText("Copy path").performScrollTo() } catch (e: Throwable) { println("no scroll: ${e.message}") }
        shot("exported", "Exported to disk: Download or copy the path", 4, 3.4)

        // ---- hands-free voice (frames recorded from a live voice session, if present)
        val voice = load("wsraw_voice.json")
        if (voice.isNotEmpty()) {
            shadowOf(ApplicationProvider.getApplicationContext<Application>()).grantPermissions(Manifest.permission.RECORD_AUDIO)
            val vs = voice.indexOfFirst { it.first == "out" && typeOf(it.second) == "voice_stream" }
            val frames = voice.drop(vs + 1).filter { it.first == "in" }.map { it.second }.filter { typeOf(it) !in setOf("audio_out", "tts_status") }
            tap(rule.onNodeWithContentDescription("Hands-free voice")); shot("listening", "Hands-free voice: just talk", 5, 2.2)
            var v = 0
            v = feedThrough(rig, frames, v, "voice_activity"); shot("voice_hearing", "Hearing you, with a live transcript…", 5, 2.0)
            v = feedThrough(rig, frames, v, "transcript"); v = feedThrough(rig, frames, v, "transcript"); shot("voice_partial", "", 5, 2.0)
            v = feedThrough(rig, frames, v, "tool_call"); shot("voice_booking", "…it starts booking…", 5, 1.8)
            v = feedThrough(rig, frames, v, "tool_cancel"); shot("voice_cancel", "…and your spoken correction cancels it mid-flight", 5, 3.0)
            v = feedThrough(rig, frames, v, "spoken_response"); shot("voice_done", "", 5, 2.4)
        }
        File(out, "plan.json").writeText("[" + plan.toString().trimEnd().trimEnd(',') + "]")
    }
}
