# Setup and Quickstart

## 1. Choose a path

| Path | Needs | Time | Best for |
|---|---|---|---|
| A. One-command script | Python 3.10 to 3.12, optionally Node 18+ | about 5 min | Most judges |
| B. Docker | Docker with compose | 10 to 20 min first build | No Python on the machine |
| C. Manual | Python, Node | about 10 min | Full control |
| D. Windows installer | Windows 10 or 11 | 2 min | Using it without any tooling: see the Desktop App chapter |

Python 3.13 and newer are not supported by the speech dependencies. The scripts below find a suitable interpreter, or use `uv` to fetch Python 3.11 if you have it.

## 2. Path A: one-command script

```
./setup.sh                 # Linux / macOS
setup.bat                  # Windows
```

The script (`scripts/setup.py`) does six things, each reported as `[n/6]`:

1. Creates `.venv/` inside the repository. Nothing is installed system-wide.
2. Installs the Python dependencies, including the optional extras for the semantic interrupt classifier and spoken replies. If the extras fail to install it retries without them.
3. Creates `.env` from `.env.example` if you do not have one. An existing `.env` is never touched.
4. Builds the web UI if Node.js 18+ is present. Without Node it prints how to build later and carries on.
5. Downloads the models: Whisper `base.en`, the MiniLM classifier and the Piper voice.
6. Runs the offline evaluation as a self-check. It must print `GATE PASSED`.

Useful options:

| Option | Effect |
|---|---|
| `--quick` | Skip model downloads. They are fetched on first use instead. |
| `--no-frontend` | Skip the UI build. |
| `--no-verify` | Skip the self-check. |
| `--check` | Change nothing, only report what is installed or missing. |

On a clean copy of the repository, `./setup.sh --no-frontend --quick` completed and the self-check passed. Almost all of the time was the dependency download.

## 3. Start Kairos

```
.venv/bin/kairos            # Windows: .venv\Scripts\kairos
```

The launcher starts the server on `http://127.0.0.1:8000` and opens the UI in its own window. If an instance is already running it reuses it. It prints which LLM mode is active. Press Ctrl+C to stop.

| Command | Effect |
|---|---|
| `kairos --login` | Require an access key, to see the sign-in screen |
| `kairos --host 0.0.0.0` | Serve your network; an access key is generated and required |
| `kairos --no-open` | Start the server without opening a window |
| `kairos --browser-tab` | Open in a normal browser tab |
| `kairos --install-shortcut` | Add a launcher icon (Linux) |

## 4. Choosing the LLM

| Situation | What to do | Result |
|---|---|---|
| No key | Nothing | Offline mock mode: canned replies, full coordination logic |
| Groq | Set `GROQ_API_KEY` in `.env` | Real reasoning; model `openai/gpt-oss-120b` |
| OpenRouter | Set `OPENROUTER_API_KEY` in `.env` | Real reasoning; the default model is `qwen/qwen-2.5-7b-instruct` |
| Local server | `USE_LOCAL_LLM=1` and `LLM_BASE_URL` | Any OpenAI-compatible server |

If both Groq and OpenRouter keys are set, Groq wins. The Groq model has a 200k tokens per day cap; if replies turn into "I hit a snag reaching my reasoning engine", the cap is probably exhausted.

## 5. Path B: Docker

```
cp .env.example .env        # optionally add a key
docker compose up --build   # http://localhost:8000
```

The image bakes in the speech and classifier models and runs as a non-root user with a read-only filesystem. It is large (about 3.8 GB). `--build-arg BAKE_MODELS=0` skips the models. For HTTPS, `DOMAIN=agent.example.com TRUST_PROXY=1 docker compose --profile https up --build -d` adds Caddy. See the Server, Security and Deployment document.

## 6. Path C: manual

```
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,embeddings,tts]"
cp .env.example .env
python scripts/fetch_models.py
cd frontend && npm ci && npm run build && cd ..
kairos
```

## 7. Verify

| Check | Command | Expect |
|---|---|---|
| Installation state | `python scripts/setup.py --check` | `[ok]` lines |
| Offline evaluation | `python -m agent.eval --llm mock --virtual --set all --fail-under 97 --min-scenario 95` | `GATE PASSED` |
| Unit tests | `pytest -q` | all pass; live-network tests are excluded |
| Server health | `curl http://127.0.0.1:8000/health` | JSON status |

Set `INTENT_EMBEDDINGS=0` to skip the roughly 10 second classifier model load in quick runs.

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "none was found" for Python | Install Python 3.10 to 3.12, or install `uv`, or use Docker |
| Page says the UI is not built | Install Node 18+ and run `cd frontend && npm ci && npm run build` |
| Port 8000 in use | `kairos --port 8001` |
| Microphone button does nothing | Browsers allow the microphone only on `localhost` or HTTPS |
| No spoken replies | The Piper voice is missing: `python scripts/fetch_models.py` |
| Replies say "snag reaching my reasoning engine" | LLM key invalid, rate limited, or the daily token cap is spent |
| Model downloads fail | Offline machine. Kairos still works; models are fetched on first use |
| Interrupts are missed on phrasings with no trigger word | The semantic classifier extra is not installed: `pip install -e ".[embeddings]"` |
