package com.samsung.interruptible.data

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString.Companion.toByteString
import java.util.concurrent.TimeUnit

sealed interface ConnState {
    data object Disconnected : ConnState

    data object Connecting : ConnState

    data object Connected : ConnState

    /** The server refused us (bad token, wrong origin, session cap...). Not retried until the settings change. */
    data class Refused(val reason: String) : ConnState

    /** Connection lost; a retry is scheduled. */
    data class Retrying(val inMs: Long, val reason: String) : ConnState
}

/** The link to the server. An interface so the controller can be tested without a network. */
interface Transport {
    val state: StateFlow<ConnState>
    val incoming: SharedFlow<ServerMessage>

    fun connect(url: String)

    fun disconnect()

    /** False when there is no open connection (the frame is dropped, never queued: stale input must not replay later). */
    fun sendText(frame: String): Boolean

    fun sendBinary(bytes: ByteArray): Boolean
}

class OkHttpTransport(
    private val scope: CoroutineScope,
    private val client: OkHttpClient = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)      // notice a dead connection instead of waiting for TCP to time out
        .readTimeout(0, TimeUnit.MILLISECONDS)   // a WebSocket is idle between actions by design
        .build(),
    private val backoff: Backoff = Backoff(jitter = { (it * 0.2 * Math.random()).toLong() }),
) : Transport {
    private val _state = MutableStateFlow<ConnState>(ConnState.Disconnected)
    override val state: StateFlow<ConnState> = _state.asStateFlow()

    private val _incoming = MutableSharedFlow<ServerMessage>(extraBufferCapacity = 512, onBufferOverflow = BufferOverflow.DROP_OLDEST)
    override val incoming: SharedFlow<ServerMessage> = _incoming.asSharedFlow()

    @Volatile private var socket: WebSocket? = null
    @Volatile private var wantedUrl: String? = null
    private var retryJob: Job? = null
    private var generation = 0   // ignores callbacks from a socket we already replaced

    @Synchronized
    override fun connect(url: String) {
        retryJob?.cancel()
        wantedUrl = url
        backoff.reset()
        open(url)
    }

    @Synchronized
    private fun open(url: String) {
        socket?.cancel()
        val gen = ++generation
        _state.value = ConnState.Connecting
        socket = client.newWebSocket(Request.Builder().url(url).build(), Listener(gen))
    }

    @Synchronized
    override fun disconnect() {
        wantedUrl = null
        retryJob?.cancel()
        generation++
        socket?.close(1000, "client closing")
        socket = null
        _state.value = ConnState.Disconnected
    }

    override fun sendText(frame: String): Boolean = _state.value == ConnState.Connected && socket?.send(frame) == true

    override fun sendBinary(bytes: ByteArray): Boolean =
        _state.value == ConnState.Connected && socket?.send(bytes.toByteString()) == true

    @Synchronized
    private fun lost(gen: Int, reason: String, refused: Boolean) {
        if (gen != generation) return
        val url = wantedUrl
        socket = null
        if (url == null) {
            _state.value = ConnState.Disconnected
            return
        }
        if (refused) {
            wantedUrl = null
            _state.value = ConnState.Refused(reason)
            return
        }
        val delayMs = backoff.nextDelayMs()
        _state.value = ConnState.Retrying(delayMs, reason)
        retryJob = scope.launch {
            delay(delayMs)
            synchronized(this@OkHttpTransport) { if (wantedUrl == url) open(url) }
        }
    }

    private inner class Listener(private val gen: Int) : WebSocketListener() {
        private var finished = false    // closing, closed and failure can all fire for one socket: handle the loss once

        private fun end(reason: String, refused: Boolean) {
            synchronized(this) {
                if (finished) return
                finished = true
            }
            lost(gen, reason, refused)
        }

        override fun onOpen(webSocket: WebSocket, response: Response) {
            if (gen != generation) return
            backoff.reset()
            _state.value = ConnState.Connected
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            if (gen != generation) return
            Protocol.parse(text)?.let { _incoming.tryEmit(it) }
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            // The SERVER started closing. OkHttp only completes the handshake (and ever calls onClosed) once we answer; a
            // client that doesn't would sit "Connected" on a dead socket and never reconnect.
            webSocket.close(code, reason)
            // 1008 = policy violation: the server refuses this client (auth/origin), so retrying cannot help.
            end("closed by server ($code) $reason".trim(), refused = code == 1008)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            end("closed ($code) $reason".trim(), refused = code == 1008)
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            val code = response?.code
            val refused = code == 401 || code == 403
            val why = when {
                refused -> "refused by the server (HTTP $code): check the access token and allowed origins"
                code != null -> "server answered HTTP $code"
                else -> t.message ?: t.javaClass.simpleName
            }
            end(why, refused)
        }
    }
}
