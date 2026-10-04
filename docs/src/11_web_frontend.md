# Web Frontend (frontend/)

This chapter describes the browser client of Kairos: a Vite + React 18 + Tailwind single-page app that talks to the Python backend over one WebSocket per conversation. It is a deliberately thin client. All decisions about epochs, cancellation, tool dispatch and memory are made on the server; the browser captures the microphone and camera, renders the actions the server streams back, plays the agent's voice, and lets the user interrupt at any moment.

## 1. Purpose

The frontend exists to make the interruptible-agent behaviour visible and usable by a human judge in a few seconds:

- A chat view where the user types or talks hands-free, and can press Esc (or just start talking) to interrupt.
- A live "inspector" on the right: worker agents (one card per dispatched tool call), the current state snapshot (epoch, intent, slots, in-flight calls), a trace log with a timeline, the cognitive graph, and exported files.
- Spoken replies (server-side Piper TTS) with client-side ducking and instant stop on barge-in.
- Camera and screen sharing, sampled at 1 frame per second.
- A real sign-in step when the server is protected by `AUTH_TOKEN`.

The built bundle is served by the same FastAPI process at `/` (`agent/server.py` mounts `frontend/dist` with `StaticFiles(html=True)` when that directory exists), so there is no CORS, no second server, and the WebSocket URL is always same-origin.

## 2. Layout of the directory

| Path | Role |
|---|---|
| `frontend/package.json` | Scripts `dev`, `build`, `preview`; React 18.3, react-markdown 10 + remark-gfm, lucide-react icons, clsx, tailwind-merge; dev deps Vite 6, Tailwind 3.4, PostCSS, autoprefixer |
| `frontend/vite.config.js` | React plugin, dev server on port 5173 with a proxy, build output `dist/` |
| `frontend/tailwind.config.js` | Scans `index.html` and `src/**/*.{js,ts,jsx,tsx}`; adds JetBrains Mono as the mono font |
| `frontend/index.html` | Dark theme shell (`class="dark"`), Google Fonts (Cormorant Garamond, Inter, JetBrains Mono), `kairos.svg` icon |
| `frontend/src/main.jsx` | Mounts `<App />` inside `React.StrictMode` |
| `frontend/src/App.jsx` | The whole application (1672 lines, one component) |
| `frontend/src/hooks/useSpeechPlayer.js` | WebAudio playback of `audio_out`, ducking, flush |
| `frontend/src/lib/history.js` | Local conversation history in `localStorage` |
| `frontend/src/components/` | `LoginScreen`, `TraceTimeline`, `ExportsList`, `Toasts`, `SuggestionChips`, `MarkdownReply`, `Brand` |
| `frontend/public/` | `kairos.svg`, `bg-f1.jpg`, `mockup.png` |

There is no router, no state library and no TypeScript. The project instructions describe `App.jsx` as "one big component", and that is accurate: nearly all state lives in `useState` hooks at the top of `App()` and the incoming-message handler is a single if/else chain.

## 3. How it works

### 3.1 Start-up and sign-in flow

On mount, `App` runs an effect that decides which of three `auth` states to show (`'checking'`, `'login'`, `'ok'`):

1. The token is read by `getAuthToken()`. If the URL has `?token=...` it is written to `localStorage['authToken']` and used; otherwise the stored value is used. This is how an operator can hand out a one-click link such as `https://host/?token=SECRET`.
2. The app fetches `/health` (public, minimal). If `auth_required` is false the state becomes `'ok'` and nothing else happens.
3. If auth is required and a token exists, it calls `GET /auth/check` with `Authorization: Bearer <token>`. A 200 sets `'ok'`. Otherwise the stored token is deleted and a notice "Your saved key is no longer valid" is shown with the login screen.
4. If `/health` itself fails (server down) the app shows itself anyway (`'ok'`), because the app has its own reconnect banner.

`LoginScreen.jsx` is shown for `'login'`. Its `onSubmit` is `signIn(key)` in `App.jsx`, which verifies the key against `/auth/check` before storing it; a typo is reported on the form instead of showing up later as a mysteriously refused WebSocket. Possible results: "Cannot reach the server. Is it running?", "That key was not accepted.", or success (token saved to `localStorage`, `auth = 'ok'`). Sign-out removes the token and returns to the login screen.

The WebSocket authentication is separate: the browser cannot set an `Authorization` header on a WebSocket, so the token is appended as `?token=` on the WebSocket URL. On the server `security.bearer_token()` accepts either the header or the `token` query parameter. The server refuses the handshake for a bad token (before `accept()`), and the client uses that to detect a stale key (see 3.2).

### 3.2 WebSocket client and reconnect policy

When `auth === 'ok'`, an effect keyed on `[sessionId, auth]` builds the URL `ws(s)://<host>/ws/<sessionId>[?token=...]` (scheme follows `window.location.protocol`) and opens it. Behaviour:

- `onopen`: resets the retry counter, sets `connected`, adds a system trace entry.
- `onclose`: sets `connected = false`, releases the microphone and camera locally (the server dropped the voice stream too), and schedules a reconnect with exponential backoff `min(8000, 500 * 2^attempt)` ms, i.e. 500 ms, 1 s, 2 s, 4 s, 8 s, 8 s...
- If the socket has never connected and two attempts have failed, the app calls `/auth/check`; a 401 there clears the stored key and returns to the login screen with "The server did not accept your key". This stops an endless retry loop on a wrong key.
- The effect cleanup sets `closedByUs`, cancels the retry timer, stops voice and sharing, and closes the socket. Changing `sessionId` (new chat or opening a history entry) therefore tears down and reconnects under the new session id.

Messages from the server are JSON text frames. `onmessage` parses each frame and dispatches:

- `type: 'error'` (rate limit, too large, bad JSON from the server's guards): shown as a toast with a friendly string for `rate_limit`, `too_large`, `bad_json`, else `Server: <code>`.
- `type: 'tts_status'`: ignored.
- Everything else goes to `handleIncomingAction(action)`, keyed on `action.action_type`.

The client sends these message types (all of them exist in `agent/server.py`):

| Client message | When | Server handling |
|---|---|---|
| `{"type":"user_text","text":...}` | Enter in the input box or a suggestion chip | `UserTextEvent` |
| `{"type":"interrupt","reason":"ui_barge_in"}` | Stop button or Esc | `InterruptSignalEvent` |
| `{"type":"voice_stream","action":"start"|"stop"}` | Hands-free mic toggle | Switches the socket to continuous PCM mode |
| binary frame (3200 bytes) | Every 100 ms while streaming voice | `AudioChunkEvent(format="pcm_16khz", streaming=True)` |
| `{"type":"tts","enabled":bool}` | Speak toggle, and on every (re)connect | `coordinator.set_tts`; the server answers with `tts_status` |
| `{"type":"video_frame",...}` | Once per second while sharing, and before sending text | `VideoFrameEvent` |

The sender checks `ws.readyState === WebSocket.OPEN` before sending text; if the socket is not open the message is not appended to the chat and a toast says "Not connected yet - reconnecting. Your message was not sent.", so the UI never shows a message as sent when it was not. The typed text stays in the box.

### 3.3 Message handling (`handleIncomingAction`)

The handler also sets `epochPulsing` for 1.5 s whenever an action carries an epoch higher than the snapshot's epoch, which animates the "Epoch #N" badge in the header. Then it branches:

| `action_type` | Client effect |
|---|---|
| `state_snapshot` | Replaces `snapshot` (epoch, intent, slots, in_flight_calls). Any worker card still marked `working` whose `call_id` is no longer in `in_flight_calls` flips to `completed`. This is how completion is learned: there is no explicit "tool finished" action |
| `tool_call` | Adds (or replaces by `call_id`) a worker card. `spawn_agent` cards use the name/role from the arguments; other tools get a friendly role name from `getBotRoleName` (for example `search_flights` becomes "Flight Scout"). For `spawn_agent` it clears the artifact, sets `artifactLoading` and opens the right panel |
| `agent_step` | Updates the card's thought, step and total steps; if the action carries an `artifact` it becomes the workspace artifact (code viewer with copy button) |
| `file_exported` | Appends to the Exports list, shows `preview` as the artifact, toasts "Saved <file>", opens the right panel on the Exports tab |
| `tool_cancel` | Marks the card `cancelled` with the reason; clears the artifact spinner if no artifact arrived |
| `voice_activity` | `speech_start` creates a pending "microphone" bubble tied to `utterance_id` and sets `isSpeaking`; `speech_end` clears it; `barge_in` adds a trace entry; other states are traced |
| `transcript` | With `utterance_id` (streaming voice): partials update the same bubble with a trailing ellipsis and fill `livePartial`; the final replaces it, or deletes the bubble when the text is empty (noise). Without `utterance_id` (push-to-talk or legacy) the oldest pending voice bubble is resolved |
| `audio_out`, `speech_state` | Delegated to `useSpeechPlayer.handleAction` (section 5). A `stopped` state also writes a trace entry with the text the user actually heard |
| `graph_update` | `op === 'full'` replaces nodes and edges; otherwise new nodes (deduplicated by id) and edges are appended |
| `filler` | Appended to chat as an agent message of type `filler` (a Tier 2 acknowledgement) |
| `spoken_response` | Appended to chat as an agent message, rendered through `MarkdownReply` |

Unknown action types are silently ignored, which is what keeps the client forward compatible with new server actions.

Inconsistency found: `agent/schemas/actions.py` defines a `clarification` action (`ClarificationAction`, with a `question` field) and the coordinator uses it when it asks the user whether to change what it is working on. `handleIncomingAction` in `App.jsx` has no branch for it, so on the web UI such a question is dropped silently and the user would never see it. The Android client (`ChatReducer.kt`) does render it as an agent message. The same applies, by design, to `tts_status`, which the web UI ignores.

### 3.4 Worked example: a correction mid-search

The user types "Find flights from Delhi to Mumbai" and, a second later, "make it Chennai".

1. `handleSendMessage` appends the user bubble and sends `user_text`. If camera sharing is on, it first awaits `captureAndSendFrame()` so "this" means what is on screen now.
2. The server answers with a `filler` action ("Let me look...") which renders immediately, then a `tool_call` for `search_flights` under epoch 1. A "Flight Scout" card appears with status `working`, and the trace gets `SPAWNED Flight Scout ... under Epoch 1`.
3. The second message makes the server bump the epoch and cancel the call: a `tool_cancel` arrives. The card turns red with "ABORTED: Cancelled by user input under Epoch 2", the mini robot avatar shows crossed eyes, a rose-coloured tick is added to the timeline, and the header badge pulses because the action epoch is greater than `snapshot.epoch`.
4. A new `tool_call` for Chennai under epoch 2 creates a second card; a `state_snapshot` updates the slots; when the call leaves `in_flight_calls` the card flips to `completed`, and a `spoken_response` shows the final answer.

Nothing on the client decides what to cancel. It only renders what the epoch model on the server decided.

## 4. Microphone capture with an AudioWorklet

Hands-free voice is implemented in `startVoiceStream()` / `stopVoiceStream()` in `App.jsx`.

1. `getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true}})`.
2. `new AudioContext({sampleRate: 16000})`. Browsers may ignore the requested rate, so the worklet resamples if needed.
3. The worklet source is an inline string constant `PCM_WORKLET_SOURCE` turned into a `Blob` URL and loaded with `ctx.audioWorklet.addModule(url)` (this avoids needing a separate served file). It registers a `pcm-capture` processor.
4. `PCMCapture.process()` converts each block to 16 kHz mono by linear interpolation (`ratio = sampleRate / 16000`, keeping the previous sample across blocks so there is no seam), clamps to [-1, 1], scales to Int16, and fills a 1600-sample `Int16Array`. When it is full (100 ms) it transfers the buffer to the main thread with `port.postMessage(buffer, [buffer])`.
5. The main thread's `port.onmessage` sends that ArrayBuffer as a binary WebSocket frame. If `ws.bufferedAmount` exceeds 1,000,000 bytes the frame is dropped rather than queued, since stale audio is useless.
6. The graph is `source -> worklet -> muted GainNode (gain 0) -> destination`. The muted sink keeps the worklet being pulled without playing the microphone back through the speakers.
7. Before the first frame the client sends `{"type":"voice_stream","action":"start"}`. On stop it sends `action: "stop"`, disconnects nodes, stops the tracks and closes the context.

The server side (`agent/server.py`, `agent/multimodal/`) runs Silero VAD and Whisper on this stream; the client only displays the resulting `voice_activity` and `transcript` actions. Because the page requests browser echo cancellation and the agent's voice is played through the same page, the agent's own speech is largely removed from the microphone signal before the server's own echo guard sees it.

Permission errors are reported as toasts: `NotAllowedError` gives a message that tells the user to allow the microphone in site settings; other errors show `Microphone unavailable: <message>`.

## 5. Audio playback and barge-in on the client (`useSpeechPlayer.js`)

The hook is called as `useSpeechPlayer(wsRef, connected)` and returns `{enabled, speaking, toggle, handleAction, flush}`.

How it works:

1. The preference `enabled` is stored in `localStorage['speakReplies']`. A `ref` mirrors it because the WebSocket handler is created once and cannot close over React state.
2. `ensureContext()` lazily creates one `AudioContext` and one `GainNode` connected to the destination, and resumes the context if suspended. `toggle()` calls it inside the click handler, because browsers block audio until a user gesture.
3. On every (re)connect with the preference on, the hook re-announces `{"type":"tts","enabled":true}`, because the server keeps no preference across sessions.
4. An `audio_out` action carries one sentence of base64 PCM16 (`audio_b64`, `sample_rate`). The hook decodes it into a `Float32Array`, makes an `AudioBuffer`, and schedules a `BufferSource` at `max(currentTime + 0.02, nextTime)`, then advances `nextTime` by the buffer duration. Sentences therefore play back to back on the WebAudio clock with no gaps. Sources are tracked in a `Set`.
5. A `speech_state` action drives barge-in:
   - `ducked`: gain goes to `DUCK_GAIN = 0.15` (time constant 10 ms). The user just started speaking; the server will decide whether it was a real interruption.
   - `resumed`: false alarm, gain returns to 1 (time constant 50 ms).
   - `stopped`: `flush()` stops every scheduled source, clears the set, resets `nextTime` and restores gain to 1. `speaking` becomes false.
   - `finished`: `speaking` becomes false.
6. `interruptAgent()` in `App.jsx` (Stop button or Esc key) sends `interrupt` and also calls `speech.flush()` locally, so the speaker goes silent immediately without waiting for the server round trip.

Audio is only played when the user has enabled speaking and the server reported a TTS backend: `ttsAvailable` is set from `/health` (`tts.available`), and the speaker button is hidden otherwise.

## 6. Panels

The right-hand inspector (toggled by the header button labelled "Artifact & Agents") has five tabs (`activeRightTab`: `agents`, `snapshot`, `trace`, `graph`, `exports`; visible labels "Bots Swarm", "Snapshot", "Trace", "Cognitive Graph", "Exports"). The demo recorder locates these controls by their visible text and by the `title` attributes of the mic, speak and camera buttons, so renaming them breaks `scripts/demo/record.py`. It is closed by default and opens automatically when a worker is spawned or a file is exported. The graph tab enlarges the panel to 60 percent of the height.

- **Agents:** one card per `call_id`, with the `MiniBotAvatar` SVG (blue working, green completed, red cancelled, yellow when "watching" a draft), the thought line, step progress and cancellation reason. The workspace (artifact viewer) shows the generated code with a copy button, and a loading state from `spawn_agent` until the artifact arrives.
- **Snapshot:** intent, epoch, slots as key/value pairs, in-flight calls. This is the raw `state_snapshot` the server publishes, so a judge can watch slots change on a correction.
- **Trace:** a `TraceTimeline` plus a scrolling list. `addTrace(type, text, payload)` creates every entry client-side with a timestamp; these are UI events derived from actions, not the server's schema-validated trace file. `TraceTimeline.jsx` places a dot per entry along a time axis (span from first to last event, minimum 1 s), coloured by type; `tool_cancel` dots are larger with a glow, so a cancellation right after a correction is visible at a glance. The component's comment cites a requirement reference (section 5.1) as the reason for it.
- **Graph:** a hand-rolled SVG. Nodes are laid out in three columns by `node_type` (`turn`, `entity`, `artifact`; x = 40 + column * 150, y = 24 + row * 56). Edges are lines; an edge pointing backwards (target x not to the right of source) is drawn in red unless its type is `PRODUCED` or `REFERENCES`, and `SUPERSEDES` and `BRANCHES_FROM` are dashed. Clicking a node shows its `label` and `data` as JSON. There is no force-directed layout, no pan or zoom.
- **Exports:** `ExportsList.jsx`. Each file shows name, size, path, and whether it was opened in an editor on the server machine. Buttons: "Open in VS Code" (navigates to the `editor_uri`, a `vscode://file/...` link that works only when the browser and server are on the same computer), "Download" (fetches `download_path` with the bearer header and hands the browser a blob, because a plain link cannot send the token), and "Path" (copies to the clipboard).

Other UI pieces:

- `Toasts.jsx`: `useToasts()` keeps at most four toasts, each dismissed after 4 s by default; kinds `info`, `ok`, `error`.
- `SuggestionChips.jsx`: six one-click demo prompts (flights, weather, TypeScript code, save and open in VS Code, a 20-second timer, "What does this sign say?" which needs the camera).
- `MarkdownReply.jsx`: renders agent text with `react-markdown` and `remark-gfm`; links open in a new tab with `noopener noreferrer`; raw HTML is rendered as text, never executed.
- `lib/history.js`: a conversation is a server session. `upsert()` stores up to 30 chats and 200 messages each in `localStorage['kairos.history']`, newest first; reopening a chat reconnects with the same session id, so the agent's server-side memory comes back while the server is up. Failures to write storage are swallowed on purpose.
- Camera and screen sharing: `startSharing(kind)` uses `getUserMedia` (ideal 1280x720) or `getDisplayMedia`, draws the video onto a canvas scaled to at most 1024 px wide, encodes JPEG quality 0.7, base64s it and sends a `video_frame` every 1000 ms (skipped when the socket buffer exceeds 1 MB). A "Sharing your camera/screen" chip with a live preview is always visible while on. The server only buffers frames; the vision model runs only when the agent decides a question needs the image.
- Settings dialog: shows the ASR engine description from `/health` (`asr.model`, device, compute type, loaded or loading state) and session id. The "model" selector (`selectedModel`, default `openai/gpt-oss-120b`) is local state only and is never sent to the server, so it is cosmetic.

## 7. Key types and functions

| Name | File | Role |
|---|---|---|
| `App` | `frontend/src/App.jsx` | Entire app state and layout |
| `getAuthToken` | `frontend/src/App.jsx` | Reads `?token=` from the URL (persisting it) or `localStorage` |
| `signIn` / `signOut` | `frontend/src/App.jsx` | Verify against `/auth/check` before storing |
| `handleIncomingAction` | `frontend/src/App.jsx` | Dispatch of every server action |
| `handleSendMessage` | `frontend/src/App.jsx` | Send `user_text`, with a fresh video frame first |
| `interruptAgent` | `frontend/src/App.jsx` | Send `interrupt`, flush local audio |
| `PCM_WORKLET_SOURCE` | `frontend/src/App.jsx` | Inline AudioWorklet: resample to 16 kHz, 100 ms Int16 frames |
| `startVoiceStream` / `stopVoiceStream` | `frontend/src/App.jsx` | Mic graph set-up and tear-down |
| `captureAndSendFrame` | `frontend/src/App.jsx` | Camera and screen frame sampler |
| `useSpeechPlayer` | `frontend/src/hooks/useSpeechPlayer.js` | Gapless playback, ducking, flush |
| `TraceTimeline` | `frontend/src/components/TraceTimeline.jsx` | Time-axis view of trace events |
| `ExportsList` | `frontend/src/components/ExportsList.jsx` | Exported files with authenticated download |
| `LoginScreen` | `frontend/src/components/LoginScreen.jsx` | Access-key form |
| `upsert`, `loadHistory`, `newSessionId` | `frontend/src/lib/history.js` | Local history; session ids look like `web_<random><time>` |

## 8. Configuration

The frontend has no environment variables. Everything is derived from `window.location`:

| Knob | Value | Where |
|---|---|---|
| Dev server port | 5173 | `frontend/vite.config.js` |
| Dev proxy | `/ws` to `ws://localhost:8000` (websocket enabled), `/health` to `http://localhost:8000` | `frontend/vite.config.js` |
| Build output | `frontend/dist`, emptied on each build | `frontend/vite.config.js` |
| Reconnect backoff | 500 ms doubling, capped at 8 s | `App.jsx` |
| Duck gain | 0.15 | `useSpeechPlayer.js` |
| Mic frame size | 1600 samples = 100 ms at 16 kHz | `PCM_WORKLET_SOURCE` |
| Backpressure drop threshold | `bufferedAmount` > 1,000,000 bytes | `App.jsx` |
| Video | 1 fps, max 1024 px wide, JPEG 0.7 | `App.jsx` |
| localStorage keys | `authToken`, `kairos.session`, `kairos.history`, `speakReplies` | various |

Server-side limits that the client has to respect (`MAX_TEXT_MESSAGE_BYTES`, per-session message rate, audio byte rate) live in `agent/settings.py` and `agent/security.py`; see the server chapter.

### Build and development

```
cd frontend
npm ci
npm run build        # writes frontend/dist, served by the backend at "/"
npm run dev          # http://localhost:5173 with HMR, proxies /ws and /health to :8000
```

In production (and in the `Dockerfile`, whose first stage is `node:20-slim`) only `npm run build` matters: the runtime image copies `frontend/dist` and `uvicorn agent.server:app` serves it. The `kairos` launcher (`agent/launcher.py`) only prints a note if `frontend/dist` does not exist; the API still works without it.

A dev-mode caveat found while reading the config: the Vite proxy only forwards `/ws` and `/health`. The sign-in flow also calls `/auth/check` and the export download uses `/exports/...`; neither is proxied, so against `npm run dev` with `AUTH_TOKEN` set those requests go to the Vite server rather than the backend. Use the built bundle served by FastAPI to exercise sign-in and downloads.

## 9. Interactions with other components

- `agent/server.py`: WebSocket endpoint `/ws/{session_id}`, `/health`, `/auth/check`, `/exports/{name}` and static serving of `dist`. The action schemas are in `agent/schemas/actions.py` and are identical to what the Android app parses (see the Android chapter).
- `agent/coordinator.py`: produces every action the UI renders; also owns TTS (`set_tts`) and the `speech_state` ducking and stop signals.
- `agent/multimodal/` and `agent/speech.py`: VAD, Whisper and TTS behind the audio frames and `audio_out`.
- Demo tooling (`scripts/demo/`): loads this same bundle inside an iframe and drives it with Playwright; see the demo chapter.

## 10. Failure modes and guarantees

- Server down at load: the app renders and keeps retrying; a "disconnected" state is shown through `connected`.
- Wrong key: the server refuses the handshake; after two failed first attempts the app probes `/auth/check` and returns to the login screen on a 401.
- Message never shows as sent when the socket is closed; the draft is kept.
- Microphone or camera permission denied: a toast explains how to fix it; nothing else breaks.
- Socket backs up: mic frames and video frames are dropped, not queued.
- The mic and camera are always released on socket close and on unmount.
- Browser autoplay rules: audio is only started from the speak-toggle click or after it was previously enabled, and the context is resumed if suspended.
- Known limits: the stream of `state_snapshot` is the only signal that a call completed, so a card can briefly show `working` until the next snapshot; "model" in settings is not wired to anything; the graph layout is naive and will grow tall for long conversations; history keeps only text, not artifacts or traces; trace entries are UI-side and not persisted.

## 11. Tests that cover it

There are no JavaScript unit tests in the repository (no test runner is configured in `frontend/package.json`). What exists:

- `tests/test_deploy_files.py` asserts CI has a `frontend` job and that the `Dockerfile` copies `frontend/dist`.
- The CI `frontend` job (`.github/workflows/ci.yml`) runs `npm ci` and the production build, so a syntax or import error in any `.jsx` file fails CI.
- `tests/test_server.py`, `tests/test_server_routing.py`, `tests/test_voice_websocket.py` and `tests/test_voice_stream.py` exercise the server side of the same protocol the client uses (text, interrupt, binary PCM and `voice_stream` start/stop).
- `tests/test_no_secrets.py` ensures no key is committed in the frontend tree.
- The demo recorder (`scripts/demo/record.py`) is in practice an end-to-end driver of this UI with a real backend.
