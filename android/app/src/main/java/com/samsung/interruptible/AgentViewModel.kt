package com.samsung.interruptible

import android.app.Application
import android.content.Context
import android.media.AudioManager
import androidx.lifecycle.AndroidViewModel
import com.samsung.interruptible.audio.AudioRecordMic
import com.samsung.interruptible.audio.AudioTrackSpeechOutput
import com.samsung.interruptible.data.OkHttpTransport
import com.samsung.interruptible.data.SharedPrefsSettingsStore
import com.samsung.interruptible.state.AgentController
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel

/** Owns the long-lived pieces so they survive rotation; everything with logic lives in [AgentController]. */
class AgentViewModel(app: Application) : AndroidViewModel(app) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    val controller = AgentController(
        transport = OkHttpTransport(scope),
        speech = AudioTrackSpeechOutput(scope),
        mic = AudioRecordMic(scope, app.getSystemService(Context.AUDIO_SERVICE) as AudioManager),
        store = SharedPrefsSettingsStore(app),
        scope = scope,
    ).also {
        it.start()
        it.connect()
    }

    override fun onCleared() {
        controller.release()
        scope.cancel()
    }
}
