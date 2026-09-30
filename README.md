# Interruptible Real-Time Agent

Full-duplex, interruptible real-time agent — Samsung Hackathon, Theme 05.

A user talks or types; the agent classifies interruptions, plans with an LLM, calls tools, and
streams every step to a React UI. The core is an **epoch model**: each session has a monotonically
increasing epoch, every dispatched tool call is tagged with the epoch it started under, and a
correction bumps the epoch and cancels stale work.

```
Browser (React/Vite)                    FastAPI + WebSocket  (/ws/{session_id})
 ├ text input ───────────────┐           ┌──────────────────────────────────────────────┐
 ├ hands-free mic (AudioWorklet,  │       │ EVENT QUEUE  ← user_text / audio / interrupt / tool_result
 │   16 kHz PCM, 100 ms frames) ──┼──────►│                                              │
 └ (camera/screen: not built)     │       │ Tier 1  interrupt classifier (keyword mode)  │
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
INTENT_EMBEDDINGS=1 python -m agent.eval --llm mock --virtual  # (same, with the MiniLM interrupt classifier)
python -m agent.fast_path.calibrate --errors        # refit + report the interrupt detector on the held-out split
python -m agent.eval --llm mock --virtual               # virtual-time event loop: whole suite in <1s (skips voice)
python -m agent.eval --llm live --out report.json    # real LLM, paced to a TPM budget (~10 min)
python -m agent.eval --llm mock --tag interrupt      # subset by tag: task|interrupt|safety|fault|voice|context
```

Results are written to `eval_results/`.

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
| `VOICE_ENDPOINT_MS` | silence that ends an utterance | 700 |
| `TRACE_LOG_PATH` | JSON-lines file the server appends trace records to | unset (in-memory only) |

## Optional extras

```bash
pip install -e ".[embeddings]"   # sentence-transformers for the semantic Tier-1 intent classifier
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
| `agent/multimodal/asr.py`, `streaming.py`, `vision.py` | Whisper, streaming VAD/segmentation, vision (stub) |
| `agent/memory/*` | scratchpad, graph memory, context builder, tool→slot extraction |
| `agent/eval/*` | scenario runner, environment, scorer, suite, report, CLI |
| `frontend/src/App.jsx` | the React UI |
| `eval_results/` | recorded evaluation runs |

Full architecture and rubric alignment: see `interruptible-agents-build-plan(1).md`.
