# Android App (android/)

This chapter describes the native Android client of Kairos: a Kotlin and Jetpack Compose app that speaks the same WebSocket protocol as the web UI. Like the web client it is thin: all agent logic (epochs, cancellation, planning, memory) stays on the server. What the app adds is the mobile-specific plumbing: reconnecting over unreliable networks, capturing the microphone with the platform echo canceller, playing the agent's voice so it can be interrupted, and sampling the camera.

Important honesty note, taken from `android/README.md` and confirmed by the repository layout: the app has never been run on a device or an emulator (the development machine has no KVM). Everything described here is verified at the JVM level only: unit tests, Robolectric-rendered Compose UI, and a mock WebSocket server. Platform behaviours such as the real echo canceller, CameraX on hardware, and permission prompts are unverified.

## 1. Purpose

- Give a phone user the same capabilities as the web console: chat, hands-free voice with barge-in, spoken replies that can be talked over, camera sharing for the vision tool, and views of the agent's state (epoch, slots, in-flight calls), a trace timeline and the cognitive graph.
- Prove that the server protocol is a real, documented contract and not something coupled to the React app: the Android client is built against fixtures generated from the server's own pydantic classes.
- Be testable without hardware: every Android-specific edge (network, speaker, microphone, settings store, auth probe) sits behind an interface and is replaced by a fake in tests.

## 2. Project layout

| Path | Role |
|---|---|
| `android/settings.gradle.kts`, `android/build.gradle.kts`, `android/gradle/libs.versions.toml` | Gradle project and version catalogue |
| `android/app/build.gradle.kts` | Application module: `compileSdk 34`, `minSdk 26`, `targetSdk 34`, namespace and applicationId `com.samsung.interruptible`, Compose, serialization, CameraX, OkHttp |
| `android/Dockerfile.build` | JDK 17 image with the Android SDK (platform 34, build-tools 34.0.0), used to build and test without Android Studio |
| `android/app/src/main/AndroidManifest.xml` | Permissions `INTERNET`, `RECORD_AUDIO`, `CAMERA`, `MODIFY_AUDIO_SETTINGS`; microphone and camera features are optional; `allowBackup=false` |
| `android/app/src/main/res/xml/network_security_config.xml` | Allows cleartext (`ws://`) so a LAN or emulator backend works |
| `.../data/` | `Protocol.kt`, `Transport.kt`, `Urls.kt` (also holds `Backoff`), `Settings.kt`, `AuthApi.kt` |
| `.../state/` | `ChatState.kt`, `ChatReducer.kt`, `AgentController.kt` |
| `.../audio/Audio.kt` | `SpeechOutput` and `MicInput` interfaces, `AudioTrackSpeechOutput`, `AudioRecordMic`, `Pcm` helpers |
| `.../camera/FrameSampler.kt` | CameraX analyzer that produces throttled JPEG frames |
| `.../ui/` | Compose screens: `AgentApp.kt`, `ChatScreen.kt`, `Panels.kt`, `LoginScreen.kt`, `Markdown.kt`, `GraphLayout.kt`, `Theme.kt` |
| `.../AgentViewModel.kt`, `MainActivity.kt` | Wiring: the ViewModel owns the long-lived pieces so they survive rotation |
| `android/app/src/test/` | Unit and Robolectric tests, fixtures under `resources/` |
| `android/demo-data/` | Recorded server frames (`wsraw.json`, `wsraw_voice.json`) used to render the demo video's phone section |

Libraries (from `app/build.gradle.kts`): AndroidX core, activity-compose, lifecycle, the Compose BOM with Material 3, OkHttp for WebSocket and HTTP, kotlinx-serialization-json, kotlinx-coroutines, CameraX (core, camera2, lifecycle, view). Test libraries: JUnit, coroutines-test, OkHttp MockWebServer, Robolectric, Compose UI test. The README states the versions as AGP 8.5, Kotlin 2.0.20 and Compose BOM 2024.09.

`BuildConfig.DEFAULT_SERVER_URL` is `ws://10.0.2.2:8000`: inside the Android emulator, `10.0.2.2` is the host machine, so the default reaches a backend on the developer's laptop. On a phone, the user types the LAN address in Settings.

## 3. How it works

### 3.1 Layering

```
  +--------------------------------------------------------------------------+
  | ui/  Compose: AgentApp (tabs) ChatScreen Panels LoginScreen              |
  +-------------------------------+------------------------------------------+
                                  | StateFlow<ChatState>, StateFlow<AuthStatus>
  +-------------------------------v------------------------------------------+
  | state/AgentController   full-duplex rules, glue, auth flow               |
  |   state/ChatReducer     pure (ChatState, ServerMessage) -> ChatState     |
  +----+---------------+---------------+--------------+-----------------+----+
       | Transport     | SpeechOutput  | MicInput     | SettingsStore   | AuthApi
  +----v-----+   +-----v------+  +-----v-----+  +-----v------+   +------v-----+
  | OkHttp   |   | AudioTrack |  | AudioRecord|  | SharedPrefs|   | HttpAuthApi|
  | WebSocket|   | (voice     |  | + AEC/NS/  |  |            |   | /health,   |
  | +backoff |   |  comm.)    |  |   AGC      |  |            |   | /auth/check|
  +----------+   +------------+  +------------+  +------------+   +------------+
```

Everything above the interface line is plain Kotlin and runs in a JVM unit test. Everything below it is Android-specific.

### 3.2 Start-up

`MainActivity` sets `FLAG_KEEP_SCREEN_ON` (hands-free listening is pointless if the screen sleeps) and renders `AgentApp(viewModel.controller)`. `AgentViewModel` builds the real implementations (`OkHttpTransport`, `AudioTrackSpeechOutput`, `AudioRecordMic`, `SharedPrefsSettingsStore`, `HttpAuthApi`) inside a `SupervisorJob` scope on `Dispatchers.Main.immediate`, then calls `controller.start()` (begin collecting the transport flows) and `controller.begin()` (sign-in probe, then connect).

### 3.3 Sign-in flow (`AgentController.begin`, `signIn`, `HttpAuthApi`)

The flow mirrors the web login. `HttpAuthApi.probe(serverUrl, token)` converts the WebSocket address to HTTP with `Urls.httpBase`, then:

1. `GET /health` (6 s call timeout). A non-2xx answer or an exception gives `AuthProbe.Unreachable`.
2. If `auth_required` is false the result is `AuthProbe.Open`.
3. If a key is needed and none is stored: `NeedsKey(hadKey = false)`.
4. Otherwise `GET /auth/check` with `Authorization: Bearer <token>`: success gives `Valid`, failure `NeedsKey(hadKey = true)`.

`begin()` maps the result: `Open`, `Valid` and `Unreachable` all set `AuthStatus.Ready` and connect (an unreachable server must not block the app, which then shows its own reconnect state); `NeedsKey` shows `AuthStatus.Login`, with the notice "Your saved key is no longer valid" only when a key had been stored. `signIn(serverUrl, key)` probes a candidate settings object first and saves it only on `Open` or `Valid`; otherwise it returns "That key was not accepted." or "Cannot reach the server (...)". If the transport later reports `ConnState.Refused` and an auth API is present, the controller returns to the login screen with "The server did not accept your key". `signOut()` stops voice, flushes audio, disconnects, clears the token and shows the login screen.

### 3.4 Transport (`data/Transport.kt`)

`Transport` is an interface (`state: StateFlow<ConnState>`, `incoming: SharedFlow<ServerMessage>`, `connect`, `disconnect`, `sendText`, `sendBinary`). `OkHttpTransport` is the real implementation:

- OkHttp client with a 20 s ping interval (to notice dead connections) and no read timeout (a WebSocket is idle between actions by design).
- `ConnState` values: `Disconnected`, `Connecting`, `Connected`, `Refused(reason)` and `Retrying(inMs, reason)`.
- A `generation` counter ignores callbacks from a socket that has already been replaced.
- Loss handling (`lost`): when the connection drops it schedules a reconnect using `Backoff` (500 ms base, doubling, 8 s cap, plus up to 20 percent random jitter in the production configuration). Close or failure callbacks may all fire for one socket, so each listener handles the loss once.
- Refusal is never retried: HTTP 401 or 403 at handshake, or a server close with code 1008 (policy violation, which is what `agent/server.py` uses for auth, origin and abuse refusals). The state becomes `Refused` and stays there until the user changes settings.
- A bug that the tests caught: when the server initiates a close, OkHttp only completes the close handshake once the client replies. `onClosing` therefore calls `webSocket.close(code, reason)` itself; without it the client sat "Connected" on a dead socket and never reconnected.
- `sendText` and `sendBinary` return false unless the state is `Connected`, and the frame is dropped, never queued: stale input must not replay after a reconnect.
- Incoming frames go into a `MutableSharedFlow` with a 512-element buffer that drops the oldest on overflow.

### 3.5 Protocol (`data/Protocol.kt`)

`Protocol.parse(frame)` turns a JSON text frame into a `ServerMessage`: `Action(AgentAction)`, `Error(code)` or `TtsStatus(enabled, available)`. It never throws; non-JSON gives null. It uses kotlinx-serialization's tree API with `ignoreUnknownKeys` and `isLenient`, and small helper accessors that return defaults for missing or mistyped fields.

Server actions, keyed on `action_type`, become members of the sealed interface `AgentAction`:

| `action_type` | Kotlin type |
|---|---|
| `filler` | `Filler` |
| `spoken_response` | `SpokenResponse` (with `isFinal`, default true) |
| `clarification` | `Clarification` (question) |
| `tool_call` | `ToolCall` (arguments rendered to strings) |
| `tool_cancel` | `ToolCancel` (reason defaults to `epoch_stale`) |
| `state_snapshot` | `StateSnapshot` (intent, slots, in-flight list) |
| `agent_step` | `AgentStep` (with optional `Artifact`) |
| `graph_update` | `GraphUpdate` (op, nodes, edges) |
| `transcript` | `Transcript` (partial flag, utterance id) |
| `voice_activity` | `VoiceActivity` |
| `audio_out` | `AudioOut` (base64 PCM16 sentence) |
| `speech_state` | `SpeechState` (carries `spokenText` on `stopped`) |
| `file_exported` | `FileExported` |
| anything else | `Unknown`, ignored by the reducer, so a newer server never crashes an older app |

`Outgoing` builds the client frames: `userText`, `interrupt`, `tts`, `voiceStream("start"|"stop")` and `videoFrame(jpegBase64, source, width, height)`. Binary frames (raw PCM) are sent directly by the controller.

### 3.6 State and reducer (`state/ChatState.kt`, `state/ChatReducer.kt`)

`ChatState` is one immutable data class: connection, chat messages, the agent's view (`epoch`, `intent`, `slots`, `inFlight`), diagnostics (`trace`, `agents`, `artifact`, `exports`, graph nodes and edges), voice and camera flags (`handsFree`, `userSpeaking`, `livePartial`, `agentSpeaking`, `ducked`, `ttsAvailable`, `speakReplies`, `sharing`, `lastFrameAtMs`) and an `error` banner. `ChatReducer` is a pure object; `reduce(state, message, nowMs)` is the whole UI state machine, and it mirrors what `App.jsx` does:

- The epoch is a high-water mark: `epoch = max(old, action.epoch)`, and a `state_snapshot` sets it explicitly.
- `Filler`, `SpokenResponse` and `Clarification` become chat messages (a clarification is flagged `isQuestion`); each also writes a trace item. The trace is capped at 400 entries.
- `ToolCall`: traces it; only `spawn_agent` creates an agent card and sets `artifactLoading`. Unlike the web UI, which makes a card for every tool, Android shows cards for worker agents only.
- `ToolCancel`: marks the card cancelled with the reason, and stops the artifact spinner when there is no artifact.
- `AgentStep`: updates the card, and installs the artifact when present.
- `GraphUpdate`: `full` replaces, otherwise nodes are appended deduplicated by id and edges appended.
- `Transcript`: voice bubbles are keyed by `utteranceId`. Partials set "text..." and `livePartial`; the final replaces the bubble text and clears `pendingVoice`; an empty final removes the bubble (the VAD fired on noise). A transcript with no utterance id (push-to-talk style) becomes a user message.
- `VoiceActivity`: `speech_start` creates a pending bubble and sets `userSpeaking`; `speech_end` and `idle` clear it; `barge_in` adds a trace entry.
- `SpeechState`: `started`/`finished`/`ducked`/`resumed` set `agentSpeaking` and `ducked`. `stopped` finds the agent message whose text equals the action's `text` and stores `heard = spokenText` in it, so the UI can annotate "You heard: ... then interrupted"; the trace records what was said before the cut.
- `FileExported`: appends an `ExportItem`, installs the preview as the artifact.
- `connectionChanged`: writes a trace line, and when the socket drops it clears the per-connection speech flags, because the agent's view of speech is stale once the connection is gone.
- `describeError` maps `rate_limit`, `too_large`, `bad_json` to friendly sentences, the same set of codes the web client handles.

Because the reducer is pure and takes `nowMs` as an argument, `ChatReducerTest` can assert exact states with no clock or device.

### 3.7 Controller and the full-duplex rules (`state/AgentController.kt`)

`AgentController` receives the interfaces by constructor injection and exposes `state` and `auth` flows. The rules that make the device full duplex:

1. `onServerMessage`: before reducing, side effects on audio. `AudioOut` is decoded and played only when `speakReplies` is on; `speech_state` `ducked` calls `speech.setDucked(true)`, `resumed` and `finished` call `setDucked(false)`, `stopped` calls `speech.flush()`.
2. `sendText`: calls `speech.flush()` before sending, so typing over the agent's voice silences it immediately without waiting for the server. If the transport refuses the frame the message is not added to the chat and an error "Not connected: your message was not sent." is shown.
3. `interrupt()` (the Stop button): flush locally, then send `interrupt` (`ui_barge_in`).
4. `setHandsFree(true)`: starts the microphone with a callback that sends each 100 ms frame as a binary frame, then sends `voice_stream start`; opening the microphone a second time is a no-op; if the mic cannot open an error is shown and nothing is announced.
5. `onConnected()`: after every (re)connect it re-announces `tts` (if replies are on) and `voice_stream start` (if hands-free is on), because the server keeps no per-connection preferences.
6. `sendFrame(jpeg, w, h)`: only while `sharing` is true; sends base64 JPEG with source `camera`.
7. `applySettings(new)`: sanitizes the session id (`Urls.sanitizeSessionId`), saves, and reconnects; this is also the only way out of a `Refused` state.

### 3.8 Audio (`audio/Audio.kt`)

Playback, `AudioTrackSpeechOutput`:

1. `play(pcm, rate)` puts a chunk on an unbounded channel stamped with the current `generation`.
2. A single writer coroutine on `Dispatchers.IO` writes chunks to an `AudioTrack` in slices of 4096 bytes and skips any chunk whose generation is stale.
3. `flush()` increments `generation` (queued chunks are skipped), then pauses and flushes the track to discard audio already in the pipeline. Because writes are small slices and the buffer is about 200 ms (`max(minBuf, rate / 5 * 2)` bytes), a flush lands within roughly 100 ms.
4. `setDucked(true)` sets track volume to `Pcm.DUCK_VOLUME = 0.15`, the same value as the web client.
5. The track is declared `USAGE_VOICE_COMMUNICATION` and `CONTENT_TYPE_SPEECH`: that is what gives the platform echo canceller its reference signal.

Capture, `AudioRecordMic`:

1. `AudioRecord` with source `VOICE_COMMUNICATION`, 16 kHz, mono, PCM16, buffer `max(minBuf, 4 * 3200)` bytes. A `SecurityException` or an uninitialised record returns false.
2. It switches `AudioManager` to `MODE_IN_COMMUNICATION` (restoring the previous mode on stop) and attaches `AcousticEchoCanceler`, `NoiseSuppressor` and `AutomaticGainControl` when the device offers them.
3. A coroutine reads exactly `Pcm.FRAME_BYTES = 3200` bytes (100 ms) per frame and hands a copy to the callback; this is the same frame size and format as the web worklet, which is what the server's `voice_stream` mode expects.

`Pcm` holds the constants and Base64 helpers (a malformed Base64 string decodes to empty rather than throwing).

### 3.9 Camera (`camera/FrameSampler.kt`, `ChatScreen.SharingChip`)

When sharing is on, `SharingChip` creates a CameraX `LifecycleCameraController` with only the image-analysis use case (back camera, `STRATEGY_KEEP_ONLY_LATEST`) and a preview thumbnail. `FrameSampler` is the analyzer. It throttles to one frame per `intervalMs` (1000), converts YUV_420_888 to NV21 (`yuv420ToNv21` handles arbitrary row and pixel strides), compresses to JPEG, decodes, rotates upright according to the sensor rotation, scales so the longest side is at most 1024 px, and re-encodes at quality 70. The callback goes to `controller.sendFrame`. As on the web, frames are only buffered by the server, and the vision model runs only when a question needs the image. The app does not push a fresh frame before a typed message, unlike the web client (`handleSendMessage` awaits a capture). Screen sharing is not implemented (the README says it would need MediaProjection).

### 3.10 UI

`AgentApp` shows a `Scaffold` with a top bar (wordmark, connection dot and label, epoch number, settings button) and a bottom `NavigationBar` with four tabs: Chat, State, Trace, Graph. It asks for the `RECORD_AUDIO` and `CAMERA` runtime permissions when the user first taps the mic or camera and then performs what they asked. `ChatScreen` has the message list (bubbles with a minimal markdown renderer, agent cards, artifact card, export cards with Copy path and Download), suggestion chips, a listening bar and an input row with mic, speaker, camera toggles and either Send or, when busy and the draft is empty, a Stop button. `Panels.kt` has `StatePanel`, `TracePanel` (timeline strip plus log), `GraphPanel` and `SettingsForm` (server, token, session id; a warning when the address is not encrypted and not local). `Markdown.kt` handles only bold, inline code and bullet markers, since a full renderer would be overkill. `GraphLayout.kt` lays turns out left to right in order and hangs entities and artifacts below the turn they connect to (column width 190, row height 70); it is deterministic and unit-tested.

Export download opens the server's `/exports/<name>` URL in the system viewer, with the token as a `?token=` query parameter. This relies on the server accepting the token in the query string, which `security.bearer_token()` does. The token therefore appears in a URL; this is acceptable only over `wss`/`https`, and the Settings form warns about plaintext remote addresses.

### 3.11 Worked example: a spoken barge-in on the phone

The user turns on "Speak replies" and hands-free, asks for flights, and starts talking while the agent is speaking.

1. Tapping the mic launches the permission request if needed, then `controller.setHandsFree(true)`: `AudioRecordMic.start` begins sending 3200-byte binary frames and the controller sends `{"type":"voice_stream","action":"start"}`.
2. The server VAD sends `voice_activity speech_start`; the reducer adds a pending bubble "..." keyed by the utterance id and sets `userSpeaking`. Transcript partials refine it.
3. A reply is spoken: `audio_out` frames arrive one sentence at a time and `onServerMessage` hands each to `AudioTrackSpeechOutput.play`; `agentSpeaking` is true.
4. The user talks over it. The server sends `speech_state ducked`: the controller sets volume to 0.15 immediately. If the server decides it was a real interruption it sends `stopped` with `spoken_text`: the controller calls `flush()` (generation bump plus track flush) and the reducer stores `heard` on the matching message, which the chat shows as the point where the reply was cut. If it was noise the server sends `resumed` and volume returns to 1.
5. Meanwhile the in-flight tool call is cancelled server-side; a `tool_cancel` marks the card and a snapshot raises the epoch shown in the top bar.

## 4. The fixtures contract with the server

The Kotlin `Protocol.kt` is hand-written, so drift against the Python schema would be silent in production. Two shared files in `android/app/src/test/resources/` prevent that:

- `server_messages.jsonl`: one JSON object per line, one example of every action type plus `tts_status` and `error`. It is generated by `scripts/dump_android_fixtures.py`, which instantiates the real pydantic classes from `agent/schemas/actions.py` (`FillerAction`, `SpokenResponseAction`, `ClarificationAction`, `ToolCallAction`, `ToolCancelAction`, `StateSnapshotAction`, `AgentStepAction`, `GraphUpdateAction`, `TranscriptAction` partial and final, `VoiceActivityAction`, `AudioOutAction`, `SpeechStateAction` ducked and stopped, `FileExportedAction`) with fixed timestamps and fixed `action_id` values, so the output is deterministic. `python scripts/dump_android_fixtures.py` rewrites it; `--check` exits 1 if the checked-in file is stale.
- `client_frames.json`: the seven frames the app sends (`user_text`, `interrupt`, `tts_on`, `tts_off`, `voice_start`, `voice_stop`, `video_frame`). The Kotlin tests assert that `Outgoing` builds exactly these, and the server-side test replays them against the real server.

`tests/test_android_fixtures.py` enforces the contract from the Python side:

| Test | What it guarantees |
|---|---|
| `test_server_fixtures_are_up_to_date` | Runs `dump_android_fixtures.py --check`: a schema change fails CI until fixtures and Kotlin models are updated together |
| `test_every_action_type_the_server_can_send_is_covered_by_a_fixture_or_knowingly_ignored` | Every value of `ActionType` has a fixture, so adding an action type forces a fixture and a Kotlin decision |
| `test_the_apps_generated_session_ids_pass_the_servers_validation` | The `android_<12 hex>` id passes `Settings.session_id_re`; over-long ids are refused by the server and truncated by the app |
| `test_the_server_accepts_every_frame_the_app_sends` | Replays the client frames through a `TestClient` WebSocket with a mock LLM: no `error` reply, `tts_status` acknowledged, and a typed request yields a `filler` first and a `tool_call` |
| `test_the_server_stores_the_frame_the_app_sends` | The coordinator stores the app's `video_frame` as `image/jpeg` |

The rule from the project instructions: adding or changing an action type means updating the dump script, the fixtures, `Protocol.kt` and `ChatReducer.kt`. A related gap is the web client, which has no such contract test and, as noted in the web chapter, does not handle `clarification`.

## 5. Key types and functions

| Name | File | Role |
|---|---|---|
| `Protocol.parse`, `Outgoing` | `android/app/src/main/java/com/samsung/interruptible/data/Protocol.kt` | Tolerant decoder and frame builders |
| `AgentAction` (sealed) | `data/Protocol.kt` | Typed server actions, `Unknown` fallback |
| `Transport`, `OkHttpTransport`, `ConnState` | `data/Transport.kt` | WebSocket, reconnect, refusal rules |
| `Backoff`, `Urls` | `data/Urls.kt` | Retry delays; URL, session id and security helpers |
| `Settings`, `SharedPrefsSettingsStore` | `data/Settings.kt` | Server URL, token, session id, speak flag |
| `AuthApi`, `HttpAuthApi`, `AuthProbe` | `data/AuthApi.kt` | `/health` and `/auth/check` probe |
| `ChatState`, `ChatReducer` | `state/` | Immutable UI state; pure transitions |
| `AgentController` | `state/AgentController.kt` | Glue, full-duplex rules, auth flow |
| `AudioTrackSpeechOutput`, `AudioRecordMic`, `Pcm` | `audio/Audio.kt` | Playback with flush and duck; AEC microphone capture |
| `FrameSampler` | `camera/FrameSampler.kt` | 1 fps, at most 1024 px JPEG frames |
| `GraphLayout`, `Markdown` | `ui/` | Pure helpers for the graph tab and chat bubbles |
| `AgentViewModel` | `AgentViewModel.kt` | Owns the real dependencies across rotation |

## 6. Configuration

| Knob | Value | Where |
|---|---|---|
| Default server | `ws://10.0.2.2:8000` | `BuildConfig.DEFAULT_SERVER_URL` in `app/build.gradle.kts` |
| Settings (persisted) | server, token, session id, speak replies, in `SharedPreferences` file `agent_settings` | `Settings.kt` |
| Session id | `android_` plus 12 hex chars; valid pattern `^[A-Za-z0-9_-]{1,64}$` | `Urls.kt` |
| Reconnect backoff | 500 ms doubling to 8 s cap, 20 percent jitter | `Backoff`, `OkHttpTransport` |
| WebSocket ping | every 20 s | `OkHttpTransport` |
| Mic frame | 16 kHz mono PCM16, 3200 bytes (100 ms) | `Pcm` |
| Duck volume | 0.15 | `Pcm.DUCK_VOLUME` |
| Camera | 1 frame/s, 1024 px longest side, JPEG quality 70 | `FrameSampler` defaults |
| Trace cap | 400 entries | `ChatReducer.MAX_TRACE` |
| Auth probe timeout | 6 s | `HttpAuthApi` |
| Release build | minified, unsigned; only debug-signed APKs are produced | `app/build.gradle.kts` |

There are no environment variables for the app at runtime. The `AGENT_SERVER_URL` and `AGENT_TOKEN` environment variables are test-only (live server test), and `DEMO_DATA` is for the demo frame renderer.

## 7. Building and running tests in Docker

There is no local Android SDK and no emulator, so the project builds in a container. The commands (from `android/README.md` and the header of `android/Dockerfile.build`):

```
docker build -f android/Dockerfile.build -t iagent-android-build android/
docker run --rm -v "$PWD/android:/workspace" -v iagent-gradle:/root/.gradle \
    iagent-android-build ./gradlew testDebugUnitTest assembleDebug
```

The image is `eclipse-temurin:17-jdk-jammy` with command-line tools, `platform-tools`, `platforms;android-34` and `build-tools;34.0.0`; building it accepts the Android SDK licences. The APK lands in `android/app/build/outputs/apk/debug/app-debug.apk`. Named volume `iagent-gradle` caches Gradle between runs. Gradle's build cache can replay an old test result when a test reads data that is not a Gradle input (for example fixtures or demo data): add `--no-build-cache` and `cleanTestDebugUnitTest`. Tests that need a running server use `docker run --network host -e AGENT_SERVER_URL=ws://127.0.0.1:8000 ...`.

`app/build.gradle.kts` sets `unitTests.isReturnDefaultValues = true` (Android stubs return defaults so pure Kotlin stays testable) and `unitTests.isIncludeAndroidResources = true` (Robolectric needs merged resources).

## 8. Interactions with other components

- `agent/server.py`: `/ws/{session_id}` with `?token=`, `/health`, `/auth/check`, `/exports/{name}`; the server's origin check is not applied to a native client that sends no Origin (see the server chapter for the exact rule).
- `agent/schemas/actions.py`: the single source of truth for the protocol, copied into fixtures by `scripts/dump_android_fixtures.py`.
- `agent/multimodal/` and `agent/speech.py`: consume the PCM frames and produce `transcript`, `voice_activity`, `audio_out`, `speech_state`.
- `agent/coordinator.py`: owns `set_tts`, the epoch model and the cancellations the app renders.
- `scripts/demo/`: `DemoFrames.kt` renders the phone section of the demo video from recorded server frames (see the demo chapter).

## 9. Failure modes and guarantees

- Server unreachable: the app still opens; it shows `Retrying` with the delay, and keeps retrying with backoff.
- Wrong token or refused handshake (401, 403, close 1008): state `Refused`, no retry loop; with an auth API configured, the app returns to the login screen.
- Dead socket after a server-initiated close: handled (answer the close), covered by a transport test.
- Message typed while disconnected: not shown as sent, error banner, text stays.
- Mic cannot open or permission denied: error banner; nothing is announced to the server; hands-free stays off.
- Unknown action type or malformed frame: ignored, never a crash.
- Server drops a frame under rate limiting: an `error` message produces a banner with a friendly string.
- Unverified on real hardware: echo cancellation quality (device dependent), CameraX behaviour, AudioTrack latency, permission flows.
- Known limits (README): hands-free listening stops when the app is backgrounded (no foreground service); camera only, no screen sharing; debug-signed APK only; cleartext `ws://` is allowed globally by `network_security_config.xml` (the Settings form warns on non-local plaintext addresses); the Settings dialog window itself is not UI-tested because Robolectric cannot settle a Compose `Dialog` (the form inside it is tested).
- Differences from the web client: no new-chat or history list (a single session id from Settings), no fresh camera frame sent before a typed message, agent cards only for `spawn_agent` calls, and the graph tab uses a turn-centred layout rather than three type columns.

## 10. Tests that cover it

The README states 125 JVM tests and the `@Test` counts in the source add up to that number:

| Test file | Count | Covers |
|---|---|---|
| `ProtocolTest.kt` | 19 | Parsing every fixture, tolerance to unknown actions and bad JSON, `Outgoing` frames equal to `client_frames.json` |
| `ChatReducerTest.kt` | 24 | Voice bubbles, truncation notes (`heard`), graph merge, trace cap, epoch high-water mark, connection changes |
| `AgentControllerTest.kt` | 18 | Connect with token and session, send and silence, Stop, play only when replies are on, duck then stop flushes, reconnect re-announce, hands-free start and stop, frames only while sharing, settings sanitising, end-to-end flow |
| `AuthTest.kt` | 16 | Probe results, login, sign-out, refused returns to login |
| `UrlsTest.kt` | 8 | URL building, session id sanitising, insecure-address detection, backoff |
| `OkHttpTransportTest.kt` | 9 | Against a real MockWebServer: reconnect after close, downed server, 403 and 1008 not retried, answering server-initiated close |
| `MarkdownGraphTest.kt` | 7 | `Markdown` and `GraphLayout` |
| `UiTest.kt` | 20 | Compose screens under Robolectric, clicks, and screenshots written to `app/build/screenshots/` (copies live in `android/docs/screenshots/`) |
| `LiveServerTest.kt` | 4 | Skipped unless `AGENT_SERVER_URL` is set; real client code against a running server including streamed speech from `book_flight_16k.pcm` |
| `DemoFrames.kt` | 1 | Opt-in via `DEMO_DATA`; renders demo-video frames, not a normal test |
| `Fakes.kt` | - | Shared fakes: `FakeTransport`, `FakeSpeech`, `FakeMic`, `FakeStore`, `FakeAuthApi` |

On the Python side: `tests/test_android_fixtures.py` (section 4). CI has an `android` job (checked by `tests/test_deploy_files.py`).
