package com.samsung.interruptible

import com.samsung.interruptible.data.Backoff
import com.samsung.interruptible.data.Urls
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class UrlsTest {
    @Test fun webSocketUrlNormalisesWhatPeopleType() {
        assertEquals("ws://10.0.2.2:8000/ws/s1", Urls.webSocketUrl("ws://10.0.2.2:8000", "s1"))
        assertEquals("ws://10.0.2.2:8000/ws/s1", Urls.webSocketUrl("http://10.0.2.2:8000/", "s1"))
        assertEquals("wss://agent.example.com/ws/s1", Urls.webSocketUrl("https://agent.example.com", "s1"))
        assertEquals("wss://agent.example.com/ws/s1", Urls.webSocketUrl("  wss://agent.example.com//  ", "s1"))
        assertEquals("ws://192.168.1.5:8000/ws/s1", Urls.webSocketUrl("192.168.1.5:8000", "s1"))
    }

    @Test fun tokenIsUrlEncodedAndOmittedWhenBlank() {
        assertEquals("ws://h/ws/s1?token=a%2Bb%26c%3Dd", Urls.webSocketUrl("ws://h", "s1", "a+b&c=d"))
        assertEquals("ws://h/ws/s1", Urls.webSocketUrl("ws://h", "s1", ""))
        assertEquals("ws://h/ws/s1", Urls.webSocketUrl("ws://h", "s1", "   "))
    }

    @Test fun aSessionIdTheServerWouldRefuseIsRepairedNotSent() {
        assertEquals("my_session_", Urls.sanitizeSessionId("my session!"))
        assertEquals("a_b", Urls.sanitizeSessionId("a/b"))
        assertTrue(Urls.isValidSessionId(Urls.sanitizeSessionId("../../etc/passwd")))
        assertTrue(Urls.isValidSessionId(Urls.sanitizeSessionId("x".repeat(200))))
        assertEquals(64, Urls.sanitizeSessionId("x".repeat(200)).length)
        assertTrue(Urls.isValidSessionId(Urls.sanitizeSessionId("")))          // blank -> a fresh valid id
        assertTrue("ws://h/ws/" + Urls.sanitizeSessionId("é ü") == Urls.webSocketUrl("ws://h", "é ü"))
    }

    @Test fun generatedSessionIdsAreValidAndDistinct() {
        val ids = (1..50).map { Urls.newSessionId() }.toSet()
        assertEquals(50, ids.size)
        assertTrue(ids.all(Urls::isValidSessionId))
    }

    @Test fun insecureRemoteDetection() {
        assertFalse(Urls.isInsecureRemote("ws://10.0.2.2:8000"))        // emulator -> host
        assertFalse(Urls.isInsecureRemote("ws://localhost:8000"))
        assertFalse(Urls.isInsecureRemote("http://127.0.0.1:8000"))
        assertFalse(Urls.isInsecureRemote("wss://agent.example.com"))
        assertFalse(Urls.isInsecureRemote("https://agent.example.com"))
        assertTrue(Urls.isInsecureRemote("ws://192.168.1.5:8000"))
        assertTrue(Urls.isInsecureRemote("http://agent.example.com"))
        assertTrue(Urls.isInsecureRemote("192.168.1.5:8000"))
    }

    @Test fun backoffDoublesCapsAndResets() {
        val b = Backoff(baseMs = 500, maxMs = 8_000)
        assertEquals(listOf(500L, 1000L, 2000L, 4000L, 8000L, 8000L, 8000L), (1..7).map { b.nextDelayMs() })
        b.reset()
        assertEquals(500L, b.nextDelayMs())
    }

    @Test fun backoffJitterNeverExceedsTheCapAndDoesNotOverflow() {
        val b = Backoff(baseMs = 500, maxMs = 8_000, jitter = { it })     // worst case: jitter doubles every delay
        repeat(100) { assertTrue(b.nextDelayMs() <= 8_000L) }
    }
}
