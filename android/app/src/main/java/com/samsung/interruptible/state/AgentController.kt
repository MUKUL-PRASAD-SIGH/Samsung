package com.samsung.interruptible.state

import com.samsung.interruptible.audio.MicInput
import com.samsung.interruptible.audio.Pcm
import com.samsung.interruptible.audio.SpeechOutput
import com.samsung.interruptible.data.AgentAction
import com.samsung.interruptible.data.ConnState
import com.samsung.interruptible.data.Outgoing
import com.samsung.interruptible.data.ServerMessage
import com.samsung.interruptible.data.Settings
import com.samsung.interruptible.data.SettingsStore
import com.samsung.interruptible.data.Transport
import com.samsung.interruptible.data.Urls
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * Glue between the network, the UI state and the audio devices. All Android-specific pieces are injected as interfaces
 * ([Transport], [SpeechOutput], [MicInput], [SettingsStore]), so every rule below runs in a plain JVM unit test.
 *
 * The rules that make the conversation full duplex on this side:
 *  - `speech_state: ducked`  -> lower the volume at once (the user started talking);
 *  - `speech_state: stopped` -> flush the audio queue IMMEDIATELY (the user took the floor);
 *  - a typed message or the Stop button flushes locally without waiting for the round trip.
 */
class AgentController(
    private val transport: Transport,
    private val speech: SpeechOutput,
    private val mic: MicInput,
    private val store: SettingsStore,
    private val scope: CoroutineScope,
    private val nowMs: () -> Long = System::currentTimeMillis,
) {
    private val _state = MutableStateFlow(ChatState())
    val state: StateFlow<ChatState> = _state.asStateFlow()

    var settings: Settings = store.load()
        private set

    private var started = false

    fun start() {
        if (started) return
        started = true
        _state.update { it.copy(speakReplies = settings.speakReplies) }
        scope.launch { transport.incoming.collect { onServerMessage(it) } }
        scope.launch {
            transport.state.collect { conn ->
                _state.update { ChatReducer.connectionChanged(it, conn, nowMs()) }
                if (conn == ConnState.Connected) onConnected()
            }
        }
    }

    fun connect() {
        transport.connect(Urls.webSocketUrl(settings.serverUrl, settings.sessionId, settings.token))
    }

    fun disconnect() {
        stopHandsFree()
        speech.flush()
        transport.disconnect()
    }

    /** Applies new settings and reconnects (the old connection may have been refused with the previous token). */
    fun applySettings(new: Settings) {
        settings = new.copy(sessionId = Urls.sanitizeSessionId(new.sessionId))
        store.save(settings)
        _state.update { ChatReducer.setSpeakReplies(it, settings.speakReplies) }
        connect()
    }

    // ------------------------------------------------------------------------------------------------ user actions
    fun sendText(text: String): Boolean {
        val t = text.trim()
        if (t.isEmpty()) return false
        speech.flush()                                        // typing over the agent's voice: silence it before the server answers
        if (!transport.sendText(Outgoing.userText(t))) {
            _state.update { it.copy(error = "Not connected: your message was not sent.") }
            return false
        }
        _state.update { ChatReducer.userSent(it, t) }
        return true
    }

    /** The Stop button: cancel running work on the server and go quiet right now. */
    fun interrupt() {
        speech.flush()
        transport.sendText(Outgoing.interrupt())
    }

    /** Starts/stops hands-free listening. The caller must already hold RECORD_AUDIO. Returns false if the mic could not open. */
    fun setHandsFree(on: Boolean): Boolean {
        if (!on) {
            stopHandsFree()
            return true
        }
        if (_state.value.handsFree) return true
        if (!mic.start { frame -> transport.sendBinary(frame) }) {
            _state.update { it.copy(error = "Could not open the microphone.") }
            return false
        }
        transport.sendText(Outgoing.voiceStream("start"))
        _state.update { ChatReducer.setHandsFree(it, true) }
        return true
    }

    private fun stopHandsFree() {
        if (!_state.value.handsFree) return
        mic.stop()
        transport.sendText(Outgoing.voiceStream("stop"))
        _state.update { ChatReducer.setHandsFree(it, false) }
    }

    fun setSpeakReplies(on: Boolean) {
        settings = settings.copy(speakReplies = on)
        store.save(settings)
        if (!on) speech.flush()
        _state.update { ChatReducer.setSpeakReplies(it, on) }
        transport.sendText(Outgoing.tts(on))
    }

    fun setSharing(on: Boolean) {
        _state.update { ChatReducer.setSharing(it, on, nowMs()) }
    }

    /** One camera frame (already downscaled JPEG). Only sent while sharing and connected. */
    fun sendFrame(jpeg: ByteArray, width: Int, height: Int): Boolean {
        if (!_state.value.sharing) return false
        val ok = transport.sendText(Outgoing.videoFrame(Pcm.encodeBase64(jpeg), "camera", width, height))
        if (ok) _state.update { ChatReducer.frameSent(it, nowMs()) }
        return ok
    }

    fun dismissError() = _state.update { ChatReducer.dismissError(it) }

    // ------------------------------------------------------------------------------------------------ incoming
    private fun onConnected() {
        // The server keeps no preferences across connections: re-announce what this client wants.
        if (_state.value.speakReplies) transport.sendText(Outgoing.tts(true))
        if (_state.value.handsFree) transport.sendText(Outgoing.voiceStream("start"))
    }

    internal fun onServerMessage(msg: ServerMessage) {
        if (msg is ServerMessage.Action) {
            when (val a = msg.action) {
                is AgentAction.AudioOut -> if (_state.value.speakReplies) speech.play(Pcm.decodeBase64(a.audioB64), a.sampleRate)
                is AgentAction.SpeechState -> when (a.state) {
                    "ducked" -> speech.setDucked(true)
                    "resumed" -> speech.setDucked(false)
                    "stopped" -> speech.flush()
                    "finished" -> speech.setDucked(false)
                }
                else -> Unit
            }
        }
        _state.update { ChatReducer.reduce(it, msg, nowMs()) }
    }

    fun release() {
        stopHandsFree()
        speech.release()
        transport.disconnect()
    }
}
