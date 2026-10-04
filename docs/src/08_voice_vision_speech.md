# Voice, Speech and Vision

This part of Kairos is everything that touches a microphone, a speaker or a camera frame. It is what turns the text-first agent into a full-duplex one: the user can talk while the agent is talking, the agent hears the user while they are still mid-sentence, and a spoken reply that is cut off is remembered as the part the user actually heard. The code lives in `agent/multimodal/streaming.py` (voice activity detection and utterance segmentation), `agent/multimodal/asr.py` (Whisper), `agent/multimodal/tts.py` (text to speech backends), `agent/speech.py` (the per-session speaker, echo guard and truncation record) and `agent/multimodal/vision.py` (frame analysis with free-model failover). The glue that connects them to the epoch model lives in `agent/coordinator.py` (the `_handle_voice_stream`, `_voice_partial`, `_voice_final`, `_maybe_voice_barge_in`, `_handle_video_frame` and `_run_vision` methods) and in the WebSocket handler in `agent/server.py`.

Read this chapter together with the coordinator and epoch chapters: voice never decides anything on its own. It produces text events and stop signals, and the same epoch machinery that cancels stale tool calls is what stops the agent talking.

## 1. The big picture

```
 Browser / Android                           Server (agent/)
 -----------------                           ---------------
 mic -> AEC/NS/AGC                           server.py  /ws handler
   -> 16 kHz mono PCM16                        |  binary frame + voice_stream active
   -> 100 ms frames (1600 samples)  --bin-->   v
                                             coordinator._handle_voice_stream
                                               |  VoiceStream.feed()  (Silero VAD, 32 ms frames)
                                               v
                                   SpeechStart -> voice_activity + duck()
                                   PartialDue  -> Whisper (thread) -> transcript(partial)
                                                   |-> echo guard / talk-over check -> speaker.stop
                                                   |-> adaptive endpoint hint
                                                   |-> barge-in classifier -> bump_epoch
                                   UtteranceEnd-> Whisper or reused partial -> transcript(final)
                                                   -> UserTextEvent(immediate=True) -> planner
                                             coordinator.emit_action(reply)
 speaker <-- audio_out (base64 PCM16) <---     SessionSpeaker (speech.py) -> TTS backend
 speaker <-- speech_state started/ducked/      (paced to real time, stops on epoch change)
             resumed/stopped/finished
 camera -> JPEG 1 fps --video_frame JSON-->    coordinator._frames ring buffer (3 frames)
                                               analyze_frame tool -> OpenRouter free models
```

Three design decisions run through the whole chapter:

1. Detection happens on the server, not in the browser. The client only captures and plays audio. That keeps the web UI and the Android app thin clients of the same protocol, and it lets the eval harness and the tests drive the voice path with plain bytes.
2. Whisper never runs on the event loop. All transcription goes through `asyncio.to_thread`, and the per-frame work in the coordinator is only the cheap VAD step. A stalled loop would delay exactly the interrupts the feature exists to react to.
3. Speaking is subordinate to the epoch. A spoken reply belongs to the epoch it was planned under and stops within a polling interval of that epoch changing.

## 2. Browser to server audio format and WebSocket messages

All voice, speech and vision traffic uses the single `/ws` WebSocket handled in `agent/server.py`. The handler distinguishes binary frames from text (JSON) frames.

### 2.1 Client to server

| Message | Shape | Meaning |
|---|---|---|
| binary, while streaming is on | raw 16 kHz mono PCM16 little-endian bytes, any chunk size | Continuous hands-free audio. Turned into `AudioChunkEvent(format="pcm_16khz", streaming=True)` |
| binary, streaming off | one complete WebM recording | Legacy push-to-talk. `AudioChunkEvent(format="webm", is_final=True)` |
| text `{"type":"voice_stream","action":"start"}` | JSON | Starts streaming mode; creates or resets the session voice runtime; server answers with a `voice_activity` action `listening` |
| text `{"type":"voice_stream","action":"stop"}` | JSON | Flushes any open utterance, drops the runtime, server emits `voice_activity` `idle` |
| text `{"type":"tts","enabled":true}` | JSON | Opt in or out of spoken replies. Answered with `{"type":"tts_status","enabled":...,"available":...}` |
| text `{"type":"interrupt","reason":"ui_barge_in"}` | JSON | Explicit stop button; becomes `InterruptSignalEvent` |
| text `{"type":"video_frame","mime":"image/jpeg","data":"<base64>","source":"camera|screen","width":..,"height":..}` | JSON | A shared camera or screen frame |
| text `{"type":"audio_chunk","audio_base64":"...","format":"webm","is_final":true}` | JSON | Older base64 variant of push-to-talk; goes through the same `AudioChunkEvent` path |

The web client (`frontend/src/App.jsx`) captures with `getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true}})` and opens an `AudioContext({sampleRate: 16000})`. An `AudioWorklet` (the inline `PCM_WORKLET_SOURCE`, a processor named `pcm-capture`) linearly resamples to 16 kHz if the browser ignored the requested rate and posts `Int16Array(1600)` buffers, which is 100 ms per WebSocket message. The worklet is connected to a muted gain node so it keeps running without playing the microphone back. If `ws.bufferedAmount` exceeds 1,000,000 bytes the client drops frames instead of queueing them (stale audio is useless). The Android client (`android/app/src/main/java/com/samsung/interruptible/audio/Audio.kt`) does the same with `AudioRecord` using the voice communication source with AEC, noise suppression and AGC, again as 100 ms PCM frames (per `android/README.md`).

The server does not require a fixed chunk size: `VoiceStream.feed` accumulates a remainder and cuts 512-sample (1024-byte) frames itself, and a test (`test_chunking_does_not_change_results` in `tests/test_voice_stream.py`) asserts that chunking does not change results.

Server-side guards on the binary path in `agent/server.py`: a per-connection `TokenBucket` for audio (`cfg.audio_bytes_per_s`, `cfg.audio_burst_bytes` from `agent/settings.py`) answers `{"type":"error","code":"rate_limit"}` when exceeded and disconnects after 50 violations; a single binary frame larger than `MAX_BINARY_FRAME_BYTES` (10 MiB, roughly five minutes of 16 kHz PCM16) is dropped with a warning. If the client disconnects while streaming, the `finally` block posts a synthetic `stream_control="stop"` event so an open utterance is closed and the voice runtime is freed. A binary frame that arrives without a preceding `voice_stream` start is interpreted as a legacy WebM recording (the handler keys purely on its local `voice_streaming` flag), and audio posted to the coordinator without a runtime is ignored (`test_audio_without_a_start_control_is_ignored`).

### 2.2 Server to client

All server messages are serialized pydantic actions (`agent/schemas/actions.py`) sent as JSON text, with the exception of the small `tts_status` and `error` dictionaries above.

| Action | Fields that matter here | Emitted when |
|---|---|---|
| `transcript` (`TranscriptAction`) | `text`, `is_partial`, `utterance_id`, `asr_model`, `latency_ms` | A partial (during speech) or final transcript; for push-to-talk, one final transcript, with empty `text` meaning no speech was detected |
| `voice_activity` (`VoiceActivityAction`) | `state`: `listening`, `speech_start`, `speech_end`, `barge_in`, `idle`; `utterance_id`; `detail` | Stream lifecycle. On `speech_end`, `detail` is the endpoint reason; on `barge_in`, it is the partial text that triggered it |
| `audio_out` (`AudioOutAction`) | `utterance_id`, `seq`, `text`, `sample_rate`, `duration_ms`, `audio_b64`, `is_last` | One sentence of synthesized speech |
| `speech_state` (`SpeechStateAction`) | `state`: `started`, `ducked`, `resumed`, `finished`, `stopped`; `reason`, `text`, `spoken_text`, `spoken_ms` | Lifecycle of one spoken reply |
| `tts_status` | `enabled`, `available` | Reply to the `tts` message |

`audio_b64` is raw PCM16 mono at `sample_rate` (not WAV and not compressed). `agent/trace_logger.py` redacts `audio_b64` in the trace, replacing it with a size marker, so recordings of sessions do not carry hundreds of kilobytes of base64 (`test_audio_is_redacted_in_the_trace_not_logged_as_megabytes_of_base64`).

On the client the player is `frontend/src/hooks/useSpeechPlayer.js`. It schedules each `audio_out` sentence back to back on a WebAudio clock, drops the gain to `DUCK_GAIN = 0.15` on `ducked`, restores it on `resumed`, and stops every scheduled source immediately on `stopped`. Output goes through the page's own WebAudio graph, which is what lets the browser echo canceller subtract the agent's voice from the microphone signal. The client re-sends `{"type":"tts","enabled":true}` whenever the socket (re)connects because the server keeps no preference across sessions.

Protocol contract note: the Android app parses fixtures generated from these same pydantic classes (see `scripts/dump_android_fixtures.py`, `tests/test_android_fixtures.py`), so changing any field above means updating the fixtures, `Protocol.kt` and `ChatReducer.kt`.

## 3. Streaming Silero VAD and utterance segmentation (`agent/multimodal/streaming.py`)

### Purpose

Push-to-talk can only react after the user has finished the whole sentence. Streaming lets the server hear the user while they speak: it detects speech onset, requests a partial transcript on a cadence, and ends the utterance automatically after a pause. The module is deliberately model-light and clock-free: time is measured in audio frames, not wall clock, so the segmentation logic is deterministic and unit-testable with a scripted VAD (`ScriptedVAD` in `tests/test_voice_stream.py`).

### How it works

Constants: `SAMPLE_RATE = 16000`, `FRAME_SAMPLES = 512` (the frame size Silero expects at 16 kHz), so `FRAME_MS = 32`.

1. `make_vad()` builds a `SileroStreamingVAD`. That class reuses the ONNX session of faster-whisper's Silero model (`faster_whisper.vad.get_vad_model().session`) but drives it directly, carrying the LSTM state (`_h`, `_c`) and a 64-sample context between calls. This is the reason the class exists: faster-whisper's own wrapper zeroes the recurrent state on every call, which is fine for a whole file and wrong for a live stream. If Silero or onnxruntime cannot be loaded, `make_vad()` logs a warning and returns `EnergyVAD`, a crude RMS threshold (0.012) that returns 1.0 or 0.0.
2. `VoiceStream.feed(pcm)` prepends the stored remainder, cuts whole 1024-byte frames and calls `_process_frame` for each. Each frame is converted to float32 in [-1, 1] and scored by the VAD.
3. While not speaking, a frame at or above `speech_threshold` increments `_speech_run`, anything lower resets it. Every frame is also pushed into a rolling pre-roll deque whose size is `preroll_ms` plus the onset frames. When `_speech_run` reaches `min_speech_ms` worth of frames (rounded to 4 frames at the default 128 ms) `_begin_utterance` runs: it assigns an id `utt_N`, seeds the utterance with the pre-roll (so the first word is not clipped) and emits `SpeechStart`. A short blip never reaches the minimum and is ignored (`test_short_blip_is_ignored`).
4. While speaking, frames are appended to the utterance. Hysteresis applies: a frame at or above the lower `silence_threshold` resets the silence run and counts as speech, only frames below it add to `_silence_run`. A mid-probability dip therefore does not cut the utterance (`test_hysteresis_keeps_utterance_alive_through_mid_probability_dips`).
5. When `_silence_run` reaches the endpoint frame count, `_end_utterance("endpoint")` emits `UtteranceEnd`. The trailing silence is trimmed but a 4-frame tail (about 128 ms) is kept so the last word is not clipped by an aggressive VAD. Hitting `max_utterance_s` emits an end with reason `max_length` and speech continues as a new utterance (`test_max_length_forces_an_endpoint_and_speech_continues_as_new_utterance`). `flush()` ends an open utterance with reason `flush`.
6. Partials. Every `partial_interval_ms` of speech, if at least `min_partial_ms` of audio exists, `PartialDue(utterance_id, pcm)` is emitted carrying all audio so far. In addition, the first time the silence run equals `tail_partial_ms` (provided that is shorter than the endpoint wait and enough audio exists), a `PartialDue(..., is_tail=True)` is emitted. The tail partial is taken after the speaker went quiet, so it covers all the speech and its text can stand in for the final transcript; ordinary cadence partials may stop mid-word.

Events are plain dataclasses (`SpeechStart`, `PartialDue`, `UtteranceEnd`) returned from `feed` and `flush`; the stream never calls back or sleeps.

### Adaptive endpointing

`endpoint_hint_ms(partials, base_ms)` implements the latency lever from the spec. It looks at the partial transcripts of the current utterance:

- If the last partial ends with a comma or its last word is a continuation word (`and or but to from for at in on with the a an of by then also plus into toward towards via after before between next my your our that this those these is are was`), the speaker is probably mid-thought: wait longer, `min(1000, base * 1.6)` ms. This is what stops "book a flight to ... Goa" from being cut at the pause.
- If the last two partials are equal after normalisation and read as complete (at least 3 words or terminal punctuation), the transcript has stopped changing: end sooner, `max(300, base * 0.6)` ms.
- Otherwise the base value is kept.

The coordinator applies it in `_voice_partial` through `VoiceStream.set_endpoint_ms`, only if the utterance is still live and `VOICE_ADAPTIVE_ENDPOINT` is not 0. The override lasts for the current utterance only; `_end_utterance` restores `cfg.endpoint_ms`.

### Echo resistance inside the VAD

`set_speech_threshold(threshold)` temporarily raises the onset bar (never below the configured value). The coordinator calls it from `_on_speaking_changed` with `SPEAKING_VAD_THRESHOLD` (default 0.75) while the agent is talking, and with `None` to restore it afterwards. The comment in the coordinator calls this the second line of defence behind the browser's acoustic echo canceller (`test_vad_bar_is_raised_while_speaking_and_restored` in `tests/test_speech.py`).

### Key types and functions

| Name | File | Role |
|---|---|---|
| `VoiceConfig` | `agent/multimodal/streaming.py` | Dataclass of all thresholds and timings; `from_env()` reads the `VOICE_*` variables |
| `VoiceStream` | `agent/multimodal/streaming.py` | Frame cutter and utterance state machine: `feed`, `flush`, `reset`, `set_speech_threshold`, `set_endpoint_ms` |
| `SileroStreamingVAD` | `agent/multimodal/streaming.py` | Silero ONNX driven frame by frame with carried LSTM state |
| `EnergyVAD` | `agent/multimodal/streaming.py` | RMS fallback when Silero cannot load |
| `make_vad` | `agent/multimodal/streaming.py` | Chooses Silero, falls back to energy |
| `endpoint_hint_ms` | `agent/multimodal/streaming.py` | Adaptive endpointing rule |
| `SpeechStart`, `PartialDue`, `UtteranceEnd` | `agent/multimodal/streaming.py` | Event dataclasses returned by the stream |

### Configuration

Defaults below are the dataclass defaults in `VoiceConfig`; each is overridden by the environment variable shown.

| Env var | Default | Effect |
|---|---|---|
| `VOICE_VAD_THRESHOLD` | 0.5 | Probability at or above which a frame counts as speech |
| `VOICE_VAD_SILENCE_THRESHOLD` | 0.35 | Hysteresis: once speaking, only frames below this count as silence |
| `VOICE_MIN_SPEECH_MS` | 128 | Sustained speech required to start an utterance |
| `VOICE_ENDPOINT_MS` | 500 | Silence that ends an utterance (adapted per utterance) |
| `VOICE_PREROLL_MS` | 320 | Audio kept from before onset |
| `VOICE_MAX_UTTERANCE_S` | 20 | Hard cap on one utterance |
| `VOICE_PARTIAL_INTERVAL_MS` | 500 | Cadence of partial-transcript requests |
| `VOICE_MIN_PARTIAL_MS` | 600 | Minimum audio before a partial is requested |
| `VOICE_TAIL_PARTIAL_MS` | 200 | Silence after which the "everything so far" tail partial fires; 0 disables |
| `VOICE_ADAPTIVE_ENDPOINT` | 1 | Coordinator switch for `endpoint_hint_ms` |
| `VOICE_SPEAKING_VAD_THRESHOLD` | 0.75 | VAD bar while the agent is speaking |
| `VOICE_FINAL_REUSE_SLACK_MS` | 100 | See the final-transcript reuse rule below |

### Failure modes and guarantees

- Missing Silero or onnxruntime degrades to the energy VAD rather than failing; segmentation still works, but noise is much more likely to open an utterance.
- A non-numeric value in a `VOICE_*` variable silently falls back to the default (`_env_float`).
- The stream is per session and in memory; a stop or a disconnect flushes an open utterance so audio spoken just before the stop is not lost (`test_stop_flushes_an_utterance_still_in_progress`).

### Worked example

The user says "book a flight to Goa" with a short pause before "Goa", at the default config. Frames 1 to 4 (128 ms) score above 0.5: `SpeechStart("utt_1")` is emitted, and the coordinator emits `voice_activity speech_start` and, if the agent is speaking, calls `speaker.duck()`. At about 600 ms and then every 500 ms `PartialDue` fires; the first partial reads "book a flight to". It ends with the continuation word "to", so `endpoint_hint_ms` returns 800 ms (500 x 1.6) and the pause before "Goa" does not end the utterance. The next partial reads "book a flight to Goa"; when the user stops, the tail partial fires 200 ms into the silence and returns the same text. Now the last two partials agree and have at least 3 words, so the endpoint drops to 300 ms; `UtteranceEnd` arrives with reason `endpoint` and the final reuses the tail partial text.

### Tests

`tests/test_voice_stream.py` (scripted-VAD segmentation tests, plus real Silero and Whisper tests that skip when the models are unavailable offline), `tests/test_voice_websocket.py` (PCM over the real WebSocket yields activity, partials and a final; binary without `voice_stream start` is still legacy WebM; a disconnect mid-stream frees the runtime).

## 4. Whisper ASR (`agent/multimodal/asr.py`)

### Purpose

Turn bytes of audio into text, with low first-call latency and without ever blocking the event loop.

### How it works

`ASRProcessor` wraps `faster_whisper.WhisperModel`. The model is loaded lazily on the first transcription in `_ensure_model_loaded` using double-checked locking behind a `threading.Lock`. If the import or load fails, `_load_failed` is set once, a warning is logged, and every later call returns an empty string. `transcribe_audio_bytes(audio_bytes, format)` handles two input kinds:

- `format="pcm_16khz"` (the streaming path): drop an odd trailing byte, convert int16 to float32 divided by 32768 and hand the array to Whisper directly (no container decoding).
- Anything else (`wav`, `webm`, and so on): wrap the bytes in `io.BytesIO` and let faster-whisper decode the container via PyAV.

Inference uses `beam_size=1` (fastest), `condition_on_previous_text=False`, the Silero `vad_filter` (on by default, `min_silence_duration_ms=400`) to discard silence, and an `initial_prompt` for domain biasing. The default prompt (`DEFAULT_INITIAL_PROMPT`) lists Indian city names and airport codes plus words like "flights, hotels, weather, build a component in TypeScript"; the module docstring explains this fixes mishearings such as "Goa" becoming "go away" (`test_domain_prompt_fixes_proper_noun`). The returned segments are a lazy generator, so they are consumed while the lock is still held; the same lock serialises inference on the single shared model across worker threads. Any exception during transcription is logged and yields an empty string. `warmup()` loads the model and transcribes 0.5 seconds of silence so the first real call does not pay load latency, and `info()` reports model, device, compute type, `loaded` and `load_failed` (surfaced through `/health` as `asr`).

### Key types and functions

| Name | File | Role |
|---|---|---|
| `ASRProcessor` | `agent/multimodal/asr.py` | Lazy, thread-safe faster-whisper wrapper |
| `ASRProcessor.transcribe_audio_bytes` | `agent/multimodal/asr.py` | PCM or container bytes to text; empty string on any problem |
| `ASRProcessor.warmup` / `info` | `agent/multimodal/asr.py` | Pre-load and status |
| `DEFAULT_INITIAL_PROMPT` | `agent/multimodal/asr.py` | Domain vocabulary prompt |

### Configuration

| Env var | Default | Effect |
|---|---|---|
| `WHISPER_MODEL` | `base.en` | faster-whisper model size or name |
| `WHISPER_DEVICE` | `cpu` | Inference device |
| `WHISPER_COMPUTE_TYPE` | `int8` | Quantisation |
| `WHISPER_INITIAL_PROMPT` | the travel/code prompt above | Vocabulary bias; an empty string disables it |

### Interactions

The coordinator owns one `ASRProcessor` (`asr_processor or ASRProcessor()`). Streaming partials and finals call it with `"pcm_16khz"`; push-to-talk calls it from `_handle_audio_chunk` with the container format and emits one `transcript` action followed by a `UserTextEvent`, optionally through the debounce queue. `agent/warmup.py` warms ASR (and separately the Silero VAD session and the TTS voice) at boot, and `POST /warmup` reruns it.

### Failure modes and guarantees

If the model cannot be loaded (no extras, offline with no cache) the whole voice input path yields empty transcripts: the UI shows "no speech detected" for push-to-talk and streaming emits nothing, but the server stays up and typed input is unaffected (`test_unloadable_model_degrades_gracefully`). Transcription is serialised, so a long final can delay the next partial; the coordinator therefore skips a partial tick while one is still in flight.

### Tests

`tests/test_asr_real.py` (container formats, raw PCM, domain prompt, silence, thread safety, `info`, graceful degradation, and a coordinator run with real ASR; skipped when the model is not cached).

## 5. Partial transcripts, finals and barge-in cancellation (coordinator side)

This logic is in `agent/coordinator.py`, but it is the point of the voice stack, so it is described here.

### Purpose

React to the user mid-sentence: stop the agent talking, and cancel stale work as soon as a partial transcript already reads as a correction, rather than a second after the user finishes.

### How it works

`_handle_voice_stream` reacts to the control and audio events. On `start` it creates a `_VoiceRuntime` (holding the `VoiceStream`, the in-flight partial task, an ordered tail of final tasks and per-utterance bookkeeping) or resets the existing stream, then emits `voice_activity listening`. On `stop` it dispatches `flush()`, deletes the runtime (in-flight finals continue via their own task references) and emits `idle`. Audio is fed to `feed`, and `_dispatch_voice_events` handles each event:

1. `SpeechStart`: emit `voice_activity speech_start`. If the session has a speaker, call `speaker.duck()` (emits `speech_state ducked`, the client lowers volume) or, if `VOICE_STOP_ON_SPEECH_START=1` and the speaker is speaking, stop it at once with reason `user_speech_start`.
2. `PartialDue`: start a `_voice_partial` task only if no partial task is running (one partial at a time; skipped ticks are not queued).
3. `UtteranceEnd`: mark the utterance finalized so any partial still running for it is discarded, emit `voice_activity speech_end` with the endpoint reason in `detail`, and start a `_voice_final` task chained behind the previous one so utterances reach the planner in order.

`_voice_partial` transcribes in a thread, then:

1. Drops empty text or text for an already finalized utterance.
2. If the session has a speaker, runs the echo guard (section 7). An echo is dropped silently and is never a barge-in.
3. Computes "talk-over" evidence while the agent is speaking. If the Tier 1 classifier (`_classify`) says the text is anything other than `continue` (an interrupt word like "no wait" or "stop"), the evidence jumps to 2. Otherwise, if the text is substantial (at least 10 letters in skeleton form), evidence increments for each consecutive substantial partial of this utterance, and a non-substantial one resets it. At evidence 2 the speaker is stopped with reason `user_spoke`. The effect: one stray, possibly garbled fragment cannot stop the agent, but a user who really talks over it produces a partial every 0.5 s and does.
4. Emits a partial `transcript` action, appends to the utterance's partial history and, for tail partials, records `(pcm length, text)` in `last_partial`.
5. Applies the adaptive endpoint hint.
6. Calls `_maybe_voice_barge_in`.

`_maybe_voice_barge_in` fires at most once per utterance, only when some tool call is `pending` or `running`, and only if the classifier flags the partial as an interrupt. It then bumps the epoch (`session.bump_epoch(reason="voice_barge_in: ...")`), emits the resulting cancellation actions, a `voice_activity barge_in` action with the text in `detail`, and a fresh state snapshot. It also seeds `_reaction` so that the cancel-latency metric is measured from the partial that revealed the barge-in.

`_voice_final` waits for the previous final, then decides the text. If a tail partial exists and it covered all but `VOICE_FINAL_REUSE_SLACK_MS` (default 100 ms, computed as bytes at 32 bytes per ms) of the audio, its text is reused and Whisper is not run a second time; this removes the bulk of end-of-speech latency. The slack is kept small on purpose: the code comment notes the last word lives in the last few hundred milliseconds ("Goa" versus "go"). Otherwise a full transcription runs. It emits the final `transcript`, then handles the speaker: if the text is an echo, log, `resume()` the volume and return without any user event; if there is real text, `stop("user_spoke")`; if there is no text, `resume()`. Real text is posted as `UserTextEvent(text, barge_in_handled=<fired>, immediate=True)`. `barge_in_handled` tells `_handle_user_text` not to bump the epoch a second time.

Independent of voice, any non-empty `UserTextEvent` (typed or spoken) stops speech in `post_event`, an interrupt event stops it in `_handle_interrupt` (reason `interrupt`), and `_handle_user_text` stops it again (reason `user_spoke`).

### Key types and functions

| Name | File | Role |
|---|---|---|
| `_VoiceRuntime` | `agent/coordinator.py` | Per-session streaming state (stream, partial task, final tail, histories, `barge_in_fired`, `finalized`, `talkover`, `last_partial`) |
| `_handle_voice_stream` / `_dispatch_voice_events` | `agent/coordinator.py` | Control handling and event fan-out |
| `_voice_partial` / `_voice_final` | `agent/coordinator.py` | Transcription tasks, echo guard, talk-over logic, final reuse |
| `_maybe_voice_barge_in` | `agent/coordinator.py` | Early epoch bump from a partial |
| `_on_speaking_changed` | `agent/coordinator.py` | Raises the VAD bar while speaking |

### Configuration

`VOICE_STOP_ON_SPEECH_START` (default `0`, duck then stop on the first real partial; `1` stops the instant the VAD hears speech, fastest but any noise cuts a reply short), `VOICE_ADAPTIVE_ENDPOINT` (1), `VOICE_FINAL_REUSE_SLACK_MS` (100), `VOICE_SPEAKING_VAD_THRESHOLD` (0.75). Cadence settings are in section 3.

### Failure modes and guarantees

- A barge-in is only fired with work in flight, so a "wait" with nothing running goes through the normal final-transcript path, which bumps the epoch itself.
- Finals are serialised, so two quick utterances never reach the planner out of order, and an exception in an earlier final does not block the next.
- A final that completes after the user has stopped streaming still posts its event, because the task holds its own references.
- Voice scenarios are not part of the CI eval gate; tests that need real audio and models skip in virtual time (see `CLAUDE.md`).

### Worked example

The agent is searching flights to Boston and speaking an acknowledgement. The user says "no wait, make it Chicago". The first speech frames emit `SpeechStart`; the speaker is ducked (`speech_state ducked`, client gain 0.15). About 0.6 s later the first partial, "no wait make it", arrives. It is not an echo, `_classify` returns a non-`continue` decision, so evidence is 2 and `speaker.stop("user_spoke")` runs. In the same call `_maybe_voice_barge_in` sees a running search call and an interrupt classification, bumps the epoch, emits `tool_cancel` for the search, `voice_activity barge_in` and a snapshot. When the user finishes, the final reuses the tail partial, is posted as `UserTextEvent(barge_in_handled=True)`, and the planner starts a Chicago search under the new epoch without bumping again.

### Tests

`tests/test_voice_stream.py` (`test_streaming_emits_activity_partials_and_a_final_that_reaches_the_planner`, `test_voice_barge_in_cancels_running_work_mid_sentence_and_bumps_epoch_once`, `test_non_interrupting_speech_does_not_cancel_running_work`, `test_stop_flushes_an_utterance_still_in_progress`), `tests/test_speech.py` (`test_a_mangled_fragment_does_not_stop_the_agent_but_real_words_and_interrupts_do`, `test_sustained_non_echo_speech_stops_the_agent`), `tests/test_voice_websocket.py`.

## 6. Text to speech and the SessionSpeaker (`agent/multimodal/tts.py`, `agent/speech.py`)

### Purpose

Speak the agent's replies out loud, sentence by sentence, in a way that keeps the agent interruptible: stop quickly when the user takes the floor, and remember how much was actually heard.

### TTS backends (`agent/multimodal/tts.py`)

`TTSBackend` is an abstract class with `synthesize(text) -> TTSAudio` (PCM16 little-endian mono plus `sample_rate`, with a `duration_s` property) and a `cheap` flag. Backends mirror the LLM and vision backend pattern:

- `PiperTTSBackend`: local Piper ONNX voice on CPU. The voice file defaults to `models/piper/en_US-lessac-medium.onnx` (`DEFAULT_VOICE`) or `TTS_VOICE_PATH`. Loading is lazy and a lock serialises synthesis because Piper is not documented as thread-safe. The module docstring quotes a real-time factor of about 0.04, so a sentence is ready in about 0.1 s on the author's machine; that figure is a claim in the source, not something measured in this chapter.
- `MockTTSBackend`: deterministic; returns silence of `seconds_per_word` (0.35) times the word count at 16 kHz, with an optional blocking delay. With no delay it sets `cheap = True`, so the speaker runs it inline rather than in a worker thread, which keeps it deterministic under the virtual-time event loop. Note: the class docstring says "55 ms of silence per character-ish", which does not match the code; the implementation is per word at 0.35 s.
- `get_tts_backend()` reads `TTS_BACKEND` (`auto` default, `piper`, `mock`, `off`/`none`/`0`/`false`). `auto` selects Piper only if `piper_available()` (the `piper` package imports and the voice file exists), else returns `None`. `piper` explicitly requested but missing also returns `None` with a warning (the warning prints `DEFAULT_VOICE` even when `TTS_VOICE_PATH` is set).

`split_sentences(text, max_chars=220)` strips Markdown emphasis characters and bullets (formatting for the eye, not words to speak), splits on sentence-ending punctuation followed by whitespace or on newlines, and breaks overlong sentences at a comma or space so no single chunk delays the first audio. `spoken_prefix(sentences, durations_s, played_s)` returns the words spoken after `played_s` seconds: whole finished sentences plus a proportional share (by word count) of the one in progress; sentences with no audio yet contribute nothing.

### The speaker (`agent/speech.py`)

One `SessionSpeaker` exists per session that opted in (`AgentCoordinator.set_tts`). `enabled` is False until the client sends the `tts` message. The coordinator's `emit_action` hands every `filler`, `spoken_response` and `clarification` action to the speaker via `enqueue(text, epoch)`; text is taken from `text`, falling back to `question`.

How a reply is spoken (`_run` and `_speak`):

1. `_run` pops queued items. An item whose epoch is below the session epoch is skipped: a reply planned for an epoch the user already moved past is never voiced (`test_a_reply_from_a_past_epoch_is_never_spoken`).
2. `_speak` assigns an utterance id `tts_N`, splits into sentences and emits `speech_state started` (with the full text).
3. For each sentence: after the first, it sleeps (in `poll_s` steps) until the playhead is within `lookahead_s` of running dry, so synthesis is paced to real time. It checks for a stop, synthesizes (inline for a `cheap` backend, else `asyncio.to_thread`), checks again because the user may have spoken during synthesis, then emits `audio_out` with `seq`, the sentence text, rate, duration, base64 PCM and `is_last`. It records the sentence in a timeline used by the echo guard.
4. After the last sentence it waits for it to finish playing, records `_last_spoke_at` and emits `speech_state finished` with `spoken_text` equal to the full text and `spoken_ms`.

Pacing matters for two reasons stated in the module docstring: the server's estimate of how much has been heard stays accurate (it assumes playback started when the first sentence was emitted and runs in real time), and a cut-off wastes almost no synthesis.

Stopping. `stop(reason)` clears the queue and, if speaking, records the reason and wakes the sleeper. `_check(epoch)` is called at every step and raises `_Stop` if a stop reason is set or `session.epoch != epoch`. The wait loop wakes every 25 ms (`POLL_S`), which is how speech ends well inside the 150 ms target after an epoch bump. On `_Stop`, the handler computes `played = min(now - t_start, cum)`, builds `spoken = spoken_prefix(...)`, emits `speech_state stopped` with `reason`, the full `text`, `spoken_text` and `spoken_ms`, records the stop latency (`stop_latencies_s` and the `metrics.SPEECH_STOP` histogram), clears the queue and, if the spoken text differs from the full text, calls `on_truncated`. The `finally` block resets `ducked` and recomputes the speaking flag, which calls `on_activity` so the coordinator restores the VAD threshold.

Ducking. `duck()` (called on speech start) emits `ducked` once while speaking; `resume()` emits `resumed` once (reason `false_alarm`) when the "speech" turned out to be noise or the agent's own echo. The client implements the actual volume change; the server only announces it.

### The spoken_text truncation record

When a reply is cut off, the speaker calls the coordinator's `_record_truncation`, which calls `graph_memory.mark_response_truncated(full_text, spoken_text)` in `agent/memory/graph_memory.py`. That method finds the most recent turn whose `agent_response` equals the full text and has no `spoken_text` yet, and sets `spoken_text`. When the next prompt context is built (`get_subgraph_prompt_context`), such a turn is shown to the model as the heard part followed by "[interrupted by the user; the rest was never heard]", or "[interrupted by the user before this was heard]" if nothing was spoken. The point: the planner must not assume the user received the information they were cut off before hearing (for example a booking ID in the last sentence). The match is by exact text equality, so if the reply text is changed between planning and speaking the record would not attach; the code returns False in that case and does nothing.

Truthfulness is not enforced in this layer. The speaker only renders replies the planner already emitted, and those claim completion only after the matching tool result; the speaker never speaks ahead of that.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `TTSBackend`, `PiperTTSBackend`, `MockTTSBackend` | `agent/multimodal/tts.py` | Synthesis backends |
| `get_tts_backend`, `piper_available` | `agent/multimodal/tts.py` | Backend selection from `TTS_BACKEND` |
| `split_sentences`, `spoken_prefix` | `agent/multimodal/tts.py` | Chunking and the truncation calculation |
| `SessionSpeaker` | `agent/speech.py` | Queue, pacing, stop, duck/resume, echo guard, timeline |
| `AudioOutAction`, `SpeechStateAction` | `agent/schemas/actions.py` | Wire format of speech |
| `set_tts`, `_record_truncation`, `_stop_speech` | `agent/coordinator.py` | Opt-in, memory record, stop entry point |
| `mark_response_truncated` | `agent/memory/graph_memory.py` | Writes `spoken_text` onto the turn node |

### Configuration

| Env var or constant | Default | Effect |
|---|---|---|
| `TTS_BACKEND` | `auto` | `auto`, `piper`, `mock`, `off` |
| `TTS_VOICE_PATH` | `models/piper/en_US-lessac-medium.onnx` | Piper voice file |
| `LOOKAHEAD_S` | 0.6 | Audio kept buffered ahead of the playhead (constructor argument, not an env var) |
| `POLL_S` | 0.025 | How quickly a stop or epoch change is noticed (constructor argument) |

### Failure modes and guarantees

- With no TTS backend, `set_tts` returns False, the server answers `tts_status` with `available: false`, the UI hides the speaker toggle, and nothing is ever spoken (`test_no_backend_means_no_speech_and_the_toggle_reports_it`). `/health` reports `tts.available` and the backend name.
- Replies are only voiced for sessions that opted in (`test_replies_are_spoken_only_after_the_client_opts_in`).
- A stale-epoch reply is never voiced. An epoch change, a typed message, an interrupt or a user utterance all stop speech and clear the queue.
- Synthesis already running in a worker thread when a stop arrives is not interrupted; its result is simply discarded at the next `_check`, so no stale sentence is emitted.
- `close()` (session eviction, shutdown) cancels the runner and disables the speaker.
- The eval harness checks the stop latency (`tests/test_speech_eval.py`, scenarios `speak_barge_in` and `speak_plain` per the README) and includes mutation tests proving that an agent which keeps talking, or one that forgets what was heard, is caught.

### Worked example

The reply is "Your flight is confirmed. The booking ID is F L 9 8 2 1 4." (two sentences). The speaker emits `started`, synthesizes sentence 1 (say 1.6 s of audio) and emits `audio_out seq=0`. For sentence 2 it sleeps until the playhead is 0.6 s from running dry, then emits `audio_out seq=1 is_last=true`. After 2.2 s of total playback the user says "stop" and the epoch bumps. Within 25 ms `_check` raises `_Stop("epoch_changed")`. `played` is 2.2 s, `spoken_prefix` returns sentence 1 plus a proportional part of sentence 2 (words in sentence 2 times the share of its duration already played), `stopped` is emitted with that `spoken_text`, the client flushes its scheduled sources, and the graph memory turn gets the `spoken_text`. On the next turn the planner sees the interrupted marker and does not assume the user heard the whole ID.

### Tests

`tests/test_speech.py` (sentence splitting, `spoken_prefix`, pacing, stale epoch, 150 ms stop, truncation record, duck/resume, echo guard, backend opt-in, trace redaction; a Piper-specific class that skips without Piper), `tests/test_speech_eval.py`, `tests/test_speech_loopback.py` (real Piper into real Whisper, skipped without Piper and models).

## 7. The echo guard (`SessionSpeaker.is_echo`, `skeleton`, `is_substantial`)

### Purpose

If the user has no headphones, the microphone hears the agent. Whisper then transcribes the agent's own voice, and without a guard the agent would "interrupt" itself or answer its own sentences. Layers of defence: the browser or phone echo canceller (client side, enabled in `getUserMedia` and in the Android `AudioRecord` setup), a higher VAD bar while speaking (`SPEAKING_VAD_THRESHOLD`), and this transcript-level guard.

### How it works

`skeleton(text)` produces a spelling-independent form: lowercase, every digit replaced by its spelled-out word, then everything but letters removed. So "FL-98214" becomes `flnineeighttwoonefour`. The docstring explains why: Whisper writes numbers as digits and spelled-out IDs as one token, whereas the agent spoke words, so a word-set comparison misses exactly the content most likely to be misheard.

`is_echo(transcript)` returns True only if all of these hold:

1. The transcript skeleton has at least `ECHO_MIN_CHARS = 10` letters (shorter texts such as "book it" may genuinely be the user answering).
2. The speaker has a timeline of spoken sentences (`_timeline`, a deque of the last 40 `(play_start, play_end, skeleton)` entries).
3. The agent is speaking now, or spoke within the last `ECHO_WINDOW_S = 4.5` seconds (the mic hears what was played in roughly that window, allowing for ASR and VAD latency).
4. Comparing against the concatenated skeletons of sentences played in that window, `difflib.SequenceMatcher` finds matching blocks; blocks shorter than 3 characters are ignored as chance coincidences; and the matched characters divided by the transcript length is at least `ECHO_OVERLAP = 0.6`.

The 0.6 threshold comes from an observation recorded in the source comment: Whisper garbles synthetic speech played back through a microphone ("booking ID is F L" becomes "giving ID is a film"), yet those transcripts still align about 65 to 75 percent, while real user speech essentially never does. Because only the recent window is used, a user who repeats a word the agent said earlier in the conversation is not swallowed (`test_a_user_repeating_words_we_said_earlier_is_not_echo`).

The coordinator applies the guard in two places: in `_voice_partial` (the partial is dropped, no barge-in, no transcript shown) and in `_voice_final` (the final is logged as an echo, the volume is restored with `speaker.resume()`, and no `UserTextEvent` is posted).

### Key types and functions

| Name | File | Role |
|---|---|---|
| `skeleton` | `agent/speech.py` | Letters-only, digits-spelled normal form |
| `is_substantial` | `agent/speech.py` | At least 10 skeleton letters; also used by the talk-over rule |
| `SessionSpeaker.is_echo` | `agent/speech.py` | Overlap test against the recent timeline |
| `ECHO_WINDOW_S`, `ECHO_OVERLAP`, `ECHO_MIN_CHARS` | `agent/speech.py` | Tunable constants (module constants, no env vars) |

### Failure modes and guarantees

The guard is heuristic. False negatives (a badly garbled echo under 60 percent overlap) fall through to the talk-over rule, which still needs two consecutive substantial partials before stopping speech, and finals that are not echoes still go to the planner. False positives require a user phrase that aligns 60 percent or more with the agent's last few seconds of speech; the 10-letter minimum and the 4.5 s window limit that. The thresholds are not configurable at run time. Before the speaker has ever spoken, the timeline is empty and nothing is ever flagged.

### Worked example

The agent says "The booking ID is F L nine eight two one four." The mic picks it up and Whisper writes "giving ID is a film 9 8 2 1 4". Its skeleton, with digits spelled, matches the played sentence's skeleton for most characters; `matched / len(heard)` is above 0.6, `is_echo` is True, and `_voice_partial` returns without emitting anything or stopping the agent. A real user saying "cancel that and book Chicago instead" aligns far below 0.6 with that sentence and passes (`test_echo_guard_survives_whisper_writing_numbers_and_ids_differently`, `test_the_agents_own_voice_in_the_mic_is_dropped_not_treated_as_the_user`).

### Tests

`tests/test_speech.py` (`test_echo_guard_flags_replays_of_recent_speech_but_not_the_user`, `test_skeleton_matches_digits_and_spelled_ids`, `test_echo_guard_survives_whisper_writing_numbers_and_ids_differently`, `test_a_user_repeating_words_we_said_earlier_is_not_echo`), `tests/test_speech_loopback.py` (`test_the_agent_does_not_hear_itself_and_finishes_its_sentence`, `test_a_real_user_talking_over_the_agent_stops_it_mid_sentence`; both need Piper and Whisper).

## 8. Vision (`agent/multimodal/vision.py`, plus frame buffering in the coordinator)

### Purpose

The planner LLM is text-only. When a request refers to something visible ("the city on this poster", "what is on my screen"), the planner calls the read-only `analyze_frame` tool, which runs a vision-language model on the session's newest frame. Vision runs only on demand, never per frame and never on text-only turns; sharing a camera therefore costs nothing until the agent is asked about it.

### How it works

Frame intake. The web client captures a downscaled JPEG (max width 1024, quality 0.7, about 1 frame per second per the UI comment) and sends a `video_frame` JSON message. In `agent/server.py`, the base64 string is rejected if longer than 3,000,000 characters, decoded with `validate=True` (a decode failure yields an empty frame, which is ignored), and posted as a `VideoFrameEvent` with mime, source (`camera` or `screen`) and size. The coordinator's `_handle_video_frame` re-validates (non-empty, at most `MAX_FRAME_BYTES` = 2,000,000 bytes, mime in `ALLOWED_MIME` = jpeg, png, webp) and appends a `_Frame` to a per-session `deque(maxlen=FRAMES_PER_SESSION)` where `FRAMES_PER_SESSION = 3`. Invalid frames are dropped with a warning. No inference happens here.

Frame freshness. `latest_frame(session_id)` returns the newest frame only if it is no older than `MAX_FRAME_AGE_S = 15` seconds (measured with `clock.now()`), so the agent will not answer from a stale image after sharing stopped. When the web UI sends a user message while sharing, it pushes a fresh frame ahead of the text so "this" means what is visible now (comment at `App.jsx` near line 631).

Tool call. The tool is registered in `agent/coordination/tool_router.py` as `analyze_frame` (parameter `question`, `is_state_modifying=False`), but its router handler is a stub returning `has_frame: False`; the real execution is in the coordinator (`_run_vision`), which owns the frame buffer and the backend. With no fresh frame it returns `{"has_frame": False, "answer": "", "note": "No camera or screen frame is being shared right now."}`, an honest result the planner can relay instead of a hallucination (`test_no_frame_gives_the_planner_an_honest_note_not_a_hallucination`). With a frame it calls `vision_backend.analyze(...)` and returns `answer`, `model`, `frame_id`, `frame_age_s`. Because it is an observation tool, its output is input to further reasoning: the coordinator runs one continuation plan after the result (so "search flights to the city on this poster" can read the poster first, then call `search_flights`), and that continuation is not offered `analyze_frame` again, so it cannot loop. Frame bytes are redacted from the trace (`test_frames_are_redacted_from_the_trace`), and an interrupt cancels an in-flight vision call like any other tool call (`test_an_interrupt_cancels_an_in_flight_vision_call`).

### Backends and the OpenRouter failover

`VisionBackend` is an abstract class with an async `analyze(image, mime, question) -> VisionResult(answer, model, latency_s)`. `MockVisionBackend` returns scripted answers (or "Mock vision: I can see an image.") with optional latency and records calls. `get_vision_backend()` picks `openrouter` when `VISION_BACKEND` says so or, if unset, when `OPENROUTER_API_KEY` exists; otherwise the mock.

`OpenRouterVisionBackend` walks an ordered list of free vision models, because free models are individually rate limited, occasionally restricted and sometimes return malformed replies. The module docstring records what probing found: 429s, a 403 "agentic harnesses only" and a reply without `choices`, and that OpenRouter's `openrouter/free` auto router can send a vision question to a content-safety classifier, so it is deliberately not used. The default list `DEFAULT_FREE_VISION_MODELS` is five models (`dots-studio/dots-3-note-preview:free`, `google/gemma-4-31b-it:free`, `google/gemma-4-26b-a4b-it:free`, `qwen/qwen3.8-27b:free`, `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`), ordered by observed reliability; the source tells maintainers to verify availability on OpenRouter because free availability changes. I did not verify these model ids against OpenRouter.

`analyze` steps:

1. Fail fast with `VisionError` if there is no API key, the mime is unsupported, or the image size is outside 1 to 2,000,000 bytes, before any network call.
2. Build a `data:<mime>;base64,...` URL.
3. For each model returned by `_available_models()` (models whose cooldown has expired; if all are cooling down, the single one that recovers soonest), run the blocking HTTP request in the default executor. The request puts the instructions in the user turn together with the question (some free hosts, such as Gemma through AI Studio, reject a system role), with `temperature` 0.1 and `max_tokens` from config.
4. Any `_ModelUnavailable` puts that model on cooldown and moves to the next: HTTP 429 gets 60 s (shared free capacity is busy), 402, 403 or 404 get 600 s (structural), everything else (other HTTP codes, network errors, timeouts, non-JSON bodies, a 200 reply with no `choices`, an empty answer) gets 15 s.
5. The first non-empty answer wins and `last_model` is recorded. If every model fails, `VisionError("all vision models failed: ...")` carries every reason.

Content parts replies (a list of `{"text": ...}`) are flattened. The default `max_tokens` of 1500 exists because some free vision models reason before answering and the reasoning counts against the cap; at 300 an empty answer was observed (per the source comment).

### Key types and functions

| Name | File | Role |
|---|---|---|
| `VisionBackend`, `MockVisionBackend`, `OpenRouterVisionBackend` | `agent/multimodal/vision.py` | Backends with failover and cooldowns |
| `get_vision_backend` | `agent/multimodal/vision.py` | Selects backend from env |
| `VisionError`, `VisionResult` | `agent/multimodal/vision.py` | Error and result types |
| `_handle_video_frame`, `latest_frame`, `_run_vision` | `agent/coordinator.py` | Ring buffer, freshness, tool execution |
| `VideoFrameEvent` | `agent/schemas/events.py` | Inbound event |
| `analyze_frame` tool registration | `agent/coordination/tool_router.py` | Schema the planner sees |

### Configuration

| Env var | Default | Effect |
|---|---|---|
| `VISION_BACKEND` | `openrouter` if `OPENROUTER_API_KEY` is set, else `mock` | `mock` or `openrouter` |
| `OPENROUTER_API_KEY` | none | Required for the real backend |
| `VISION_MODELS` | the five-model default list | Comma-separated override of the failover order |
| `VISION_TIMEOUT_S` | 30 | Per-request timeout |
| `VISION_MAX_TOKENS` | 1500 | Completion cap, including reasoning |

### Failure modes and guarantees

- Without a key, the mock backend is used (answers are canned, not real), so the tool works in demos and tests but does not actually look at the image. `OpenRouterVisionBackend` raises `VisionError` if constructed without a key and called.
- On a `VisionError` the coordinator reports the failure and does not continue (`test_vision_failure_is_reported_and_does_not_continue`).
- Failover latency can add up: each failing model is tried serially with up to `VISION_TIMEOUT_S` each (a timed-out model is skipped for 15 s afterwards). The cooldown map is in memory per backend instance, so it resets on restart.
- Frames are bounded (3 per session, 2 MB each); the buffer is cleaned when a session is evicted.
- The vision stage in the full warmup is a configuration check only (`get_vision_backend`); nothing is preloaded.

### Worked example

The user shares a screen showing a poster for Goa and types "find flights to the city on this poster". The UI pushes a fresh frame; `_handle_video_frame` stores it. The planner emits `analyze_frame(question="Which city is on the poster?")`. `_run_vision` finds the frame (age under 15 s) and calls the OpenRouter backend. The first model returns HTTP 429, so it is put on a 60 s cooldown; the second answers "Goa". The tool result returns `answer: "Goa"` with the model name, the continuation plan runs once with that observation, and calls `search_flights` for Goa. A second vision question 10 seconds later skips the first model entirely until its cooldown ends.

### Tests

`tests/test_vision.py` (data-URI request format, token budget, failover and cooldown, every failure kind, all-fail error text, content-part flattening, input validation before network, planner integration, no-frame honesty, stale frames, bounded buffer, redaction, interrupt cancellation, WebSocket `video_frame` routing, and a `live` test that spends real tokens and is excluded by default).

## 9. What works without the optional extras

The server and agent are designed to degrade per capability rather than fail at start-up. Facts from the code:

| Missing | What happens |
|---|---|
| Piper (`pip install -e .[tts]`) or the voice file | `get_tts_backend()` returns `None` in `auto`. `tts_status` reports `available: false`, `/health` shows `tts.available: false`, the UI hides the speaker toggle, replies are text only. `TTS_BACKEND=mock` still lets you exercise the whole speech path with silent audio |
| Microphone, or never sending `voice_stream start` | Nothing in the voice path runs. Typing works identically; the voice runtime is created only on `start`. Push-to-talk and the older `audio_chunk` JSON path need a client that records audio |
| faster-whisper or the Whisper model (offline, not cached) | `ASRProcessor` sets `load_failed`, logs a warning and returns empty strings. Voice input yields empty transcripts; everything else works |
| Silero VAD or onnxruntime | `make_vad()` falls back to `EnergyVAD`; streaming still segments utterances, with a crude RMS threshold |
| Embeddings extra (`[embeddings]`, MiniLM) | Not in this module, but voice depends on the Tier 1 classifier for barge-in decisions: `_classify` answers with the keyword heuristic until MiniLM has loaded (or if it cannot be loaded), without blocking, so barge-in and interrupt-word detection still work. Setting `INTENT_EMBEDDINGS=0` skips the MiniLM load |
| `OPENROUTER_API_KEY` | `get_vision_backend()` returns the mock vision backend; `analyze_frame` returns canned answers |
| Any camera or screen share | No frames exist; `analyze_frame` returns `has_frame: false` and a plain note |

The eval harness and CI gate run in mock mode with the virtual clock; voice scenarios are not part of that gate and the real audio and model tests skip when models are unavailable. The speech scenarios (`speak_barge_in`, `speak_plain`) use `MockTTSBackend`, which is why that backend is `cheap` (inline, deterministic, no threads).

## 10. Operational notes and honest caveats

- The defaults shown in `.env.example` and the `streaming.py` docstring match the code: endpoint 500 ms, partial interval 500 ms.
- Typical latency numbers in comments (duck at about 0.35 s, stop at the first non-echo partial at about 1.2 s, per the comment in `agent/coordinator.py`) are author estimates, not guarantees; real values depend on CPU, Whisper model and the cadence settings. The measured, tested guarantee is the 150 ms bound between an epoch change and the `stopped` emission, checked in `tests/test_speech_eval.py` and `tests/test_speech.py`.
- Barge-in during speech needs two consecutive substantial partials (or one interrupt word) before the agent stops; the volume is ducked meanwhile. This trade-off is intentional (comment in `_voice_partial`) and trades a slightly longer talk-over for far fewer self-interruptions.
- The web UI also keeps a legacy push-to-talk transport (binary WebM) for compatibility; the hands-free streaming path is the primary one.
- Speech is per session and in memory. Preferences are not persisted server-side; clients re-announce `tts` on every connect.
- Whisper is the throughput bottleneck: one model instance, serialised inference. The design limits damage by skipping partial ticks while one is running and by reusing the tail partial as the final.
