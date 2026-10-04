package com.samsung.interruptible.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.jsonObject
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

/** What the sign-in check found out about the server. */
sealed interface AuthProbe {
    /** The server needs no key. */
    data object Open : AuthProbe

    /** A key is required and the one we have (if any) was not given or is wrong. */
    data class NeedsKey(val hadKey: Boolean) : AuthProbe

    /** A key is required and ours is valid. */
    data object Valid : AuthProbe

    /** Could not reach the server at all (offline, wrong address). The app still opens and shows its reconnect state. */
    data class Unreachable(val reason: String) : AuthProbe
}

/** Asks the server whether it needs a key and whether ours works, over plain HTTP (the same checks the web login screen does). */
interface AuthApi {
    suspend fun probe(serverUrl: String, token: String): AuthProbe
}

class HttpAuthApi(
    private val client: OkHttpClient = OkHttpClient.Builder().callTimeout(6, TimeUnit.SECONDS).build(),
) : AuthApi {
    override suspend fun probe(serverUrl: String, token: String): AuthProbe = withContext(Dispatchers.IO) {
        val base = Urls.httpBase(serverUrl)
        try {
            val health = client.newCall(Request.Builder().url("$base/health").build()).execute().use { r ->
                if (!r.isSuccessful) return@withContext AuthProbe.Unreachable("server answered HTTP ${r.code}")
                Json.parseToJsonElement(r.body?.string().orEmpty()).jsonObject
            }
            val required = (health["auth_required"] as? JsonPrimitive)?.booleanOrNull ?: false
            if (!required) return@withContext AuthProbe.Open
            if (token.isBlank()) return@withContext AuthProbe.NeedsKey(hadKey = false)
            val check = Request.Builder().url("$base/auth/check").header("Authorization", "Bearer ${token.trim()}").build()
            client.newCall(check).execute().use { r ->
                if (r.isSuccessful) AuthProbe.Valid else AuthProbe.NeedsKey(hadKey = true)
            }
        } catch (e: Exception) {
            AuthProbe.Unreachable(e.message ?: e.javaClass.simpleName)
        }
    }
}
