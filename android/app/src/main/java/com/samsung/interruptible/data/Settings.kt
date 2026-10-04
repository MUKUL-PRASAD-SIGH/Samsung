package com.samsung.interruptible.data

import android.content.Context
import com.samsung.interruptible.BuildConfig

data class Settings(
    val serverUrl: String = BuildConfig.DEFAULT_SERVER_URL,
    val token: String = "",
    val sessionId: String = "",
    val speakReplies: Boolean = false,
)

/** Where the user's connection settings live. An interface so the controller is testable without Android. */
interface SettingsStore {
    fun load(): Settings

    fun save(settings: Settings)

    /** False on a fresh install, before the user has told the app where its server is. */
    fun hasSavedServer(): Boolean = true
}

class SharedPrefsSettingsStore(context: Context) : SettingsStore {
    private val prefs = context.applicationContext.getSharedPreferences("agent_settings", Context.MODE_PRIVATE)

    override fun load(): Settings {
        val saved = prefs.getString("session", null)
        val session = saved ?: Urls.newSessionId().also { prefs.edit().putString("session", it).apply() }
        return Settings(
            serverUrl = prefs.getString("server", null) ?: BuildConfig.DEFAULT_SERVER_URL,
            token = prefs.getString("token", "") ?: "",
            sessionId = session,
            speakReplies = prefs.getBoolean("speak", false),
        )
    }

    override fun hasSavedServer(): Boolean = prefs.contains("server")

    override fun save(settings: Settings) {
        prefs.edit()
            .putString("server", settings.serverUrl.trim())
            .putString("token", settings.token.trim())
            .putString("session", Urls.sanitizeSessionId(settings.sessionId))
            .putBoolean("speak", settings.speakReplies)
            .apply()
    }
}
