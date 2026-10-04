# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Kairos · Καιρός**: a full-duplex, interruptible real-time agent (Samsung Hackathon, Theme 05). The user can talk or type, interrupt or correct it mid-task, and it cancels stale work instead of finishing the wrong thing. Python backend (`agent/`, FastAPI + WebSocket), React web UI (`frontend/`), native Android client (`android/`, Kotlin/Compose), demo-video tooling (`scripts/demo/`). `README.md` has the user-facing detail (security defaults table, env vars, deployment).

## Commands

```bash
pip install -e ".[dev,embeddings,tts]"     # Python 3.10-3.12 (requirements.lock is pinned for 3.11); there is a .venv
pytest -q                                   # live-network tests are excluded by default (-m 'not live')
pytest tests/test_coordinator_flow.py::test_name -q     # a single test
pytest -m live                              # real Groq/OpenRouter tests (spends tokens)
ruff check agent tests scripts              # CI lint: only E9,F (syntax + pyflakes); a module no test imports can still ship a SyntaxError

# eval harness (the quality gate CI runs):
python -m agent.eval --llm mock --virtual --set all --fail-under 97 --min-scenario 95
INTENT_EMBEDDINGS=0 python -m agent.eval --llm mock --virtual     # skip the ~10 s MiniLM load
python -m agent.eval --llm live --set holdout                     # real LLM, paced to a TPM budget; spends tokens

kairos                                      # launcher: starts the server and opens the UI; `--login`, `--install-shortcut`
uvicorn agent.server:app --host 127.0.0.1 --port 8000
cd frontend && npm ci && npm run build      # the server serves frontend/dist/ at "/"; `npm run dev` proxies /ws,/health to :8000
```

Android (no local SDK; there is no emulator/KVM here, everything is JVM tests via Robolectric in Docker, see `android/README.md`):

```bash
docker run --rm -v "$PWD/android:/workspace" -v iagent-gradle:/root/.gradle iagent-android-build ./gradlew testDebugUnitTest assembleDebug
```

Gradle's build cache will replay an old test result: add `--no-build-cache` (and `cleanTestDebugUnitTest`) when a test reads data that is not a Gradle input.

## Architecture (the parts that need several files to understand)

- **Epoch model** (`coordination/state_machine.py`, `coordinator.py`): each session has a monotonically increasing epoch. Every dispatched tool call is tagged with the epoch it started under; a correction/interrupt bumps the epoch and cancels in-flight calls, and results from an older epoch are dropped. `SessionState` owns slots (with undo/rollback), in-flight calls and the snapshot. Idempotency keys come from canonicalized arguments (`idempotency.py`, `canonical.py`).
- **Coordinator** (`agent/coordinator.py`, the largest file): per-session event queue (user text / audio / interrupt / tool result) → Tier 1 interrupt classifier (`fast_path/intent_classifier.py`, MiniLM with keyword fallback; classification runs inline and must never wait for the model load) → Tier 2 template fillers/acks (`fast_path/templates.py`) → Tier 3 planner (`slow_path/planner.py`) calling the LLM → tool router (`coordination/tool_router.py`: schemas, read vs state-modifying tools) → action queue → `TraceLogger` (schema-validated) → WebSocket. Actions are pydantic models in `schemas/actions.py`.
- **LLM layer** (`llm_client.py`): backends Groq / OpenRouter / local / mock behind a circuit breaker with soft/hard deadlines, 429 retry and optional fallback model. Groq takes priority when `GROQ_API_KEY` is set. Do not switch the project to an Anthropic backend (explicit user decision). `openai/gpt-oss-120b` is the only Groq model enabled for this org and it has a 200k tokens/day cap: live evals and recordings can exhaust it (symptom: replies "I hit a snag reaching my reasoning engine", `clarification` actions).
- **Memory** (`memory/`): 2-tier. Scratchpad (canonical turns) + graph memory (turn/entity/artifact nodes, `SUPERSEDES` edges for real overrides, `spoken_text` records what the user actually heard when a spoken reply was cut off). `tool_slots.py` maps tool results to slots (`SKIP_TOOLS` lists tools that must not).
- **Voice** (`multimodal/`, `speech.py`): server-side streaming Silero VAD → Whisper (`faster-whisper`), adaptive endpointing, partial transcripts can trigger a cancel before the sentence ends. Piper TTS speaks sentence by sentence as `audio_out` actions; barge-in ducks then stops it, and an echo guard discards transcripts that are mostly the agent's own words (spelling-independent matching).
- **Virtual clock** (`agent/clock.py`): use `clock.now()` / `clock.monotonic()` / `clock.is_virtual()` instead of `time.*` in agent code, so the eval harness can run the whole suite in virtual time (`--virtual`, <1 s). Anything that sleeps or measures time must go through it.
- **Eval harness** (`agent/eval/`): scenarios (`scenarios.py`, dev set + `holdout.py`), a scripted environment, scorer, suites. CI gates on mock mode (overall ≥97, every scenario ≥95, zero stale completions/duplicate state changes/trace violations/false completion claims). Results go to `eval_results/`.
- **Tools beyond travel**: `export_artifact` (`exporter.py`: safe filenames, allow-listed extensions, 200 KB cap, never overwrites, launches the editor via an argument list, `EXPORT_DIR` / `EXPORT_OPEN` / `EXPORT_EDITOR_CMD`, served back at `/exports/{name}`) and `set_timer`. The coordinator keeps the last worker artifact per session so export can use it. Exports emit a `file_exported` action.
- **Server** (`server.py`, `security.py`, `settings.py`): `AUTH_TOKEN` bearer protection on `/ws`, `/warmup`, `/metrics`, `/auth/check`, `/exports/`; `/health` is public but minimal. Origin check, per-IP/session/message limits, `X-Frame-Options: DENY`. `agent/launcher.py` (`kairos`) picks auth policy by host and reuses a running instance.
- **Clients**: the web UI (`frontend/src/App.jsx` is one big component) and the Android app (`android/app/.../state/AgentController.kt` + `ChatReducer.kt`, pure reducer) are thin clients of the same WebSocket protocol.

## Desktop app (Windows `.exe`)

`packaging/` (PyInstaller spec, Inno Setup script, entry script, icon) + `.github/workflows/build-desktop.yml` (windows-latest builds the installer and attaches it to a release). `agent/desktop.py` is the entry (first-run Piper voice download), `agent/keystore.py` stores the user's Groq/OpenRouter keys in `%APPDATA%\Kairos` (never in the repo, never echoed back), `PUT /settings/keys` validates them with the provider and calls `coordinator.reload_llm()` so no restart is needed; the UI side is `frontend/src/components/ApiKeysScreen.jsx`. The build deliberately excludes torch/sentence-transformers (keyword Tier 1 fallback). PyInstaller cannot cross-compile: the `.exe` is only built in CI; `pyinstaller packaging/kairos.spec` locally gives a Linux binary that is a valid smoke test of the spec. Tests that start the server must use a light mock coordinator (see `tests/test_keystore.py`): the real lifespan loads models and a few of them in one pytest process gets OOM-killed.

## Cross-cutting rules that are easy to break

- **Protocol contract with Android**: the Kotlin `Protocol.kt` parses fixtures generated from the real pydantic classes by `scripts/dump_android_fixtures.py` (checked by `tests/test_android_fixtures.py`). Adding or changing an action type means updating the dump script, the fixtures, `Protocol.kt` and `ChatReducer.kt`.
- `tests/test_imports.py` imports every module (a syntax error once shipped because no test imported the file).
- `tests/test_no_secrets.py` and CI's gitleaks scan for committed keys; `.env` is local only.
- Tests that need real audio/models skip in virtual time; voice scenarios are not part of the gate.

## Conventions

- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Only commit or push when asked. Stage specific files; never `.serena/`, `plan.md`, `frontend/node_modules/.vite/`.
- Untracked files in the working tree may belong to someone else: check before deleting or overwriting.
- Avoid `pkill -f <pattern>` where the pattern also appears in your own command line (use `pkill -f "[p]attern"`).

## Demo video (`scripts/demo/`)

A mock Linux desktop (`desktop.html`) hosts the real web app in an iframe; `record.py` drives it with Playwright + CDP screencast, `assemble.py` builds an exact 60 fps file, `mux_audio.py` rebuilds the soundtrack (headless Chrome has no audio), `android/.../DemoFrames.kt` renders the phone section from recorded real server frames (`build_phone_plan.py` turns them into the plan). `scripts/demo/README.md` has the run order and what is real vs mocked. A take costs ~60k LLM tokens: check `events.txt` for `clarification` before accepting one.
