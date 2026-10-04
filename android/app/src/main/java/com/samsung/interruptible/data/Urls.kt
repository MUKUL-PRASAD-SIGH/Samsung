package com.samsung.interruptible.data

import java.net.URLEncoder
import java.util.UUID

/** Building the WebSocket address the way the server expects it (see agent/security.py on the server). */
object Urls {
    private val sessionPattern = Regex("^[A-Za-z0-9_-]{1,64}$")

    /** A fresh session id the server will accept. */
    fun newSessionId(): String = "android_" + UUID.randomUUID().toString().replace("-", "").take(12)

    fun isValidSessionId(id: String): Boolean = sessionPattern.matches(id)

    /** Keeps what is valid in `raw` and falls back to a new id: a pasted "my session!" must not become a refused handshake. */
    fun sanitizeSessionId(raw: String): String {
        val cleaned = raw.trim().replace(Regex("[^A-Za-z0-9_-]"), "_").take(64)
        return if (cleaned.isEmpty()) newSessionId() else cleaned
    }

    /**
     * `http://host:8000`, `https://host`, `ws://host`, bare `host:8000` all become a `ws(s)://.../ws/{session}` address;
     * the access token (if any) goes in `?token=`, which the server accepts for WebSockets.
     */
    fun webSocketUrl(server: String, sessionId: String, token: String? = null): String {
        var base = server.trim().trimEnd('/')
        base = when {
            base.startsWith("https://") -> "wss://" + base.removePrefix("https://")
            base.startsWith("http://") -> "ws://" + base.removePrefix("http://")
            base.startsWith("ws://") || base.startsWith("wss://") -> base
            else -> "ws://$base"
        }
        val query = token?.takeIf { it.isNotBlank() }?.let { "?token=" + URLEncoder.encode(it, "UTF-8") }.orEmpty()
        return "$base/ws/${sanitizeSessionId(sessionId)}$query"
    }

    /** The HTTP(S) address of the same server, for plain requests (`/health`, `/auth/check`, `/exports/...`). */
    fun httpBase(server: String): String {
        val base = server.trim().trimEnd('/')
        return when {
            base.startsWith("wss://") -> "https://" + base.removePrefix("wss://")
            base.startsWith("ws://") -> "http://" + base.removePrefix("ws://")
            base.startsWith("http://") || base.startsWith("https://") -> base
            else -> "http://$base"
        }
    }

    /** True when traffic (and the access token) would cross a network unencrypted. Loopback and emulator addresses are fine. */
    fun isInsecureRemote(server: String): Boolean {
        val s = server.trim().lowercase()
        if (s.startsWith("wss://") || s.startsWith("https://")) return false
        val host = s.removePrefix("ws://").removePrefix("http://").substringBefore('/').substringBefore(':')
        return host !in setOf("localhost", "127.0.0.1", "10.0.2.2", "10.0.3.2", "[::1]")
    }
}

/** Reconnect delays: 0.5 s, 1 s, 2 s ... capped, with optional jitter so a restarted server isn't hit by every client at once. */
class Backoff(private val baseMs: Long = 500, private val maxMs: Long = 8_000, private val jitter: (Long) -> Long = { 0L }) {
    private var attempt = 0

    fun nextDelayMs(): Long {
        val d = minOf(maxMs, baseMs shl minOf(attempt, 20))
        attempt++
        return minOf(maxMs, d + jitter(d))
    }

    fun reset() {
        attempt = 0
    }
}
