<p align="center"><img src="frontend/public/kairos.svg" width="96" alt="Kairos"></p>

# Kairos · Καιρός

*Kairos* is the Greek word for the right, opportune moment: the instant when acting matters. This is a full-duplex,
interruptible real-time agent (Samsung Hackathon, Theme 05): talk over it, correct it mid-task, and it adapts instead of finishing the
wrong thing.

## Download

Ready-made apps are on the [release page](https://github.com/V4RSH1TH-R3DDY/Kairos/releases/tag/PRISM_GENAI_HACKATHON_Y2026): no Python, Node or terminal needed.

| Platform | Download | Notes |
|---|---|---|
| **Windows 10 / 11** | [`Kairos-Setup-1.0.0.exe`](https://github.com/V4RSH1TH-R3DDY/Kairos/releases/download/PRISM_GENAI_HACKATHON_Y2026/Kairos-Setup-1.0.0.exe) (installer) or [`Kairos-Windows-portable-1.0.0.zip`](https://github.com/V4RSH1TH-R3DDY/Kairos/releases/download/PRISM_GENAI_HACKATHON_Y2026/Kairos-Windows-portable-1.0.0.zip) | Runs the whole agent on your PC. On first run it asks for your Groq / OpenRouter API key. Details: [Windows app](#windows-app-download-and-run). |
| **Android** | [`Kairos-Android-1.0.0.apk`](https://github.com/V4RSH1TH-R3DDY/Kairos/releases/download/PRISM_GENAI_HACKATHON_Y2026/Kairos-Android-1.0.0.apk) | A remote for a running Kairos: the agent itself runs on a computer, so install the Windows app (or run from source) first, then point the phone at it. Details: [Android app](#android-app). |

Both builds are unsigned (Windows SmartScreen shows "unknown publisher"; Android asks you to allow installing from your browser). The
phone and the computer must be on the same network, and the computer must be started with `--host 0.0.0.0` so the phone can reach it.
Prefer to run from source? Continue below.

## For judges: setup in 3 steps

Needs Python 3.10-3.12 (3.13+ is not supported by the speech dependencies) and, to build the web UI, Node.js 18+. No API key is required.

```bash
./setup.sh            # Windows: setup.bat. Creates .venv, installs everything, builds the UI, downloads models, runs a self-check
.venv/bin/kairos      # Windows: .venv\Scripts\kairos. Starts the server and opens Kairos in its own window
```

Then, to use a real model, put `GROQ_API_KEY=...` (or `OPENROUTER_API_KEY=...`) in `.env` and restart.

* **No key?** Kairos runs in an offline mock mode (canned replies, the real coordination logic), enough to see interruptions,
  epochs and cancellation work.
* **Options:** `./setup.sh --quick` skips model downloads (fetched on first use), `--no-frontend` skips the UI build,
  `--check` only reports what is installed.
* **No Python or Node?** `docker compose up --build`, then open http://localhost:8000 (see [Deployment](#deployment)).
* **Verify:** `.venv/bin/python -m agent.eval --llm mock --virtual --set all --fail-under 97 --min-scenario 95` should end with
  `GATE PASSED`; `.venv/bin/python -m pytest -q` runs the unit tests.
* **Trouble?** The microphone only works on `localhost` or HTTPS; port 8000 busy: `kairos --port 8001`; the troubleshooting table is in
  `docs/01_setup_and_quickstart.docx`.

## Windows app (download and run)

No Python, Node or terminal needed: download **`Kairos-Setup-<version>.exe`** from the [Releases](../../releases) page, run it, and
start Kairos from the Start menu. (A portable `Kairos-Windows-portable-<version>.zip` is attached too: unzip and run `Kairos.exe`.)

* **First run:** Kairos opens in its own window and asks for your **Groq** and/or **OpenRouter** API key. It checks the key with
  the provider, then stores it on your computer only (`%APPDATA%\Kairos\keys.json`). Change or remove keys any time under
  Settings → API keys. "Skip" runs the offline demo mode.
* **First start downloads** the speech-recognition model (about 140 MB, when you first use voice) and the speaking voice (about 60 MB), then works offline except for the LLM calls.
* The installer is unsigned, so Windows SmartScreen may say "unknown publisher": choose *More info → Run anyway*.
* A small console window stays open while Kairos runs; close it to quit.
* **To use it from your phone** (Android app): start it with `Kairos.exe --host 0.0.0.0` (from a terminal in the install folder). It then
  prints an access key to type into the phone, and Windows may ask to allow it through the firewall.
* The desktop build omits PyTorch to stay small, so interruptions are detected by the keyword classifier rather than the MiniLM
  one (the Docker and `pip` installs keep MiniLM).
* Build it yourself: `.github/workflows/build-desktop.yml` (runs on version tags or from the Actions tab), or locally with
  `pip install -e ".[tts,build]"` then `pyinstaller packaging/kairos.spec` (see `packaging/`).

## Documentation

Detailed documentation of every component is in [`docs/`](docs/) as `.docx` files (Markdown sources in `docs/src/`, rebuilt with
`python docs/build_docs.py`, which needs `pip install python-docx`). Start with `00_judges_guide.docx`; `Kairos_Complete_Documentation.docx`
combines all chapters.

| File | Covers |
|---|---|
| `00_judges_guide` | what Kairos is, what to try, document map |
| `01_setup_and_quickstart` | install paths, LLM choice, verification, troubleshooting |
| `02_coordination_core` | epoch model, session state, idempotency, tool router, virtual clock |
| `03_coordinator_event_flow` | coordinator, event/action queues, interrupts, trace |
| `04_fast_and_slow_path` | Tier 1 interrupt classifier, Tier 2 fillers, Tier 3 planner |
| `05_llm_layer` | backends, circuit breaker, deadlines, fallback, mock mode |
| `06_memory` | scratchpad, graph memory, context builder |
| `07_tools_and_workers` | every tool and worker |
| `08_voice_vision_speech` | VAD, Whisper, Piper, barge-in, echo guard, vision |
| `09_server_security_deployment` | HTTP/WebSocket protocol, limits, Docker, CI, env-var reference |
| `10_evaluation_and_testing` | eval harness, scoring, CI gate, pytest suite |
| `11_web_frontend`, `12_android_app`, `13_demo_tooling` | the clients and the demo-video tooling |

### Other ways to start

```bash
kairos --login                              # require an access key, to see the sign-in screen
kairos --install-shortcut                   # (Linux) adds a Kairos icon to your application launcher
python scripts/setup.py --check             # what is installed / missing
```

`kairos` is loopback-only with no key by default (your machine), generates and requires an access key as soon as you serve a network
(`--host 0.0.0.0`), reuses an instance that is already running, and tells you what is missing (LLM key, UI build) instead of failing.

What it can do: travel search and booking, weather, **write code and export it into VS Code**, timers (cancellable), look at your
camera, hands-free voice you can talk over, spoken replies, and a live view of its own state, trace and memory graph.

## Architecture

A user talks or types; the agent classifies interruptions, plans with an LLM, calls tools, and
streams every step to a React UI. The core is an **epoch model**: each session has a monotonically
increasing epoch, every dispatched tool call is tagged with the epoch it started under, and a
correction bumps the epoch and cancels stale work.

```
Browser (React/Vite)                    FastAPI + WebSocket  (/ws/{session_id})
 ├ text input ───────────────┐           ┌──────────────────────────────────────────────┐
 ├ hands-free mic (AudioWorklet,  │       │ EVENT QUEUE  ← user_text / audio / interrupt / tool_result
 │   16 kHz PCM, 100 ms frames) ──┼──────►│                                              │
 └ (camera/screen frames)     │       │ Tier 1  interrupt classifier (keyword mode)  │
                                  │       │ Tier 2  fast-path fillers / acks (templates) │
 ◄── actions (JSON over WS) ──────┘       │ Tier 3  Planner → LLM (Groq gpt-oss-120b)    │
   filler · spoken_response · tool_call   │           ├ tool router (schemas, read/state)│
   tool_cancel · state_snapshot           │           ├ SessionState (epoch, slots,      │
   agent_step · graph_update              │           │   in-flight calls, idempotency)  │
   transcript · voice_activity            │           └ 2-tier memory (scratchpad+graph) │
                                          │ Voice: streaming Silero VAD → Whisper base.en│
                                          │ Workers: dynamic "spawn_agent" artifacts     │
                                          │ ACTION QUEUE → TraceLogger (validated) → WS  │
                                          └──────────────────────────────────────────────┘
```

## Spoken replies (full duplex)

The agent can speak. Install Piper (`pip install -e .[tts]`) and download a voice (commands in `.env.example`), restart the
backend, and the UI shows a speaker button. Replies are synthesized sentence by sentence (faster than real time on CPU) and
sent as `audio_out` actions; the browser plays them through WebAudio.

* **Barge-in:** the voice ducks when you start talking and stops at your first real words (or instantly on a typed message, a
  correction, or an interrupt). The server records what was actually heard (`speech_state: stopped`, `spoken_text`) and memory
  shows the model `... [interrupted by the user]`, so it never assumes you heard the part you cut off.
* **Echo:** the browser's echo canceller handles most of it; additionally the VAD bar is raised while the agent talks and any
  transcript that is mostly the agent's own recent words is discarded. Verified with real Piper audio looped into the mic.
* Check it in a real browser with `python scripts/ui_tts_check.py`. Evals: scenarios `speak_barge_in` / `speak_plain`.

## Quickstart

### Backend

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   ·   macOS/Linux: source .venv/bin/activate
pip install -e .

cp .env.example .env   # fill in GROQ_API_KEY and/or OPENROUTER_API_KEY

uvicorn agent.server:app --host 0.0.0.0 --port 8000 --reload
```

`/health` reports readiness (including ASR warm-up progress). If `frontend/dist/` exists the
server serves the built React app at `/`; otherwise it falls back to a minimal inline demo page.

### Frontend

```bash
cd frontend
npm ci
npm run dev        # http://localhost:5173, proxies /ws and /health to :8000
# or, to serve the built app straight from the backend:
npm run build       # writes frontend/dist/
```

### Tests

```bash
pytest -q                 # network-dependent "live" tests are excluded by default
pytest -q -m live          # run the live-provider tests too (needs GROQ_API_KEY / OPENROUTER_API_KEY)
```

### Evaluation harness

```bash
python -m agent.eval --llm mock                      # deterministic, scores the coordination layer
INTENT_EMBEDDINGS=0 python -m agent.eval --llm mock --virtual  # skip the ~10 s MiniLM load for a quick run
python -m agent.fast_path.calibrate --errors        # refit + report the interrupt detector on the held-out split
python -m agent.eval --llm mock --virtual               # virtual-time event loop: whole suite in <1s (skips voice)
python -m agent.eval --llm live --set holdout           # the hold-out set (different phrasings/situations); look at sparingly
python -m agent.eval --llm live --set all --runs 3      # mean/std/worst per scenario, flaky flags, dev-vs-hold-out gap
python -m agent.eval --llm live --out report.json    # real LLM, paced to a TPM budget (~10 min)
python -m agent.eval --llm mock --tag interrupt      # subset by tag: task|interrupt|safety|fault|voice|context
```

Results are written to `eval_results/`.

## Android app

A native Kotlin/Compose client (`android/`) with chat, hands-free voice and barge-in, spoken replies, camera sharing and the state/trace/graph views.
See [android/README.md](android/README.md) for building (Android Studio or Docker), the protocol contract shared with the server, and its 143 JVM tests.

**Download the APK:** `Kairos-Android-<version>.apk` is attached to the [Releases](../../releases) page (built by `.github/workflows/build-android.yml`).
Enable "install unknown apps" for your browser, open the file, and install. The agent itself runs on a computer, so first start Kairos there
(the Windows app above, `kairos --host 0.0.0.0` from source, or Docker). On the phone:

1. **Connect:** the first screen asks for the computer's address, for example `ws://192.168.1.20:8000` (same Wi-Fi), plus the access key
   Kairos prints in its window when you serve a network (leave it empty if the computer does not ask for one).
2. **API keys:** if that Kairos has no LLM key yet, the app offers a form for your **Groq** and/or **OpenRouter** key (also under
   Settings → API keys). The server checks the key with the provider and stores it on the computer; the phone never keeps it.
3. The microphone and camera work from the phone itself. For use away from home, serve Kairos over HTTPS (`wss://`); the app refuses to send
   keys unencrypted to a public address.

## Deployment

```bash
cp .env.example .env            # add GROQ_API_KEY (or OPENROUTER_API_KEY) and set AUTH_TOKEN
docker compose up --build       # http://localhost:8000, bound to localhost only; models are baked into the image
DOMAIN=agent.example.com TRUST_PROXY=1 docker compose --profile https up --build -d   # + Caddy, automatic HTTPS
```

The microphone and camera only work in a secure context, so anything other than `localhost` needs HTTPS (the `https`
profile does that). The image is multi-stage (UI build, pinned CPU-only dependencies from `requirements.lock`, slim
non-root runtime, read-only filesystem) and about 3.8 GB with the Whisper, MiniLM and Piper models baked in
(`--build-arg BAKE_MODELS=0` skips them and downloads at first run into the `models` volume). Dependencies are pinned for
Python 3.11; CI tests 3.10, 3.11 and 3.12.

**Security defaults** (`agent/settings.py`, all overridable, documented in `.env.example`):

| Protection | Default |
|---|---|
| Bearer token for `/ws`, `/warmup`, `/metrics`, full `/health` | off until `AUTH_TOKEN` is set (a startup warning says so). The UI reads `?token=` from its URL. |
| WebSocket origin check | same host or localhost only; `ALLOWED_ORIGINS` for others. Clients with no `Origin` (scripts, the eval kit) are allowed. |
| Session ids | `[A-Za-z0-9_-]{1,64}` or the handshake is refused |
| Limits | 20 connections per IP, 500 sessions, 30 msgs/s per connection (burst 60), 256 KB/s of audio, 4 MB per message, 8000 chars per user text |
| Abuse handling | over-limit messages are dropped with `{"type":"error","code":...}`; 50 violations close the connection |
| `POST /warmup` | at most once per 30 s (it spends LLM tokens) |
| Headers | `X-Request-ID` echoed, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` |

**Observability:** `LOG_FORMAT=json` gives one JSON object per line with `session_id`, `epoch` and `request_id`.
`GET /metrics` (Prometheus text format) exposes first-acknowledgement and interrupt-to-cancel latency histograms, LLM
request outcomes (ok / timeout / error / rate_limited / fallback / circuit_open), LLM latency, circuit-breaker state,
spoken-reply stop latency, and what was turned away and why (`agent_rejected_total{reason=...}`).

**CI** (`.github/workflows/`): ruff, gitleaks, tests on Python 3.10/3.11/3.12, a mock-mode eval gate
(`python -m agent.eval --llm mock --virtual --set all --fail-under 97`), the frontend build and a Docker build; plus a nightly
live eval (needs the `GROQ_API_KEY` repository secret) that uploads its report. `python scripts/eval_trend.py` prints the trend
of recorded reports.

## Environment variables

See [`.env.example`](.env.example) for the full annotated list. The essentials:

| Variable | Purpose | Default |
|---|---|---|
| `GROQ_API_KEY` | Groq backend (takes priority over OpenRouter) | — |
| `OPENROUTER_API_KEY` | OpenRouter backend | — |
| `USE_LOCAL_LLM`, `LLM_BASE_URL` | point at a local OpenAI-compatible server instead | — |
| `LLM_MODEL_NAME` | model id | `openai/gpt-oss-120b` (Groq) / `qwen/qwen-2.5-7b-instruct` (OpenRouter) |
| `LLM_TIMEOUT_S` | hard LLM deadline | 8 (Groq) / 5 (OpenRouter) / 2 (local) |
| `WHISPER_MODEL` / `_DEVICE` / `_COMPUTE_TYPE` | ASR | `base.en` / `cpu` / `int8` |
| `VOICE_ENDPOINT_MS` | silence that ends an utterance | 500 |
| `TRACE_LOG_PATH` | JSON-lines file the server appends trace records to | unset (in-memory only) |

## Optional extras

```bash
pip install -e ".[embeddings]"   # sentence-transformers for the semantic Tier-1 intent classifier
pip install -e ".[tts]"          # Piper, for spoken replies (also needs a voice file, see .env.example)
pip install -e ".[local]"        # tooling for serving a local model behind LocalQwenBackend
pip install -e ".[dev]"          # pytest, ruff
```

## Key files

| Path | Role |
|---|---|
| `agent/coordinator.py` | orchestrator: queues, debounce, turn tasks, tool execution, voice runtime |
| `agent/coordination/state_machine.py` | `SessionState`: epoch, slots (+undo), in-flight calls, snapshot, rollback |
| `agent/coordination/idempotency.py`, `canonical.py` | idempotency keys over canonicalized values |
| `agent/slow_path/planner.py` | prompt assembly, LLM call, epoch-tagged plans, tool-call → slots |
| `agent/llm_client.py` | Groq/OpenRouter/local/mock backends, circuit breaker, 429 retry |
| `agent/fast_path/*` | Tier-1 interrupt classifier, Tier-2 templates |
| `agent/multimodal/asr.py`, `streaming.py`, `vision.py`, `tts.py` | Whisper, streaming VAD/segmentation, vision, Piper/mock speech synthesis |
| `agent/speech.py` | the agent's voice per session: paced playback, instant stop on interruption, truncation record, echo guard |
| `agent/memory/*` | scratchpad, graph memory, context builder, tool→slot extraction |
| `agent/eval/*` | scenario runner, environment, scorer, suite, report, CLI |
| `frontend/src/App.jsx` | the React UI |
| `eval_results/` | recorded evaluation runs |

Full architecture and rubric alignment: see `interruptible-agents-build-plan(1).md`.
