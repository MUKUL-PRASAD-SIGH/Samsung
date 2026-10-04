package com.samsung.interruptible.audio

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.util.Base64

/** The agent's voice. An interface so the controller is testable without a speaker. */
interface SpeechOutput {
    /** Queue one sentence of PCM16 mono audio for playback after whatever is already queued. */
    fun play(pcm: ByteArray, sampleRate: Int)

    /** Silence NOW and forget everything queued (the user took the floor). */
    fun flush()

    /** Lower the volume while we find out whether the user is really interrupting. */
    fun setDucked(ducked: Boolean)

    fun release()
}

/** The user's voice: 16 kHz mono PCM16 frames of [Pcm.FRAME_BYTES] each (100 ms), as the server expects. */
interface MicInput {
    /** False if the microphone could not be opened (no permission, in use). */
    fun start(onFrame: (ByteArray) -> Unit): Boolean

    fun stop()
}

object Pcm {
    const val MIC_RATE = 16_000
    const val FRAME_BYTES = 3_200          // 100 ms of 16 kHz PCM16
    const val DUCK_VOLUME = 0.15f

    fun decodeBase64(b64: String): ByteArray = try {
        Base64.getDecoder().decode(b64)
    } catch (e: IllegalArgumentException) {
        ByteArray(0)
    }

    fun encodeBase64(bytes: ByteArray): String = Base64.getEncoder().encodeToString(bytes)

    fun durationMs(bytes: Int, sampleRate: Int): Double = bytes / 2.0 / sampleRate * 1000.0
}

/**
 * Plays the agent's replies through an [AudioTrack] declared as VOICE_COMMUNICATION. That declaration is what lets the
 * platform echo canceller use our own playback as the reference signal, so the microphone does not hear the agent.
 */
class AudioTrackSpeechOutput(private val scope: CoroutineScope) : SpeechOutput {
    private class Chunk(val pcm: ByteArray, val rate: Int, val generation: Int)

    private val queue = Channel<Chunk>(Channel.UNLIMITED)
    @Volatile private var generation = 0
    @Volatile private var ducked = false
    private var track: AudioTrack? = null
    private var trackRate = 0
    private val writer: Job = scope.launch(Dispatchers.IO) {
        for (chunk in queue) {
            if (chunk.generation == generation) write(chunk)
        }
    }

    override fun play(pcm: ByteArray, sampleRate: Int) {
        if (pcm.isNotEmpty()) queue.trySend(Chunk(pcm, sampleRate, generation))
    }

    override fun flush() {
        generation++                                  // chunks queued before now are skipped by the writer
        val t = track ?: return
        try {
            t.pause()
            t.flush()                                 // discards what is already buffered in the audio pipeline
        } catch (_: IllegalStateException) {
        }
    }

    override fun setDucked(ducked: Boolean) {
        this.ducked = ducked
        track?.setVolume(if (ducked) Pcm.DUCK_VOLUME else 1f)
    }

    override fun release() {
        writer.cancel()
        queue.close()
        track?.release()
        track = null
    }

    private fun write(chunk: Chunk) {
        val t = ensureTrack(chunk.rate)
        t.setVolume(if (ducked) Pcm.DUCK_VOLUME else 1f)
        if (t.playState != AudioTrack.PLAYSTATE_PLAYING) t.play()
        var offset = 0
        while (offset < chunk.pcm.size && scope.isActive && chunk.generation == generation) {
            val n = t.write(chunk.pcm, offset, minOf(4096, chunk.pcm.size - offset))   // small slices: a flush lands within ~100 ms
            if (n <= 0) break
            offset += n
        }
    }

    private fun ensureTrack(rate: Int): AudioTrack {
        track?.let { if (trackRate == rate) return it else it.release() }
        val minBuf = AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val t = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build(),
            )
            .setAudioFormat(
                AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT).setSampleRate(rate)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build(),
            )
            .setBufferSizeInBytes(maxOf(minBuf, rate / 5 * 2))      // ~200 ms: little audio to throw away on a flush
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        track = t
        trackRate = rate
        return t
    }
}

/** Captures the microphone as VOICE_COMMUNICATION with the platform's echo canceller, noise suppressor and AGC attached. */
class AudioRecordMic(private val scope: CoroutineScope, private val audioManager: AudioManager?) : MicInput {
    private var record: AudioRecord? = null
    private var job: Job? = null
    private val effects = mutableListOf<android.media.audiofx.AudioEffect>()
    private var previousMode: Int? = null

    @SuppressWarnings("MissingPermission")   // the UI requests RECORD_AUDIO before calling start()
    override fun start(onFrame: (ByteArray) -> Unit): Boolean {
        if (job != null) return true
        val minBuf = AudioRecord.getMinBufferSize(Pcm.MIC_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = try {
            AudioRecord(
                MediaRecorder.AudioSource.VOICE_COMMUNICATION, Pcm.MIC_RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, Pcm.FRAME_BYTES * 4),
            )
        } catch (e: SecurityException) {
            return false
        }
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            rec.release()
            return false
        }
        previousMode = audioManager?.mode
        audioManager?.mode = AudioManager.MODE_IN_COMMUNICATION      // routes playback+capture through the voice-call echo path
        if (AcousticEchoCanceler.isAvailable()) AcousticEchoCanceler.create(rec.audioSessionId)?.also { it.enabled = true; effects += it }
        if (NoiseSuppressor.isAvailable()) NoiseSuppressor.create(rec.audioSessionId)?.also { it.enabled = true; effects += it }
        if (AutomaticGainControl.isAvailable()) AutomaticGainControl.create(rec.audioSessionId)?.also { it.enabled = true; effects += it }
        rec.startRecording()
        record = rec
        job = scope.launch(Dispatchers.IO) {
            val buf = ByteArray(Pcm.FRAME_BYTES)
            while (isActive) {
                var filled = 0
                while (filled < buf.size && isActive) {
                    val n = rec.read(buf, filled, buf.size - filled)
                    if (n <= 0) return@launch
                    filled += n
                }
                if (filled == buf.size) onFrame(buf.copyOf())
            }
        }
        return true
    }

    override fun stop() {
        job?.cancel()
        job = null
        effects.forEach { it.release() }
        effects.clear()
        record?.let {
            try {
                it.stop()
            } catch (_: IllegalStateException) {
            }
            it.release()
        }
        record = null
        previousMode?.let { audioManager?.mode = it }
        previousMode = null
    }
}
