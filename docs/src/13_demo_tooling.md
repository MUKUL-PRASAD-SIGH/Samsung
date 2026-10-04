# Demo Video Tooling (scripts/demo)

## 1. Purpose

`scripts/demo/` is a self-contained pipeline that produces the 6:00, 1920x1080, 60 fps demo video of Kairos. It is tooling, not part of the product: nothing under `agent/` imports it, there are no unit tests for it, and it is not exercised by CI. It exists because the recording machine has no emulator, no KVM and a headless Chrome without an audio device, so a plain screen capture of "a person using Kairos on a Linux desktop and an Android phone" was not possible. The pipeline instead drives the real application and reconstructs the parts that cannot be captured.

The design principle, stated in `scripts/demo/README.md` and repeated in the docstring of `scripts/demo/record.py`, is: everything that demonstrates the agent is real; everything that is merely a stage around it is a mock-up. The sections below separate the two precisely so a judge can tell what the video proves.

The pipeline has five scripts, one HTML page and one injected JavaScript file.

| File | Role |
|---|---|
| `scripts/demo/record.py` | The scripted take: Playwright + Chrome DevTools Protocol (CDP) screencast of the mock desktop hosting the real web app |
| `scripts/demo/overlay.js` | Injected into every frame: WebSocket tap, cursor, ripples, highlight rings, captions, title cards, terminal |
| `scripts/demo/desktop.html` | The mock Linux desktop (wallpaper, panel, dock, window chrome, VS Code window, phone frame) |
| `scripts/demo/make_assets.py` | Builds the fake-microphone clips and relies on a poster for the fake camera |
| `scripts/demo/build_phone_plan.py` | Turns the frames rendered by the Android test `DemoFrames` into a timed plan |
| `scripts/demo/assemble.py` | Converts irregular screencast frames to an exact 60 fps H.264 file with ffmpeg |
| `scripts/demo/mux_audio.py` | Rebuilds the soundtrack from the logged audio and muxes it into the video |
| `android/app/src/test/java/com/samsung/interruptible/DemoFrames.kt` | Opt-in Robolectric test that renders the phone section |

## 2. What is real and what is mocked

This table follows `scripts/demo/README.md` and was checked against the code.

| Part of the video | Real or mock | Evidence in the code |
|---|---|---|
| Everything inside the Kairos window: login, chat, tabs, hands-free voice, spoken replies, camera, settings | Real | `record.py` opens the built web app from `http://localhost:8200/` (the real FastAPI server) in an iframe (`#app`) and drives it with Playwright. Answers come from the live LLM |
| Code export | Real | The server runs `export_artifact`; with `EXPORT_OPEN=1` it invokes `code --reuse-window <path>`. A stand-in `code` shell script on `PATH` only logs the call to `/tmp/kairos-rec/code_opened.log` |
| The exported file shown in "VS Code" and its output | Real contents, mock window | `record.py` reads the real file from `EXPORT_DIR` and runs it with `run_file` when `safe_to_run` approves; only the editor chrome in `desktop.html` is fake |
| Linux desktop, dock, panel, window chrome | Mock | `desktop.html` |
| The `/metrics` numbers in the terminal | Real | `real_get("/metrics")` fetches them live with the bearer token |
| The eval-harness output in the terminal | Replayed from a file | `record.py` reads `eval_out.txt` from the recording directory; the typed command is cosmetic |
| Phone edition | Real UI, replayed server frames | Real Compose UI (`AgentApp`, `AgentController`) under Robolectric, with a fake transport that replays frames recorded from the web session |
| Soundtrack | Reconstructed | `mux_audio.py` places logged Piper PCM and the fake-microphone file on a timeline |
| Microphone and camera input | Simulated | Chrome flags `--use-file-for-fake-audio-capture` and `--use-file-for-fake-video-capture` feed a WAV and a poster video |
| On-screen captions, highlight rings, cursor, ripples | Overlay | `overlay.js` |

Two points deserve emphasis for honesty. First, the phone section is not an on-device recording; the README says so explicitly. Second, the caption "Measured: ack about 0.35 s after you stop, cancel about 1.0 s after speech onset" in the voice scene is a hard-coded string in `record.py`, not a value read from the run. The numbers in the `/metrics` terminal scene, in contrast, come from the live server.

## 3. How the pipeline works

### 3.1 Data flow

```
 make_assets.py --> mic_base.wav, C.npy, (poster.y4m)           [assets/]
                                |
 uvicorn agent.server  <--------+-- record.py (Playwright, headless Chrome)
   (real LLM, real tools)            |  desktop.html  > iframe#app = real web UI
   export_artifact, code stub        |  overlay.js    > cursor, captions, WS tap
                                     v
              frames/NNNNNN.jpg + stamps.json   (CDP screencast, variable rate)
              wsraw.json  (raw WebSocket frames)  audio.json  (Piper PCM + events)
              meta.json (ts0)   events.txt
                 |                        |                         |
                 |                        v                         v
                 |        Robolectric DemoFrames.kt          mux_audio.py
                 |        (replays wsraw into real          (soundtrack.wav)
                 |         Android UI) -> PNGs + plan.json          |
                 |                        |                         |
                 |           build_phone_plan.py -> phone_plan.json |
                 |                        |                         |
                 +--> assemble.py <-------+  (phone frames shown on  |
                      (exact 60 fps)         the desktop mock-up)    |
                          video.mp4 ------------------------------> kairos_demo.mp4
```

All working files live under the recording directory, `KAIROS_REC`, which defaults to `/tmp/kairos-rec`. Note that the Android phone frames do not travel through `assemble.py` directly: `build_phone_plan.py` copies them into `www/phone/`, and `record.py` displays them inside the phone mock-up in `desktop.html`, so they are captured by the screencast like everything else.

### 3.2 Run order

The order below is the one in `scripts/demo/README.md`.

1. Start the demo server. Create `/tmp/kairos-rec/{bin,exports}`, write a stand-in `code` script into `bin/` that appends `date code <args>` to `code_opened.log`, then run uvicorn on port 8200 with `PATH` including that `bin`, `AUTH_TOKEN=athena-kairos-2026`, `EXPORT_DIR=/tmp/kairos-rec/exports` and `EXPORT_OPEN=1`.
2. Run `python make_assets.py` to create the microphone clips and fake-camera assets in `/tmp/kairos-rec/assets`.
3. Produce the phone frames. Record the web scenes once (`python record.py --only flights,correction,code`), copy `/tmp/kairos-rec/wsraw.json` to `android/demo-data/wsraw.json`; repeat with `--only voice` and copy to `android/demo-data/wsraw_voice.json`. Then run the Android Docker image with `DEMO_DATA=/workspace/demo-data` and the `*DemoFrames*` test filter, and finally `python scripts/demo/build_phone_plan.py`.
4. Make the take: `python scripts/demo/record.py` (needs `playwright`, `numpy` and Chrome; takes six minutes and the machine must stay idle because screencast timing is wall-clock).
5. Assemble: `python scripts/demo/assemble.py /tmp/kairos-rec/video.mp4 360`.
6. Mux the soundtrack: `python scripts/demo/mux_audio.py /tmp/kairos-rec/video.mp4 /tmp/kairos-rec/kairos_demo.mp4 360`.

Between takes, empty the export folder, because `export_artifact` never overwrites (the README refers to `prime-1.py`, the name a second export would receive). A full take costs roughly 60k LLM tokens according to the project notes, so check `events.txt` for `clarification` actions (the symptom of an exhausted Groq daily budget) before accepting one.

## 4. Component details

### 4.1 `record.py`: the scripted take

**Staging.** `stage()` copies `desktop.html` and `frontend/public/kairos.svg` into `www/`, and starts `python -m http.server` on port 8301 (bound to 127.0.0.1) if that port is not already listening. `build_mic()` builds the final microphone file `mic.wav`: it loads `assets/mic_base.wav` and overwrites a window with the clip `assets/C.npy` at offset `CLICK_C_AT = TALK_OVER_AT - MIC_ON_AT` seconds. This is how the "user talks over the agent" moment is made to coincide with the agent speaking. `TALK_OVER_AT` defaults to 216.0 and can be overridden by an environment variable of the same name; `MIC_ON_AT` is the constant 173.6, the time at which the voice scene clicks the microphone button.

**Browser setup.** `main()` launches `/usr/bin/google-chrome-stable` (hard-coded path) headless with fake media flags (`--use-fake-ui-for-media-stream`, `--use-fake-device-for-media-stream`, the fake audio file `mic.wav%noloop` and the fake video file `assets/poster.y4m`), autoplay allowed, GPU raster flags, and several Chrome features disabled so a page on `localhost:8301` may talk to `localhost:8200`. The viewport is 1920x1080 at device scale factor 1. The overlay script is registered with `add_init_script`, so it runs in every frame. A route handler `strip_xfo` fetches every response from `http://localhost:8200/**` and removes the `X-Frame-Options` header before fulfilling it. This is necessary because the production server sends `X-Frame-Options: DENY` (see `agent/security.py`), and the mock desktop embeds the app in an iframe on purpose. Only the recording browser sees the modified headers; the server's behaviour is untouched.

**The recorder class.** `Rec` wraps the page and a CDP session. `start()` calls `Page.startScreencast` with JPEG quality 92 at up to 1920x1080 and `everyNthFrame=1`; `on_frame` writes each frame to `frames/NNNNNN.jpg`, appends `(timestamp, filename)` to `stamps`, and acknowledges the frame. `stop()` dumps `stamps.json`. Helper methods (`move`, `click`, `type`, `send`, `hl`, `cap`, `at`) call functions of the `__rec` object defined in `overlay.js` and sleep for the animation duration. `Rec.at(t)` is the key scheduling primitive: it sleeps until the video timeline reaches `t`, so scenes are pinned to absolute times.

**Scenes.** A `@scene(name, start)` decorator registers an async function with its start time. In a full take, the main loop waits (`R.at(start)`) and runs each scene in order; an exception in a scene is printed (`!! scene ... failed`) and the take continues. The scenes and their start times in seconds:

| Scene | Start | What happens |
|---|---|---|
| `title` | 0 | Title card via `__rec.title` |
| `desktop` | 6 | Icon highlight, simulated launch (`__desk.launch`), wrong access key then the right key (`WRONG = "kairos-demo"`, `TOKEN` from `AUTH_TOKEN`) |
| `flights` | 42 | "Find flights from Delhi to Mumbai"; opens the Artifact and Agents panel and the Snapshot, Trace and Cognitive Graph tabs |
| `correction` | 68 | Books a flight, waits for the `tool_call` WebSocket event, then types "No wait, change it to Goa"; shows epoch bump; asks for hotels "there"; presses Escape; shows graph |
| `code` | 98 | Asks for a prime-number function saved as `prime.py` and opened in VS Code; waits for `file_exported`; switches to the mock editor showing the real file and running it |
| `timer` | 150 | Sets a 12 second timer, asks weather meanwhile, sets a 60 second timer and cancels with Escape |
| `voice` | 173 | Clicks hands-free mic; the fake microphone speaks a booking and then a correction |
| `speak` | 205 | Turns on spoken replies; clip C talks over the reply; reads the `speech_state` `stopped` event and quotes the `spoken_text` the user actually heard |
| `vision` | 235 | Shares the poster camera and asks for flights "to the city on this poster" |
| `settings` | 252 | Opens Settings and closes it |
| `metrics` | 258 | Terminal overlay: live `/metrics` lines and eval output |
| `phone` | 272 | Shows the Android frames on the phone mock-up following `phone_plan.json` |
| `closing` | 340 | Closing card, ends at 359 s |

**Waiting on the real system.** Scenes synchronise on real WebSocket events rather than fixed sleeps where it matters. `ws_mark()` returns the length of `window.__ws` in the app frame, and `wait_ws(pred, since, timeout)` polls that list for the first event after the mark that satisfies a JavaScript predicate such as `x=>x.type==='file_exported'`. This is why the correction scene says the correction is sent "only once the booking call is really in flight". If an expected event never arrives (for example because the LLM quota is exhausted), the scene continues on its timeline and the take degrades silently; the only symptoms are printed warnings (`!! no file_exported event`) and `clarification` actions in `events.txt`.

**Safe execution of exported code.** In the code scene, the exported file is run with the host Python only if `safe_to_run(src)` passes. It rejects sources matching `open`, `exec`, `eval`, `__import__`, `input`, `os`, `subprocess`, `socket`, `shutil`, `pathlib` or `requests` followed by a call or attribute access, and requires every imported top-level module to be in `SAFE_IMPORTS` (`math`, `sys`, `unittest`, `typing`, `itertools`, `functools`, `re`, `collections`, `doctest`, `string`). `run_file` executes it with a 10 second timeout in the export directory. This is a heuristic guard for the recording, not a sandbox.

**Dry runs.** `--only scene1,scene2` records only those scenes. If the first selected scene is not `title` or `desktop`, the script logs in on its own, and for most later scenes also sends an initial flight query and opens the panel so the UI is in a plausible state. `t0_scene` offsets the timeline so scenes keep their absolute times.

**Outputs.** After the scenes, `record.py` reads logs from the app iframe: `window.__wsraw` (raw frames) and `window.__audio` (Piper chunks) and `window.__micStart`, then writes `wsraw.json`, `audio.json`, `meta.json` (`ts0`, the first screencast timestamp) and `events.txt` (a human-readable event list on the video timeline).

### 4.2 `overlay.js`: instrumentation and visuals

The script has two roles. In both the top frame and the app iframe it wraps `window.WebSocket` so every message is recorded. In the top frame only (`window.top === window`) it also draws the overlay.

The WebSocket tap stores three things. `window.__ws` holds summaries (time, type taken from `action_type` or `type`, state, reason, epoch, text, `spoken_text`, tool name, filename) used by `wait_ws`. `window.__wsraw` holds the raw text frames with direction, with `audio_b64` blanked and replaced by a length so the file stays small. `window.__audio` collects the full `audio_out` payloads (sample rate, base64 PCM, utterance id, sequence number) for the soundtrack. It also forwards a summary to the parent via `postMessage` (the `__recEvent` message the live event HUD uses). A patched `getUserMedia` records `window.__micStart` when the page asks for audio, because the fake microphone file starts playing at that moment.

The visual layer (injected only in the top frame) draws the progress bar, cursor, click ripples, highlight rings with labels, captions, title cards, an event stream HUD and a small terminal. The `record.py` helpers call into it through the `__rec` object (`cursorTo`, `ripple`, `hl`, `hlOff`, `cap`, `title`, `titleOff`, `hud`, `termShow`, `termType`, `termLine`, `termClear`).

### 4.3 `desktop.html`: the mock desktop

A single static page, 1920x1080, with a wallpaper, top panel, desktop icon, dock, a Kairos window `#kw` (which contains the real app in an iframe `#app` with a splash screen), a VS Code-like window, a terminal and a phone mock-up. It exposes a `window.__desk` object (for example `launch`, `maximize`, `hideApp`, `showApp`, `openEditor`, `closeEditor`, `termType`, `phoneList`, `phoneOn`, `phoneLaunch`, `phoneStep`, `phoneShow`, `phoneTapRect`, `scrRect`, `tapAt`, `phoneOff`) that `record.py` calls through `page.evaluate`. The editor window does simple syntax highlighting of the real exported source. None of this page contains application logic; it is purely staging.

### 4.4 `make_assets.py`: microphone clips

It loads two existing test fixtures, `tests/fixtures/audio/book_flight.wav` ("clip A") and `tests/fixtures/audio/correction_goa.wav` ("clip B"), resamples them to 48 kHz mono through ffmpeg, and synthesises a third user utterance (clip C, "No wait, show me hotels in Pune instead.") with `PiperTTSBackend` from `agent/multimodal/tts.py`, the same engine the agent uses to speak. It lays A at 3.0 s and B after A plus a 2.4 s gap onto a 100 second silent buffer (`mic_base.wav`), saves C separately (`C.npy`) so `record.py` can splice it in at the talk-over time, and writes `mic_meta.json` with the timings. The script has hard-coded absolute paths (`sys.path.insert(0, "/home/varshith/Samsung")` and `/tmp/kairos-rec/assets/...`), so it only works unchanged on the original recording machine. It does not create `poster.y4m`, even though `record.py` passes that file to Chrome; the README says only that the assets step produces "microphone clips + poster", and I did not find the code that generates the poster video, so that step cannot be reproduced from the repository alone.

### 4.5 `DemoFrames.kt` and `build_phone_plan.py`: the phone section

`DemoFrames.kt` is a Robolectric test class with one test, `renderThePhoneSection`. It is opt-in: `enabled` is true only when the environment variable `DEMO_DATA` is set, otherwise `assumeTrue` skips it, so a normal test run does not render video frames. It builds a real `AgentController` with a `FakeTransport`, `FakeSpeech`, `FakeMic`, `FakeStore` and `FakeAuthApi` (the shared fakes from `Fakes.kt`), renders the real `AgentApp` composable at 411 x 891 dp, and performs real UI actions (typing into the real text field, tapping the real Send button and tabs). For each user message, the `answers(...)` helper finds the matching outgoing frame in `wsraw.json` and takes the inbound frames that followed it. `feedThrough` then replays them into `rig.receive` up to a chosen frame type (`filler`, `spoken_response`, `tool_cancel`, `file_exported` and so on) so a screenshot can be taken at each interesting moment. Audio frames (`audio_out`, `tts_status`) are filtered out. The voice section uses `wsraw_voice.json` if it exists. The `shot(...)` helper draws the activity's decor view into a bitmap, writes `app/build/demo/NN_name.png`, and appends a plan line (image, tap position, caption, bullet index, hold seconds).

The phone section therefore proves that the real Android UI renders real server frames correctly. It does not prove on-device behaviour such as audio latency or camera access (see the Android chapter for unverified items). One inconsistency: the class comment says the test is skipped unless `demo-data/wsraw.json` exists, but the actual gate is the `DEMO_DATA` variable; the missing-file case is handled by a second `assumeTrue`.

`build_phone_plan.py [t_start=279.4] [t_end=337.8]` reads `android/app/build/demo/plan.json`, copies the PNGs to `www/phone/` under the recording directory, and rescales each step's `hold` so the whole sequence exactly fills the interval from `t_start` to `t_end` on the video timeline. It writes `phone_plan.json` with the step list, six fixed bullet strings (sign-in, chat, correction, State/Trace/Graph, export, hands-free voice) and an end time of `t_end + 1.2`. The phone scene in `record.py` plays that plan, moving the overlay cursor to the tap position stored in each step (converted from fractions to screen pixels using `__desk.scrRect()`).

### 4.6 `assemble.py`: exact 60 fps

The CDP screencast delivers frames only when the page changes, with irregular timestamps, so `assemble.py` resamples them. Arguments: output path (default `demo.mp4` in the recording directory) and duration (default the total span of the stamps). It computes `N = round(dur * 60)` output frames; for output frame `i` it picks the latest captured frame whose timestamp is at or before `ts0 + i/60`. Runs of the same source frame are collapsed into one entry with a `duration` line in an ffmpeg concat list (`concat.txt`), with the last file repeated because of a concat demuxer quirk (commented in the code). ffmpeg then encodes with libx264, CRF 14, `-preset medium`, `-tune animation`, yuv420p, BT.709 colour tags, `fps=60` and a Lanczos scale to 1920x1080, truncated to the requested duration. The result is a constant 60 fps file regardless of how irregular the capture was.

### 4.7 `mux_audio.py`: rebuilding the soundtrack

Because headless Chrome has no audio device, nothing was heard during recording. The script reconstructs what a user would have heard from `audio.json` and `meta.json` (`ts0`). Arguments: the video, the output file and an optional duration (default 360 s).

1. Allocate a mono float buffer at 48 kHz.
2. Place the user's voice: if `micStart` was logged, put `mic.wav` (scaled by 0.9) at that time relative to `ts0`.
3. Place the agent's voice: sort the logged `audio_out` chunks by arrival time, decode each base64 PCM chunk (resampled to 48 kHz by linear interpolation), and replay the client's playback queue. A chunk starts at `max(arrival, end of previous chunk)`, so chunks play back to back.
4. Emulate barge-in: `speech_state` events with state `stopped` cut the audio at that instant with a linear fade-out; chunks queued behind a flushed one are dropped (the `flushed_until` logic). `ducked` events attenuate the gain to 0.3 until `resumed`, `started`, `stopped` or `finished`.
5. Normalise so the peak is at most 0.95, write `soundtrack.wav`, and mux it with the already-encoded video using ffmpeg (`-c:v copy`, AAC 192 kbit/s at 48 kHz).

The reconstruction mirrors the real client behaviour (queue, duck, flush on stop) described in the web frontend chapter, but it is an approximation: for example the duck gain of 0.3 is a constant in this script and not read from the client, so the video's loudness during ducking is indicative only.

## 5. Key types and functions

| Name | File | Role |
|---|---|---|
| `stage`, `build_mic` | `scripts/demo/record.py` | Serve `desktop.html` on port 8301; build `mic.wav` with clip C spliced at the talk-over time |
| `Rec` | `scripts/demo/record.py` | CDP screencast capture, absolute-time scheduling (`at`), cursor/caption helpers |
| `scene` (decorator) | `scripts/demo/record.py` | Register a scene with its absolute start time |
| `wait_ws`, `ws_mark` | `scripts/demo/record.py` | Wait for a real WebSocket event after a mark |
| `safe_to_run`, `run_file` | `scripts/demo/record.py` | Heuristic guard and runner for the exported code shown in the mock terminal |
| `strip_xfo` | `scripts/demo/record.py` | Remove `X-Frame-Options` for the recording browser only |
| `window.__ws`, `__wsraw`, `__audio`, `__micStart` | `scripts/demo/overlay.js` | In-page logs of WebSocket events, raw frames, audio and mic start time |
| `window.__rec` | `scripts/demo/overlay.js` | Cursor, captions, highlights, title cards, terminal |
| `window.__desk` | `scripts/demo/desktop.html` | Programmable mock desktop |
| `renderThePhoneSection` | `android/app/src/test/java/com/samsung/interruptible/DemoFrames.kt` | Render phone frames from replayed server frames |
| `place`, `gain_at` | `scripts/demo/mux_audio.py` | Mix audio into the timeline; ducking gain |
| (module script) | `scripts/demo/assemble.py` | Variable-rate frames to constant 60 fps |
| (module script) | `scripts/demo/build_phone_plan.py` | Scale phone frame holds to the time window |

## 6. Configuration

| Variable or argument | Default | Used by |
|---|---|---|
| `KAIROS_REC` | `/tmp/kairos-rec` | `record.py`, `assemble.py`, `mux_audio.py`, `build_phone_plan.py`: working directory |
| `AUTH_TOKEN` | `athena-kairos-2026` | `record.py` (access key typed into the login screen and bearer for `/metrics`); must match the server's `AUTH_TOKEN` |
| `EXPORT_DIR` | `$KAIROS_REC/exports` in `record.py` | Where `record.py` reads the exported file; must match the server's `EXPORT_DIR` |
| `TALK_OVER_AT` | `216.0` | Time (video seconds) when clip C is played over the agent |
| `DEMO_DATA` | unset | Android test: directory containing `wsraw.json` and `wsraw_voice.json`; its presence enables `DemoFrames` |
| `--only a,b` | all scenes | `record.py` dry run of selected scenes |
| `assemble.py` args | output `demo.mp4`, duration from stamps | Output file, duration in seconds |
| `mux_audio.py` args | duration 360 | Input video, output file, duration |
| `build_phone_plan.py` args | `279.4`, `337.8` | Start and end time of the phone section in the video |
| Server: `EXPORT_OPEN`, `EXPORT_DIR`, `AUTH_TOKEN` | see the server chapter | The demo server must run with `EXPORT_OPEN=1` so that the stand-in `code` is invoked |

Fixed constants worth knowing: ports 8200 (server) and 8301 (desktop page server), Chrome path `/usr/bin/google-chrome-stable`, `MIC_ON_AT = 173.6`, and the `RATE = 48000` audio sample rate.

## 7. Interactions with other components

- Server (`agent/server.py`, `agent/security.py`): the take needs the bearer `AUTH_TOKEN`, a writable `EXPORT_DIR`, and tolerates the `X-Frame-Options: DENY` header only because the recording browser strips it. `/metrics` is read with the bearer token.
- Exporter (`agent/exporter.py`): exports never overwrite, so a repeat take requires an empty export folder; the exporter launches `code` through an argument list, which is how the stand-in script on `PATH` intercepts the call.
- Web frontend (`frontend/`): the recording script locates elements by their visible text, `title` attributes and placeholders (for example `What would you like to know?`, `Access key`, `Hands-free voice`, `Artifact & Agents`). A UI text change can silently break a scene, which then fails with a printed warning and continues.
- Voice stack (`agent/multimodal/`, `agent/speech.py`): the voice and speak scenes only work if Whisper, Silero VAD and Piper are installed; the `speech_state` and `audio_out` actions produced there are what `mux_audio.py` consumes.
- Android app (`android/`): `DemoFrames.kt` reuses the production composables and the test fakes. It parses the recorded frames with the production `Protocol.parse`, so recording and replay also exercise the protocol contract.
- Eval harness (`agent/eval/`): the metrics scene shows eval output, but from a saved `eval_out.txt` and not from a live run.

## 8. Failure modes and guarantees

There are no behavioural guarantees: this is best-effort tooling, and the README itself recommends checking each take.

- LLM budget exhausted: Groq `openai/gpt-oss-120b` has a 200k tokens/day cap and a take costs about 60k tokens. Replies degrade to "I hit a snag reaching my reasoning engine" and `clarification` actions appear in `events.txt`. Reject such a take.
- Expected event never arrives: `wait_ws` times out and returns nothing; the scene proceeds on its fixed timeline and the video shows the wrong state under a caption that assumes the right one.
- Scene exceptions are caught and printed, so a take always completes but may be wrong; read the log for `!! scene ... failed`.
- Machine not idle: scenes are pinned to wall-clock absolute times through `perf_counter`, so load can make an action late relative to its caption.
- Missing inputs: the `metrics` scene opens `eval_out.txt` and the `phone` scene needs `phone_plan.json`. The phone scene checks for the plan and skips with a printed warning; the metrics scene has no such check and would raise, which is caught and printed as a scene failure. I found no script in the repository that creates `eval_out.txt`; presumably it is the saved output of `python -m agent.eval --llm mock --virtual --set all --fail-under 97` (the command typed on screen) captured by hand.
- Non-portable paths: `make_assets.py` and `record.py` contain absolute paths specific to the original machine (`/home/varshith/Samsung`, `/usr/bin/google-chrome-stable`, `/tmp/kairos-rec`).
- Stale Gradle cache: when the Android frames come from new `demo-data`, run with `cleanTestDebugUnitTest` and `--no-build-cache`, as in the README command, otherwise Gradle may replay an old result because the data directory is not a Gradle input.
- Exported file running on the host: `safe_to_run` is a regex heuristic. Treat generated code as untrusted.
- Voice and audio accuracy: the soundtrack is reconstructed from logs; timing is faithful to arrival and stop events but the mix is an approximation of what a real device would play.

## 9. Worked example: the correction scene end to end

This traces what happens when the `correction` scene runs (scheduled at 68 s) and how it feeds the later phone section.

1. At `R.at(68)` the scene function `s_corr` starts. It clicks the Trace tab through the overlay cursor (`R.click` moves the cursor, plays the ripple, then calls Playwright `click`).
2. It calls `ws_mark()`, which returns the current length of `window.__ws` in the app iframe, say 41.
3. `ask("Book a flight from Delhi to Mumbai", 40)` types the text at 40 ms per key and presses Enter. The real web app sends a user text frame to the real server, which runs the coordinator, tags the booking tool call with the current epoch and emits a `tool_call` action.
4. `wait_ws("x=>x.type==='tool_call'", m, 8)` polls `window.__ws.slice(41)` every 250 ms until the `tool_call` summary appears (up to 8 s), then the script waits 0.4 s so the call is genuinely in flight.
5. The script types "No wait, change it to Goa" into the input and presses Enter. On the server this is the correction path: the epoch is bumped, the in-flight Mumbai call is cancelled and stale results are dropped. The overlay highlights the epoch counter and shows the caption explaining it.
6. `wait_ws("x=>x.type==='spoken_response'", m + 1, 16)` waits for the corrected answer; the script then opens the Snapshot tab at 86 s to show `Goa` in the destination slot.
7. Everything the server sent during this scene is also in `window.__wsraw`. At the end of the take `record.py` dumps it to `wsraw.json`. After copying it to `android/demo-data/`, `DemoFrames.kt` finds the outgoing frame whose text starts with "No wait, change it to Goa", replays the inbound frames up to `tool_cancel` into the Android controller and takes the screenshot named `cancelled`, captioned "corrected mid-flight: the stale call is cancelled".
8. `build_phone_plan.py` stretches that frame's 2.4 s hold to fit the 58.4 s phone window, and the phone scene shows it at its slot.

So the same real server frames appear twice in the video, once in the web UI at 68 s and once on the Android UI near 300 s, and the mock desktop only surrounds them.

## 10. Tests that cover it

None of the scripts in `scripts/demo/` has a dedicated test, and CI does not run them. Related coverage is indirect:

- `tests/test_imports.py` imports every module under `agent/`; it does not cover `scripts/demo/`. The CI lint command `ruff check agent tests scripts` (rules E9 and F only) does include `scripts/demo/*.py`, so syntax errors and undefined names would be caught there.
- `android/app/src/test/java/com/samsung/interruptible/DemoFrames.kt` is skipped in normal Gradle runs (needs `DEMO_DATA`); it is counted as one test in the Android chapter.
- The protocol that the replayed frames follow is covered by `tests/test_android_fixtures.py` and the Kotlin `ProtocolTest.kt`.
- The server behaviours the demo relies on (exports, auth, security headers) are covered by the server and tool tests described in the other chapters.
