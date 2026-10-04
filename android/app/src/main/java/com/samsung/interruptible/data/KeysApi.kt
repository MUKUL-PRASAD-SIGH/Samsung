package com.samsung.interruptible.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

/** Which LLM provider keys the paired server holds. Only masked hints ever come back; a key is never read back from the server. */
data class KeysStatus(
    val groqConfigured: Boolean = false,
    val groqHint: String = "",
    val openrouterConfigured: Boolean = false,
    val openrouterHint: String = "",
    /** "groq", "openrouter", "local" or "mock" (the offline demo mode). */
    val backend: String = "mock",
    val configured: Boolean = false,
)

sealed interface KeysResult {
    data class Saved(val status: KeysStatus, val warnings: List<String>) : KeysResult

    /** The server (or the provider behind it) refused a key; `field` is "groq" or "openrouter" when it is known which. */
    data class Rejected(val message: String, val field: String? = null) : KeysResult

    /** The server needs an access key and ours was not accepted. */
    data object Unauthorized : KeysResult

    /** This server predates the key endpoints (older Kairos build). */
    data object NotSupported : KeysResult

    data class Unreachable(val reason: String) : KeysResult
}

/** Reads and updates the provider API keys on the server this app is paired with (`GET` / `PUT /settings/keys`). */
interface KeysApi {
    suspend fun status(serverUrl: String, token: String): KeysStatus?

    /** A null key leaves that provider alone, an empty string removes it. */
    suspend fun save(serverUrl: String, token: String, groqKey: String?, openrouterKey: String?): KeysResult
}

class HttpKeysApi(
    // Checking a key with the provider can take a few seconds, so this is longer than the sign-in probe's budget.
    private val client: OkHttpClient = OkHttpClient.Builder().callTimeout(25, TimeUnit.SECONDS).build(),
) : KeysApi {
    private val json = Json { ignoreUnknownKeys = true }

    private fun request(serverUrl: String, token: String) =
        Request.Builder().url(Urls.httpBase(serverUrl) + "/settings/keys").apply {
            if (token.isNotBlank()) header("Authorization", "Bearer ${token.trim()}")
        }

    override suspend fun status(serverUrl: String, token: String): KeysStatus? = withContext(Dispatchers.IO) {
        try {
            client.newCall(request(serverUrl, token).get().build()).execute().use { r ->
                if (!r.isSuccessful) null else parseStatus(json.parseToJsonElement(r.body?.string().orEmpty()).jsonObject)
            }
        } catch (e: Exception) {
            null
        }
    }

    override suspend fun save(serverUrl: String, token: String, groqKey: String?, openrouterKey: String?): KeysResult =
        withContext(Dispatchers.IO) {
            val body = buildJsonObject {
                groqKey?.let { put("groq_api_key", it.trim()) }
                openrouterKey?.let { put("openrouter_api_key", it.trim()) }
            }.toString().toRequestBody("application/json".toMediaType())
            try {
                client.newCall(request(serverUrl, token).put(body).build()).execute().use { r ->
                    val text = r.body?.string().orEmpty()
                    val obj = runCatching { json.parseToJsonElement(text).jsonObject }.getOrNull()
                    when {
                        r.isSuccessful && obj != null -> KeysResult.Saved(
                            parseStatus(obj),
                            (obj["warnings"] as? JsonArray)?.mapNotNull { (it as? JsonPrimitive)?.contentOrNull }.orEmpty(),
                        )
                        r.code == 401 -> KeysResult.Unauthorized
                        r.code == 404 || r.code == 405 -> KeysResult.NotSupported
                        r.code in 400..499 -> KeysResult.Rejected(
                            (obj?.get("error") as? JsonPrimitive)?.contentOrNull ?: "The server did not accept that key.",
                            (obj?.get("field") as? JsonPrimitive)?.contentOrNull,
                        )
                        else -> KeysResult.Unreachable("server answered HTTP ${r.code}")
                    }
                }
            } catch (e: Exception) {
                KeysResult.Unreachable(e.message ?: e.javaClass.simpleName)
            }
        }

    private fun parseStatus(o: JsonObject): KeysStatus {
        fun provider(name: String) = o[name] as? JsonObject
        fun configured(p: JsonObject?) = (p?.get("configured") as? JsonPrimitive)?.booleanOrNull ?: false
        fun hint(p: JsonObject?) = (p?.get("hint") as? JsonPrimitive)?.contentOrNull.orEmpty()
        return KeysStatus(
            groqConfigured = configured(provider("groq")), groqHint = hint(provider("groq")),
            openrouterConfigured = configured(provider("openrouter")), openrouterHint = hint(provider("openrouter")),
            backend = (o["backend"] as? JsonPrimitive)?.contentOrNull ?: "mock",
            configured = (o["configured"] as? JsonPrimitive)?.booleanOrNull ?: false,
        )
    }
}
