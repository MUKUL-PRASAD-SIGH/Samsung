package com.samsung.interruptible

import com.samsung.interruptible.audio.MicInput
import com.samsung.interruptible.data.AuthApi
import com.samsung.interruptible.data.AuthProbe
import com.samsung.interruptible.audio.SpeechOutput
import com.samsung.interruptible.data.ConnState
import com.samsung.interruptible.data.ServerMessage
import com.samsung.interruptible.data.Settings
import com.samsung.interruptible.data.SettingsStore
import com.samsung.interruptible.data.Transport
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject

// Test doubles for the Android-specific edges (network, speaker, microphone, settings), shared by the controller and UI tests.
class FakeTransport : Transport {
    override val state = MutableStateFlow<ConnState>(ConnState.Disconnected)
    val incomingFlow = MutableSharedFlow<ServerMessage>(extraBufferCapacity = 64)
    override val incoming: SharedFlow<ServerMessage> = incomingFlow
    val sentText = mutableListOf<String>()
    val sentBinary = mutableListOf<ByteArray>()
    var connectedTo: String? = null
    var open = true

    override fun connect(url: String) { connectedTo = url; state.value = ConnState.Connected }
    override fun disconnect() { state.value = ConnState.Disconnected }
    override fun sendText(frame: String): Boolean = if (open) { sentText += frame; true } else false
    override fun sendBinary(bytes: ByteArray): Boolean = if (open) { sentBinary += bytes; true } else false
    fun sentTypes() = sentText.map { (Json.parseToJsonElement(it).jsonObject["type"] as JsonPrimitive).content }
}

class FakeSpeech : SpeechOutput {
    val events = mutableListOf<String>()
    val played = mutableListOf<Pair<Int, Int>>()
    override fun play(pcm: ByteArray, sampleRate: Int) { events += "play"; played += pcm.size to sampleRate }
    override fun flush() { events += "flush" }
    override fun setDucked(ducked: Boolean) { events += "duck=$ducked" }
    override fun release() { events += "release" }
}

class FakeMic(private val opens: Boolean = true) : MicInput {
    var onFrame: ((ByteArray) -> Unit)? = null
    var running = false
    override fun start(onFrame: (ByteArray) -> Unit): Boolean { if (!opens) return false; this.onFrame = onFrame; running = true; return true }
    override fun stop() { running = false }
}

class FakeStore(var settings: Settings = Settings(serverUrl = "ws://10.0.2.2:8000", sessionId = "sess_1")) : SettingsStore {
    override fun load() = settings
    override fun save(settings: Settings) { this.settings = settings }
}


/** Scripted server answers for the sign-in check. `probe` records what it was asked. */
class FakeAuthApi(var answer: (server: String, token: String) -> AuthProbe) : AuthApi {
    val asked = mutableListOf<Pair<String, String>>()
    override suspend fun probe(serverUrl: String, token: String): AuthProbe {
        asked += serverUrl to token
        return answer(serverUrl, token)
    }
}
