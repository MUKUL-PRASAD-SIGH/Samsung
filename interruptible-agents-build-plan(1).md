# Interruptible Real-Time Agents — Full Build Plan
### Samsung Hackathon · Theme 05 · v1

---

## 1. Problem Recap

Build a voice-native agent that operates in **full-duplex** mode — handling interruptions, re-planning, and mid-sentence corrections — instead of the standard half-duplex listen→think→speak loop.

**Scoring weights (design around these):**

| Category | Weight | What it actually measures |
|---|---|---|
| Task Completion | 40% | Correct tool execution, argument extraction, snapshot accuracy |
| Interruption Recovery | 35% | Fast cancellation of stale calls, no stale re-runs, updated snapshots |
| Response Latency | 15% | Time to first substantive spoken action |
| Safety & Protocol | 10% | Zero duplicate state-changing calls, valid schema/payloads |

Quality multiplier (0.80×–1.20×) on naturalness/truthfulness/relevance; 1.5× multiplier on hidden multimodal scenarios.

---

## 2. System Architecture

```
                        ┌─────────────────────────────────────────┐
                        │              EVENT QUEUE (in)            │
                        │  text chunks · audio · video frames ·    │
                        │  interrupt signals · tool results        │
                        └───────────────────┬───────────────────────┘
                                            │
                    ┌───────────────────────┼───────────────────────┐
                    │                       │                       │
              ┌─────▼─────┐          ┌──────▼──────┐         ┌──────▼──────┐
              │  TIER 1    │          │  TIER 2      │         │  TIER 3      │
              │  Intent /  │          │  Fast Path   │         │  Slow Path   │
              │  Interrupt │          │  (fillers,   │         │  (reasoning, │
              │  Detector  │          │  acks)       │         │  tool calls) │
              │ MiniLM-L6  │          │  Templates   │         │  Qwen3-4B /  │
              │            │          │              │         │  OpenRouter  │
              └─────┬─────┘          └──────┬──────┘         └──────┬──────┘
                    │                       │                       │
                    └───────────────────────┼───────────────────────┘
                                            │
                        ┌───────────────────▼───────────────────────┐
                        │           COORDINATION LAYER               │
                        │  · Session state machine (epoch-versioned) │
                        │  · In-flight call registry (call_id map)   │
                        │  · Idempotency key store                   │
                        │  · Tool schema router (read-only / state)  │
                        └───────────────────┬───────────────────────┘
                                            │
                        ┌───────────────────▼───────────────────────┐
                        │              ACTION QUEUE (out)            │
                        │  spoken fillers · tool calls · cancels ·   │
                        │  clarifications · final responses + state  │
                        │  snapshot                                  │
                        └─────────────────────────────────────────────┘
```

### 2.1 The Epoch Model (core interruption-handling mechanism)

Every session has a monotonically increasing `epoch`. Every dispatched tool call is tagged with the epoch it was spawned under.

```
On new user event:
    if event is correction/new-intent (via Tier 1 classifier):
        session.epoch += 1
        cancel all in-flight calls where call.epoch < session.epoch
        emit cancellation actions for each (with call_id)
    dispatch new plan under session.epoch
```

This single mechanism is what earns Interruption Recovery (35%) and half of Safety & Protocol (10%) points — it's the highest-leverage piece of the whole system. Build and stress-test it first.

### 2.2 Idempotency for State-Modifying Calls

```
idempotency_key = hash(intent + sorted(slot_values) + epoch)
```

Before dispatching a state-modifying tool call, check the key store. If seen, skip (return cached result) instead of re-firing. This guarantees zero duplicate bookings even under retries or race conditions.

### 2.3 Slot State (Session Snapshot)

```json
{
  "session_id": "abc123",
  "epoch": 4,
  "intent": "book_flight",
  "slots": {
    "origin": "BLR",
    "destination": "DEL",
    "date": "2026-10-12",
    "time": null
  },
  "in_flight_calls": [
    {"call_id": "c9", "tool": "search_flights", "epoch": 4, "status": "pending"}
  ],
  "last_updated": "2026-09-25T10:22:01Z"
}
```

Corrections **patch** this object (diff-based), never replace it wholesale — this preserves "state snapshot accuracy" scoring even across multi-turn corrections.

---

## 3. Model Stack

| Tier | Role | Model | Why |
|---|---|---|---|
| 1 | Interrupt / intent-shift detection | **all-MiniLM-L6-v2** | CPU-only, ~ms latency, embedding similarity is enough to classify continuation vs. correction vs. new intent |
| 2 | Acks / fillers | **Rule-based templates**, slot-filled | Zero hallucination risk on "false completion claims" — the biggest quality-multiplier risk. No model needed here. |
| 3 | Reasoning + tool calls | **Qwen3-4B-Instruct-2507** (local, quantized) *or* **OpenRouter API model** (pluggable) | See §3.1 |
| — | ASR (raw audio scenarios) | **Whisper (small/base)** | Needed for the 30% audio-only inputs where no pre-transcribed text is given |
| — | Vision grounding (frame-grounded lookups) | **Qwen2.5-VL-3B** (or similar small VLM), called on-demand only | 20% visual scenarios; don't run continuously — invoke only when slow path needs frame grounding |

### 3.1 Local vs. API — Decision Point

**Before committing, verify the eval sandbox's network policy.** Section 6 of the brief specifies a fixed runtime (Python 3.10–3.12, 120s cap/scenario, virtual-clock harness) — this is a strong signal of a sandboxed, possibly network-restricted environment. If outbound calls are blocked, an OpenRouter-only design fails outright.

**Mitigation: build the LLM call behind one interface, keep both paths ready.**

```python
# llm_client.py
from abc import ABC, abstractmethod

class LLMBackend(ABC):
    @abstractmethod
    async def generate(self, messages: list, tools: list) -> dict: ...

class LocalQwenBackend(LLMBackend):
    async def generate(self, messages, tools):
        # vLLM / local inference call
        ...

class OpenRouterBackend(LLMBackend):
    async def generate(self, messages, tools):
        # HTTP call to OpenRouter, same interface
        ...

def get_backend(config) -> LLMBackend:
    return OpenRouterBackend() if config.use_api else LocalQwenBackend()
```

Switching backends becomes a one-line config change, not a rearchitecture. Default to local Qwen3-4B for the submission; keep OpenRouter as dev-time fallback for faster iteration/testing against the public suite.

| | Local Qwen3-4B-Instruct-2507 (AWQ int4, vLLM) | OpenRouter API |
|---|---|---|
| Network dependency | None | Required — verify sandbox allows it |
| Latency | Predictable, no network hop | Variable, provider queueing risk |
| Setup risk | Medium (quantization, server config) | Low (API key + HTTP call) |
| Tool-call reliability | Good with validation/repair layer | Better, if using a strong model |
| Cost/rate limits | None after setup | Watch limits across ~60 hidden scenarios |

**Recommendation:** ship local as the default. It's the only option guaranteed to work regardless of sandbox network policy, and 4B is small enough to coexist with MiniLM + Whisper on a single GPU.

### 3.2 Tool-Call Output Validation

Never trust raw LLM JSON output directly against Safety & Protocol scoring:

```python
def validate_and_repair(raw_output: str, schema: dict) -> dict:
    try:
        parsed = json.loads(raw_output)
    except json.JSONDecodeError:
        parsed = attempt_json_repair(raw_output)  # e.g. json_repair lib
    jsonschema.validate(parsed, schema)  # raises on structural mismatch
    return parsed
```

---

## 4. Backend Stack

| Layer | Choice | Notes |
|---|---|---|
| Language/runtime | Python 3.11 | Matches the required 3.10–3.12 range |
| Async framework | `asyncio` + `FastAPI` (for any HTTP/WebSocket surface) | FastAPI's native async support matches the non-blocking requirement directly |
| Event queues | `asyncio.Queue` (in-process) | Simplest match to the "two asynchronous queues" interface contract — no need for external brokers like Redis/Kafka at hackathon scale |
| Inference serving (local LLM) | `vLLM` | Best throughput/latency for quantized small models; supports OpenAI-compatible API surface for easy backend swapping |
| Quantization | AWQ int4 via `autoawq` | Keeps Qwen3-4B fast and memory-light |
| Embeddings | `sentence-transformers` (MiniLM) | Standard, CPU-friendly |
| ASR | `faster-whisper` (CTranslate2-backed) | Meaningfully faster than stock `openai-whisper` for real-time use |
| Vision | `transformers` + Qwen2.5-VL, invoked on-demand | Don't keep resident unless VRAM allows |
| State store | In-memory dict keyed by `session_id` (session-scoped only, per constraints) | Spec explicitly rules out cross-session caching — don't over-engineer with a DB |
| Schema validation | `pydantic` + `jsonschema` | For both tool manifests and outgoing action payloads |
| Tracing/logging | Structured JSON logs per event/action, timestamped | The eval harness scores strictly from trace logs — this is not optional |

### 4.1 Directory Structure

```
agent/
├── main.py                 # entrypoint, wires queues + harness interface
├── llm_client.py            # LLMBackend interface (§3.1)
├── coordination/
│   ├── state_machine.py     # session snapshot + epoch logic
│   ├── idempotency.py       # key store
│   └── tool_router.py       # manifest parsing, read-only vs state-modifying
├── fast_path/
│   ├── intent_classifier.py # MiniLM-based interrupt detection
│   └── templates.py         # filler/ack templates
├── slow_path/
│   ├── planner.py           # LLM reasoning + tool-call generation
│   └── validator.py         # JSON schema validation/repair
├── multimodal/
│   ├── asr.py                # faster-whisper wrapper
│   └── vision.py              # Qwen2.5-VL wrapper
├── schemas/                  # tool manifests, action payload schemas
└── tests/
    ├── test_public_scenarios.py
    └── test_adversarial_timing.py
```

---

## 5. Frontend Stack

The eval kit is harness-driven (trace logs, not UI), but a demo UI matters for judging presentation and your own debugging.

| Layer | Choice | Notes |
|---|---|---|
| Framework | React + Vite | Fast dev loop, minimal ceremony |
| Styling | Tailwind CSS | Speed of iteration over custom design system for a hackathon timeline |
| Real-time transport | WebSocket (native, or `socket.io` if you want reconnection handling for free) | Mirrors the event/action queue model directly — frontend emits events, backend streams actions |
| Audio capture | `MediaRecorder` API → stream chunks over WebSocket | For live mic demo scenarios |
| State visualization | A live panel rendering the session snapshot JSON + epoch counter + in-flight call list | This is your best demo asset — visually prove interruption recovery is working in real time |
| Transcript view | Scrolling event/action log, color-coded by type (event vs. fast-path ack vs. tool call vs. cancellation) | Doubles as your own debugging tool during dev |

### 5.1 Demo UI Panels (recommended)

1. **Conversation view** — chat-style transcript of user input + agent responses
2. **State snapshot panel** — live JSON view of intent/slots/epoch, updates in real time
3. **Trace timeline** — horizontal timeline showing tool calls, cancellations, and fillers with timestamps — this is the panel that sells "we handle interruptions correctly" visually to judges

---

## 6. Build Order

1. **State machine + event/action queues** with a trivial echo agent (no LLM yet). This is the scoring backbone — get session snapshot + epoch logic rock solid before anything else.
2. **Epoch-based interrupt handling** — cancel stale `call_id`s on new epoch. Test against text-only scenarios first (50% of both suites).
3. **Fast-path filler/ack layer** — template-based, slot-filled. Pure latency-score points for low effort.
4. **Schema-driven tool routing** — parse manifests, classify read-only vs. state-modifying, attach idempotency keys automatically.
5. **Slow-path LLM integration** — Qwen3-4B via vLLM behind the `LLMBackend` interface; validate/repair all tool-call JSON output.
6. **Multimodal layering** — ASR (Whisper) and vision (Qwen2.5-VL) added on top of the same event/action interface, not as separate systems.
7. **Frontend demo UI** — build in parallel once the WebSocket event/action contract is stable; don't block backend progress on this.
8. **Adversarial timing stress tests** — rapid-fire interruptions, mid-call corrections, simultaneous audio+text — before the hidden set does it for you.

---

## 7. Robustness Additions

These target the parts of the architecture most likely to break under adversarial timing or fault injection — exactly what the hidden set tests. Priority order if time is tight: **event debounce** and **confidence-gated interrupts** first — both are small additions that directly harden the epoch mechanism, where most points get won or lost.

### 7.1 Timeout + Circuit Breaker on the Slow Path
If the LLM call exceeds a hard deadline (~1.5–2s), degrade gracefully instead of blowing the latency score.
- Set a per-call deadline; on timeout, fall back to a templated clarification ("still working on that") rather than silence.
- Track consecutive timeouts per session; after N failures, switch to a smaller/faster fallback model for the rest of that session.
- Protects Response Latency (15%) from a single slow LLM call tanking a scenario.

### 7.2 Confidence-Gated Interrupt Detection
Tier 1 (MiniLM) gates the entire epoch-bump decision — if it's wrong, you either miss a real interruption or cancel a valid call.
- Add a similarity-score threshold band: high confidence → auto-act, mid-band → ask a quick clarification instead of guessing, low → treat as noise.
- Log the raw similarity score in trace output so the threshold can be tuned against the public suite before the hidden set.
- Cheap to add, protects the highest-weighted category after Task Completion (Interruption Recovery, 35%).

### 7.3 Fault-Injection Test Harness (Mirror Theirs)
The brief states the mock environment does deterministic latency and fault injection — build an equivalent now rather than waiting for the real one.
- Wrap tool calls in a test double that can inject delays, timeouts, and malformed responses on demand.
- Run adversarial-timing tests against it before the real eval kit drops.
- Catches idempotency and cancellation bugs while they're still cheap to fix.

### 7.4 Snapshot Versioning with Rollback
A corrupted or partial slot patch mid-correction can silently wreck state-snapshot accuracy for the rest of a scenario.
- Keep the last N known-good snapshots (small ring buffer, session-scoped — no persistence needed).
- If a patch fails schema validation, roll back to the last good snapshot and emit a clarification instead of propagating bad state.
- Cheap insurance against a single bad turn cascading into total task failure.

### 7.5 Event Debounce / Coalescing
Rapid-fire corrections (e.g. a user correcting themselves three times in 500ms) can spawn and cancel calls faster than the coordination layer can keep up.
- Buffer incoming events for a short window (~100–150ms) and coalesce into a single net state change before dispatching.
- Prevents epoch-thrashing — bumping the epoch three times in a row when only the final correction matters.
- Keeps `call_id` churn low, which also helps the duplicate-call safety score.

### 7.6 Self-Validating Trace Output
Scoring is strictly from trace logs, so a malformed log entry is effectively an automatic point loss even if the agent behaved correctly.
- Run every emitted event/action through the same `jsonschema` validation used for tool calls, before it hits the log.
- Assert invariants at emit time: every tool call has a `call_id`, every cancellation references a real prior `call_id`, every epoch is monotonic.
- Fail loudly in dev/test, but never let a malformed log line reach the real trace during eval.

### 7.7 Warm-Up Preloading
The 300s setup/warm-up hook exists before the 120s-per-scenario clock starts — use all of it.
- Load and run one dummy inference through every model (Qwen3, MiniLM, Whisper, VLM) during setup so first-call latency isn't paid mid-scenario.
- Pre-warm the vLLM KV cache and CUDA context specifically — cold GPU calls are often 2–5x slower than warm ones.
- Verify this actually happens inside the hook once the harness spec is released — don't assume.

---

## 8. Risks & Open Questions

- **Sandbox network policy** — determines whether OpenRouter is viable at all for the actual submission. Verify the moment the evaluation kit drops.
- **ASR necessity** — the interface contract lists pre-transcribed text chunks *and* raw audio as separate input types; confirm whether Whisper is needed for all audio scenarios or only a subset.
- **VRAM budget** — confirm what hardware you'll actually have during judging; this decides whether Qwen3-4B, or something smaller still, is realistic to run alongside MiniLM + Whisper + on-demand VLM.
- **120s wall-clock cap per scenario** — make sure cold-start (model loading) happens in the 300s setup/warm-up hook, not counted against the per-scenario budget.
