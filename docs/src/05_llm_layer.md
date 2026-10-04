# The LLM Layer (agent/llm_client.py)

## 1. Overview

`agent/llm_client.py` is the single module through which Kairos talks to a language model. It has four responsibilities.

1. Choose a backend from the environment (Groq, OpenRouter, a local OpenAI compatible server, or an offline mock).
2. Speak each provider's chat completions protocol and normalise the answer into one type, `LLMResponse` (a tool call, a spoken response or a clarification).
3. Protect the real time loop from slow and failing providers: hard and soft deadlines, a one time retry on HTTP 429, a retry on model produced tool call rejections, a circuit breaker with half-open probing, and an optional fallback model.
4. Strip the in-band `MEMORY_UPDATE:` line out of the model text so it feeds memory and never reaches the chat or speech.

The planner (`agent/slow_path/planner.py`) is the only caller. It builds a `CircuitBreakerLLMClient` around the backend that the coordinator selected, so every real LLM request in the system passes through the breaker.

```
 Planner.plan
    |
    v
 CircuitBreakerLLMClient.generate(messages, tools, on_slow)
    |   state: CLOSED | OPEN | HALF-OPEN        metrics: LLM_REQUESTS, LLM_LATENCY, ...
    |
    |--- circuit OPEN (cooldown not over)
    |        fallback backend configured ?  yes -> _call_fallback
    |                                       no  -> static "still processing" clarification
    |
    |--- CLOSED or half-open probe
    |        _generate_with_deadlines  (soft deadline -> on_slow, hard deadline -> Timeout)
    |             |
    |             v
    |        backend.generate(messages, tools)
    |             |  Groq / OpenRouter : urllib in a thread executor, 429 retry once
    |             |  Groq              : tool-call rejection resampled once
    |             |  Local             : plain request
    |             |  Mock              : canned queue, else keyword heuristic
    |             v
    |        LLMResponse  (tool_call | spoken_response | clarification)
    |
    +--- timeout / exception -> record failure, maybe open circuit, fallback or static text
```

## 2. Configuration model

### LLMConfig

`LLMConfig` is a pydantic model. All its defaults are computed from environment variables at construction time (every `LLMConfig()` re-reads the environment). `load_dotenv()` runs at import so a `.env` file is honoured.

| Field | Default | Source |
|---|---|---|
| `backend_type` | `groq` if `GROQ_API_KEY`, else `openrouter` if `OPENROUTER_API_KEY`, else `local` if `USE_LOCAL_LLM`, else `mock` | `_default_backend_type()` |
| `model_name` | `LLM_MODEL_NAME`; otherwise `openai/gpt-oss-120b` when `GROQ_API_KEY` is set, else `qwen/qwen-2.5-7b-instruct` | env |
| `api_key` | first set of `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY` | env |
| `base_url` | `LLM_BASE_URL`; otherwise `https://api.groq.com/openai/v1` (Groq key), `https://openrouter.ai/api/v1` (OpenRouter key), `http://localhost:8000/v1` (anything else) | env |
| `timeout_s` | `LLM_TIMEOUT_S`; otherwise 8.0 (Groq key), 5.0 (OpenRouter key), 2.0 (neither) | env; this is the hard deadline |
| `max_consecutive_timeouts` | 3 | constant, not an env var |
| `rate_limit_max_wait_s` | `LLM_RATE_LIMIT_MAX_WAIT_S`, default 3.0 | env |
| `soft_deadline_s` | `LLM_SOFT_DEADLINE_S`, default 2.0 (0 disables) | env |
| `circuit_cooldown_s` | `LLM_CIRCUIT_COOLDOWN_S`, default 15.0 | env |
| `temperature` | 0.1 | constant, sent with every request |

Selection precedence is Groq, then OpenRouter, then local, then mock. This priority is repeated in `README.md` ("Groq takes priority when `GROQ_API_KEY` is set") and tested in `tests/test_llm_client.py`.

Where the model name comes from: Groq defaults to `openai/gpt-oss-120b`. The code comment notes that the available Groq model set is account specific (two other models were unavailable on the account the project was built against). `CLAUDE.md` adds that this is the only Groq model enabled for the project's organisation and that it has a 200k tokens per day cap; exhausting it shows up as the "I hit a snag reaching my reasoning engine" reply.

### The data model

`LLMResponse` (pydantic) is the normalised answer.

| Field | Meaning |
|---|---|
| `response_type` | `tool_call`, `spoken_response` or `clarification` |
| `content` | text for spoken responses and clarifications |
| `tool_name`, `arguments` | the tool call (first call only) |
| `raw_text` | raw provider text or the JSON of the tool call |
| `latency_s` | field exists, default 0.0 (not populated by the backends in this file) |
| `memory_update` | parsed `MEMORY_UPDATE` dictionary, or None |
| `via_fallback` | True if the answer came from the fallback backend |
| `usage` | provider reported token counts (`prompt_tokens`, `completion_tokens`, `total_tokens`) when present, via `_usage()` |

`LLMBackend` is the abstract base: one async method `generate(messages, tools=None) -> LLMResponse`.

## 3. The backends

### GroqBackend

Class `GroqBackend`. Posts to `{base_url}/chat/completions` with `urllib` (no HTTP library dependency), in a thread through `loop.run_in_executor`, so the event loop is never blocked.

Details drawn from the code:

- Sends `Authorization: Bearer <api_key>` and a custom `User-Agent`. A code comment explains that Groq sits behind Cloudflare, which blocks urllib's default `Python-urllib` agent as a bot (HTTP 403, error code 1010) even with a valid key.
- Payload: `model`, `messages`, `temperature`, and `tools` when the planner passes any.
- HTTP 429 becomes `RateLimitError` carrying the suggested wait (see section 5).
- HTTP 400 whose body contains `tool_use_failed` or `Tool call validation failed` becomes `ToolCallRejectedError` (see below). Any other HTTP error becomes a `RuntimeError`.
- Network errors (`URLError`) propagate to the breaker.
- Response parsing: if `choices[0].message.tool_calls` is non-empty, the first call is used. Its `arguments` string goes through `validate_and_repair` (the repair layer from `agent/slow_path/validator.py`), giving a `tool_call` response. Otherwise the content passes through `_extract_memory_update` and becomes a `spoken_response`.

Tool call rejection handling: Groq validates the model's tool call against the schema and returns HTTP 400 if the model's output was bad. The provider is healthy, and sampling again usually fixes it. So the backend catches `ToolCallRejectedError` once and re-requests. If it is rejected twice, it returns a `clarification` response ("I couldn't work out that request cleanly -- could you rephrase it or give me the details again?") instead of raising, so the turn does not fail and, importantly, the breaker is not tripped (the exception never escapes). This is tested in `tests/test_rate_limit_retry.py`.

### OpenRouterBackend

Class `OpenRouterBackend`. Same OpenAI compatible protocol, with `HTTP-Referer` and `X-Title` headers. It shares the 429 retry helper. Differences from Groq worth knowing:

- No handling of `tool_use_failed`; an HTTP 400 is a plain `RuntimeError`.
- Tool call arguments are parsed with plain `json.loads`, not `validate_and_repair`. A model that emits slightly malformed JSON raises there, and the failure is counted by the breaker.
- Default model `qwen/qwen-2.5-7b-instruct`, default hard deadline 5 s.

### LocalQwenBackend

Class `LocalQwenBackend`. For any OpenAI compatible local server such as vLLM or llama.cpp. Enabled when `USE_LOCAL_LLM` is set (and neither cloud key is). Differences:

- No `Authorization` header (the `api_key` is not sent).
- No 429 retry (it uses `run_in_executor` directly, not `_request_with_rate_limit_retry`) and no HTTP error translation: errors surface as raw `urllib` exceptions and are handled by the breaker.
- Arguments pass through `validate_and_repair`.
- Does not populate `usage`.
- The name is historical; it makes no assumption that the model is Qwen. The module docstring mentions "Qwen3-4B / Qwen2.5-3B via vLLM" as examples.
- Default `base_url` `http://localhost:8000/v1` and a 2 s hard deadline, which is tight for any local model that is not very fast. Raise `LLM_TIMEOUT_S` for larger local models.

### MockLLMBackend

Class `MockLLMBackend`: the offline backend. Because judges may run the project without any API key, its behaviour is described here in detail. Everything below is read from the code.

How it is selected: `get_backend(LLMConfig())` returns `MockLLMBackend()` whenever `backend_type` is anything other than `groq`, `openrouter` or `local`. With none of `GROQ_API_KEY`, `OPENROUTER_API_KEY` or `USE_LOCAL_LLM` set, `_default_backend_type()` returns `mock`. The coordinator constructor does exactly this when no backend is injected (`self.llm_backend = llm_backend or get_backend(LLMConfig())`). The `kairos` launcher prints the banner "no LLM key found: using canned demo replies. Set GROQ_API_KEY in .env for the real thing." in this case.

What the mock does:

1. Records each message list in `call_history`.
2. If a queue of canned `LLMResponse` objects has been supplied (constructor argument `canned_responses` or `queue_response`), it pops and returns the next one. This is how the eval harness scripts the model: `agent/eval/runner.py` builds `MockLLMBackend(canned_responses=list(scenario.mock_llm))` for each scenario in mock mode, so mock mode evaluates the coordination layer with a scripted model.
3. Otherwise it applies a keyword heuristic to the last message content (the user text), lowercased, in this order:

| Order | Condition (substring of the user text) | Response |
|---|---|---|
| 1 | contains any of `agent`, `bob`, `clock`, `code`, `build`, `script`, `typescript`, `component`, `scout` | `spawn_agent` tool call. The name is `bob` and the role "TypeScript Generator" with goal "Build AnalogClock component in TypeScript"; if the text contains `scout`, the name is `scout`, the role "Market & Flight Scout" and the goal "Search and compare travel options". Arguments also include `component` "AnalogClock" and `language` "TypeScript" |
| 2 | contains `flight` | `search_flights` with `origin` "BLR" and `destination` "DEL" |
| 3 | contains `hotel` | `book_hotel` with `city` "Paris" and `nights` 2 |
| 4 | none of the above | spoken response "How can I assist you with your booking?" |

What the mock does NOT do. This list is the important part for anyone running without a key.

- It does not understand the request. It never extracts values from what the user said: "flights from Chennai to Goa tomorrow" still produces `search_flights` for BLR to DEL. "Hotels in Rome" produces a `book_hotel` call (not a search) for Paris, 2 nights.
- It does not respond to corrections in a content sensitive way. The coordination machinery (Tier 1 classification, epoch bump, cancellation, snapshot, filler) still works because it does not depend on the LLM, but the re-plan after a correction yields the same canned arguments, so the corrected city never appears.
- Matching is substring matching on the final message only, in the fixed order above, so unrelated words can trigger the first rule: "description" contains "script", "barcode" contains "code", and any request mentioning "build" becomes a `spawn_agent` call, which runs a worker. A request such as "cancel my flight" matches rule 2 (`flight`) and runs a flight search, not a cancellation.
- It has no weather, timer, export, cancel-booking or vision logic. "What's the weather in Delhi" gets the fallback spoken line "How can I assist you with your booking?". The mock never returns a `clarification`, never returns a `MEMORY_UPDATE`, and never returns a tool call other than the three above. `export_artifact` and `set_timer` are never chosen by the mock heuristic.
- It never uses prior turns or the vision observation; only the last message is read. When the planner attaches a vision result, the text it reads includes the observation string, so keyword rules can fire on words in it.
- It makes no network call, has no latency, never times out and never trips the breaker. Replies are therefore instantaneous, and any latency numbers measured on it describe the coordination layer only.
- The `spawn_agent` call still runs the real worker machinery. Whether that produces content without an LLM key depends on the worker implementation (see the agent worker chapter); this document does not claim a worker output.

Practical meaning: the offline mode demonstrates the full duplex mechanics (typed or spoken input, filler within milliseconds, cancellation and epoch handling, state snapshot, the UI) and gives deterministic canned replies. It is not a substitute for the real model for judging language understanding. The quality gate numbers in `CLAUDE.md` (eval mock mode, overall at least 97) score the coordination layer with scripted model responses, not the model.

A further caution found in the repository: `.env.example` ships with `OPENROUTER_API_KEY=sk-or-v1-your-key-here` and `LLM_MODEL_NAME=qwen/qwen-2.5-7b-instruct` as uncommented lines. If someone copies `.env.example` to `.env` unedited, `_default_backend_type()` sees a non-empty `OPENROUTER_API_KEY` and selects the OpenRouter backend with a placeholder key. Nothing in the code detects a placeholder. Every request then fails at the provider (an HTTP error), the breaker counts failures, and the user sees "I hit a snag reaching my reasoning engine" rather than the mock's canned replies. To run offline, either do not create a `.env` or comment those two lines out. Also, a `LLM_MODEL_NAME` left set to the Qwen model will be sent to Groq if a Groq key is added later, because the env override beats the Groq default.

## 4. Parsing the model text: MEMORY_UPDATE

The planner prompt asks the model to end its reply with a line of the form `MEMORY_UPDATE: {"entities": [...], "intent_shift": null}`. This is the one place slot and entity extraction from the model's own text happens. `_extract_memory_update(content)` returns `(cleaned_content, memory_update_or_None)` and is used by all three real backends for non tool call replies.

How it works:

1. Find the marker with the regex `_MEMORY_MARKER`, which tolerates surrounding markdown (asterisks, underscores, backticks) and an optional colon.
2. Split the text into what is before and after the marker. Locate the first `{` after the marker and parse one JSON value with `json.JSONDecoder().raw_decode`, so trailing text after the JSON is tolerated and kept.
3. If the JSON is a dictionary, it becomes `memory_update`. If it is malformed, the rest of the block is dropped.
4. The text before the marker has trailing noise removed (`_TRAILING_NOISE`: a `---` rule, `***`, `___` or a code fence), and any text after the JSON is appended after a blank line.

Guarantee (stated in the docstring): the marker is always removed from the visible reply, even if the JSON is malformed, so the raw line never leaks into chat or speech. A malformed block yields None. The planner then records entities with `record_entity_candidate` in the scratchpad (`agent/slow_path/planner.py`).

The mock backend never produces a `MEMORY_UPDATE`, so with no key, memory entities come only from tool call arguments (`entities_from_tool_call`), not from the model's text.

## 5. Rate limits: HTTP 429 handling

### Purpose

Free tier token per minute limits cause frequent 429 responses that clear within about two seconds. One retry turns most of them into a slightly slower success instead of a failed turn.

### How it works

`_request_with_rate_limit_retry(config, do_request, provider)`:

1. Runs the blocking `do_request` in the default thread pool.
2. If it raises `RateLimitError`, reads `retry_after_s`. If it is None (no hint) or greater than `config.rate_limit_max_wait_s` (default 3.0 s), the error is re-raised immediately so the breaker can degrade instead of stalling the user.
3. Otherwise it logs a warning, sleeps `wait + 0.1` seconds with `asyncio.sleep` (so it honours the virtual clock in tests) and retries exactly once. A second 429 is not retried again.

`_parse_retry_after(header_value, body_text)` reads the wait from the `Retry-After` header first (numeric seconds, clamped at zero), then from the provider text (`try again in 1.905s`, `250ms`, `1m5.2s` are all parsed through `_DURATION_TOKEN` and `_UNIT_SECONDS`). The header wins when both are present.

Important property: a 429 that is retried successfully is invisible to the circuit breaker, because the exception never escapes the backend call. Only an unrecoverable 429 (long or unknown wait, or a second 429) reaches the breaker, which labels it `rate_limited` in the metrics.

The retry is used by Groq and OpenRouter. The local backend has none.

### Tests

`tests/test_rate_limit_retry.py`: parsing of retry hints, header precedence, a short 429 retried once and succeeding, long waits and missing hints not retried, only one retry, non 429 errors not retried, a retried 429 invisible to the breaker, an unrecoverable 429 degrading gracefully, the Groq default timeout of 8 s, and the tool call rejection resampling.

## 6. The circuit breaker

### Purpose

If the provider is down, rate limited or slow, every user message would otherwise wait for the full hard deadline and then fail. The breaker converts repeated failure into an immediate, graceful response, and probes automatically to recover. Its section in the module docstring cites the spec as 7.1.

### States

| State | Meaning | Behaviour |
|---|---|---|
| CLOSED | normal | requests go to the primary backend |
| OPEN | `max_consecutive_timeouts` (3) consecutive failures happened | requests do not touch the primary; use fallback or static text |
| HALF-OPEN | `circuit_cooldown_s` (15 s) elapsed since opening | exactly one request probes the primary; all others still get the fallback or static text |

Both timeouts and backend errors (HTTP 429 that could not be retried, 5xx, network failure, malformed JSON) count as failures. A success resets the counter and closes the circuit. A failed probe re-opens it and restarts the cooldown (in `_record_failure`, `is_circuit_open` already true means it restarts the clock even though the counter is above the threshold). Without the half-open step, one bad minute at the provider would degrade the agent until restart, according to the docstring.

### How `generate` works, step by step

`CircuitBreakerLLMClient.generate(messages, tools, on_slow)`:

1. If the circuit is open: if the cooldown elapsed and no probe is in flight, this call becomes the probe (`_probe_in_flight = True`, logged as HALF-OPEN). Otherwise, if a fallback backend exists, call `_call_fallback`. If not, count `circuit_open` in the metrics and return the static clarification "I'm still processing your request, please give me a moment.".
2. Record the start time with `clock.monotonic()` (virtual time aware).
3. Run `_generate_with_deadlines` (next section). On success: `_record_success`, count `ok`, observe `LLM_LATENCY`, return the response.
4. On `asyncio.TimeoutError` or `TimeoutError`: count `timeout`, `_record_failure`. If the circuit is now open and a fallback exists, use it; otherwise return the clarification "Still pulling that data together, one second...".
5. On any other exception: count `rate_limited` for a `RateLimitError` and `error` for anything else, `_record_failure`. With the circuit open and a fallback, use the fallback; otherwise return the clarification "I hit a snag reaching my reasoning engine — could you try that again?".
6. In a `finally` block, the probe flag is cleared so one stuck probe cannot block recovery.

Notice a subtlety: the first two failures never reach the fallback. They return the static clarification (the circuit is still closed). The fallback is used only once the circuit is open (third failure and later). Whether this is intended is not stated in the code; it is how it behaves, and it is covered by `test_circuit_breaker_routes_to_fallback_backend_once_open`.

Another subtlety: the breaker returns failure as an `LLMResponse` of type `clarification`; it never raises to the planner. As a result, on a persistent outage the user sees one of the three messages above in the chat as a question from the agent.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `CircuitBreakerLLMClient` | `agent/llm_client.py` | breaker plus deadlines plus fallback routing |
| `_record_failure`, `_record_success`, `_cooldown_elapsed` | `agent/llm_client.py` | state transitions |
| `_generate_with_deadlines` | `agent/llm_client.py` | soft and hard deadline logic |
| `_call_fallback` | `agent/llm_client.py` | route to the fallback backend |
| `get_backend`, `get_fallback_backend` | `agent/llm_client.py` | factories |
| `RateLimitError`, `ToolCallRejectedError` | `agent/llm_client.py` | provider error types |
| `_request_with_rate_limit_retry`, `_parse_retry_after` | `agent/llm_client.py` | 429 handling |
| `_extract_memory_update` | `agent/llm_client.py` | MEMORY_UPDATE parsing |
| `LLMConfig`, `LLMResponse`, `LLMBackend` | `agent/llm_client.py` | configuration, result, interface |

### Metrics

The client uses `agent/metrics.py`: `LLM_REQUESTS` with an `outcome` label (`ok`, `timeout`, `error`, `rate_limited`, `fallback`, `circuit_open`), `LLM_LATENCY` (histogram of successful calls), `LLM_CIRCUIT_OPEN` (gauge, 1 when open), and `LLM_SOFT_DEADLINE` (counter). They are exposed at `/metrics` (bearer protected when `AUTH_TOKEN` is set), as noted in `README.md`.

### Tests

`tests/test_circuit_breaker_recovery.py`: recovery after the cooldown when the probe succeeds, a failed probe re-opening the circuit for another cooldown, only one concurrent probe, backend errors degrading gracefully and tripping the breaker, and a success resetting the failure count. `tests/test_llm_client.py`: timeout fallback, routing to the fallback backend once open, a failing fallback still returning a response, backend selection (Groq over OpenRouter, OpenRouter without Groq), plus live tests marked `live` that call real providers.

## 7. Soft and hard deadlines

### Purpose

A slow answer should not look like a hang, but an answer that is merely slow should not be thrown away. The design uses two deadlines (comment in `LLMConfig`):

- Soft deadline (`soft_deadline_s`, default 2.0 s): if the model has not answered, the caller is told once so it can show a progress line, and the request keeps waiting.
- Hard deadline (`timeout_s`, default 8 s for Groq): the request is abandoned and treated as a failure.

### How it works

`_generate_with_deadlines(messages, tools, on_slow)`:

1. If no `on_slow` callback is given, or the soft deadline is not strictly between 0 and the hard deadline, the call is simply `asyncio.wait_for(backend.generate(...), timeout=hard)`.
2. Otherwise the backend call becomes a task. `asyncio.wait({task}, timeout=soft)` waits for the soft deadline. If the task finished, return its result.
3. If not, increment `LLM_SOFT_DEADLINE`, await `on_slow()` (exceptions are logged and swallowed, since a failing progress line must never fail the request), then `asyncio.wait_for(task, timeout=max(0, hard - soft))` for the remainder.
4. In a `finally`, an unfinished task is cancelled.

The coordinator's `_slow_notice(session, plan_epoch)` is the callback. It emits a filler "Still working on that -- one moment." only if the epoch has not changed, so a stale plan stays silent.

A detail about the hard deadline: `asyncio.wait_for` cancels the awaiting wrapper, but the actual HTTP request runs in a thread through `run_in_executor` with a `urllib` timeout of the same `timeout_s`. A cancelled await does not forcibly stop the thread; the thread ends when urllib's own socket timeout fires. So the abandoned request can still count against the provider's quota, and for the 429 path `asyncio.sleep` inside the backend would be cancelled with the task.

Because the HTTP library timeout is also `timeout_s`, a single urllib socket timeout can fire at about the same moment as the asyncio deadline; either way the breaker sees a timeout or an error and treats it as one failure.

### Tests

`tests/test_latency_levers.py`: the soft deadline fires once and then keeps waiting for the answer; fast answers never trigger the progress line; the hard deadline still applies and cancels the call; a failing progress callback cannot fail the request; no callback or a disabled soft deadline behaves as before; provider usage is parsed defensively. The same file also covers voice endpointing levers, which belong to the voice chapter.

## 8. The fallback model

### Purpose

When the primary circuit is open, a smaller or faster model can keep answering instead of the static "please give me a moment" text. Per the `.env.example` comment it is used until a half-open probe closes the primary circuit.

### How it works

`get_fallback_backend()` returns None unless `LLM_FALLBACK_MODEL_NAME` is set (a fallback with no model configured would only duplicate the primary). If set, it builds a second `LLMConfig` with:

| Field | Value |
|---|---|
| `backend_type` | `LLM_FALLBACK_BACKEND_TYPE`, default the same selection as the primary (`_default_backend_type()`) |
| `model_name` | `LLM_FALLBACK_MODEL_NAME` |
| `api_key` | `LLM_FALLBACK_API_KEY`, else `GROQ_API_KEY`, else `OPENROUTER_API_KEY`, else `OPENAI_API_KEY` |
| `base_url` | `LLM_FALLBACK_BASE_URL`, else `LLMConfig().base_url` |
| `timeout_s` | `LLM_FALLBACK_TIMEOUT_S`, default 4.0 |

`Planner.__init__` calls it when constructing its client.

`_call_fallback` increments `fallback_calls`, counts the `fallback` outcome and calls the fallback backend under `asyncio.wait_for(..., timeout=self.config.timeout_s)`. Note the wait uses the primary's `timeout_s`, while the fallback backend's own `urllib` timeout is the fallback config's `timeout_s` (4.0 by default), so the shorter one governs. The response gets `via_fallback = True`. If the fallback also fails, the result is a clarification ("I'm having trouble reaching my reasoning engine right now — please try again shortly.") flagged `via_fallback`.

The fallback does not have its own breaker, retry for 429 beyond what its backend class provides, or recovery logic. It is simply the route taken while the primary's breaker is open. It is off by default; the `.env.example` suggested model is `llama-3.1-8b-instant` on Groq. Whether that model is enabled for a given Groq account is account specific (see the model note above).

### Tests

`tests/test_llm_client.py`: `test_circuit_breaker_routes_to_fallback_backend_once_open` and `test_circuit_breaker_fallback_failure_still_returns_a_response`.

## 9. Environment variables (summary)

| Variable | Default | Effect |
|---|---|---|
| `GROQ_API_KEY` | none | selects Groq (highest priority); model default `openai/gpt-oss-120b`, timeout 8 s |
| `OPENROUTER_API_KEY` | none | selects OpenRouter if no Groq key; model default `qwen/qwen-2.5-7b-instruct`, timeout 5 s |
| `OPENAI_API_KEY` | none | only used as an `api_key` value; does not by itself select a backend |
| `USE_LOCAL_LLM` | none | selects the local backend if no cloud key; `base_url` default `http://localhost:8000/v1`, timeout 2 s |
| `LLM_MODEL_NAME` | see above | model identifier, overrides the per backend default |
| `LLM_BASE_URL` | per backend | endpoint base, `/chat/completions` is appended |
| `LLM_TIMEOUT_S` | 8 / 5 / 2 | hard deadline in seconds |
| `LLM_SOFT_DEADLINE_S` | 2.0 | progress line threshold; 0 disables |
| `LLM_RATE_LIMIT_MAX_WAIT_S` | 3.0 | longest 429 wait that is honoured by the one retry |
| `LLM_CIRCUIT_COOLDOWN_S` | 15.0 | time the circuit stays open before a probe |
| `LLM_FALLBACK_MODEL_NAME` | none (fallback disabled) | enables the fallback and names its model |
| `LLM_FALLBACK_BACKEND_TYPE` | same as primary selection | backend class for the fallback |
| `LLM_FALLBACK_API_KEY` | falls back to the primary keys | key for the fallback |
| `LLM_FALLBACK_BASE_URL` | primary base URL | endpoint for the fallback |
| `LLM_FALLBACK_TIMEOUT_S` | 4.0 | the fallback backend's own request timeout |

Not configurable by env: the failure threshold (3), the temperature (0.1) and the 429 retry count (1). Related variables from other chapters: `WARMUP_LLM=1` makes the server send one tiny LLM request at boot and `POST /warmup` does the same on demand (rate limited, because it spends tokens).

## 10. Interactions with other components

- Coordinator: `AgentCoordinator.__init__` calls `get_backend(LLMConfig())` when no backend is injected, and hands it to the planner. The eval runner wraps the backend in a recording backend and injects either the scripted mock (mock mode) or `get_backend(LLMConfig())` (live mode).
- Planner: the only caller of `CircuitBreakerLLMClient.generate`; passes the messages, tool manifests and the `on_slow` callback; receives `LLMResponse`.
- Validator: `GroqBackend` and `LocalQwenBackend` use `validate_and_repair` for tool call argument strings.
- Metrics and clock: `agent/metrics.py` for counters, histograms and the gauge; `agent/clock.py` (`clock.monotonic()`) for the cooldown and latency, so the eval harness can run in virtual time. Note the `asyncio.sleep` in the 429 retry also goes through the event loop clock, which the virtual loop controls.
- Server: `/warmup` and the optional boot warm up make a request through the same path.
- Memory: `memory_update` is consumed by the planner and fed to the scratchpad.

## 11. Failure modes and guarantees (summary)

| Failure | What happens | User sees |
|---|---|---|
| Slow answer (over 2 s) | `on_slow` fires once, wait continues | "Still working on that -- one moment." |
| Over the hard deadline | counted as timeout; failure counter up | "Still pulling that data together, one second..." |
| Short 429 (up to 3 s hint) | one retry; invisible to the breaker | slightly slower normal reply |
| Long or hint-less 429 | counted `rate_limited`; failure counter up | "I hit a snag reaching my reasoning engine..." |
| Other HTTP error or network failure | counted `error`; failure counter up | the same snag message |
| Model's tool call rejected (Groq 400) | resampled once; twice means clarification, no breaker impact | "I couldn't work out that request cleanly..." |
| 3 consecutive failures | circuit opens | fallback model, else "I'm still processing your request..." |
| Cooldown (15 s) elapsed | one probe; success closes the circuit | normal service resumes |
| Fallback fails too | clarification with `via_fallback` | "I'm having trouble reaching my reasoning engine..." |
| No key at all | mock backend | canned replies as described above |

Guarantees: the breaker never raises to the planner; a thrown exception from a provider becomes a clarification. A failing progress callback cannot fail a request. The `MEMORY_UPDATE` marker is always stripped. Concurrent callers during half-open cannot send more than one probe (`_probe_in_flight`).

Limits worth stating: the breaker state is per `CircuitBreakerLLMClient`, which is created per `Planner`; the coordinator creates one planner, so the state is shared across sessions of that coordinator. The static failure messages are indistinguishable from a model's genuine clarification question in the action stream (both are `clarification` actions); the eval harness and the demo notes treat the text "I hit a snag" as the sign of an exhausted quota. The mock backend cannot produce any of the failure paths because it never fails; they are exercised in tests with scripted backends.

## 12. Worked examples

### Example A: a short rate limit

1. A user message reaches the planner; Groq returns HTTP 429 with the body "Please try again in 1.905s".
2. `GroqBackend._do_request` raises `RateLimitError(retry_after_s=1.905)`.
3. `_request_with_rate_limit_retry` sees 1.905 is not above 3.0, sleeps 2.005 s and re-requests, which succeeds.
4. The breaker sees a normal success: `outcome=ok`, failure counter reset. During the wait the soft deadline (2.0 s) fires `on_slow`, so the user saw "Still working on that -- one moment." The total latency is about 2 s plus the request time, well inside the 8 s hard deadline.

### Example B: provider outage

1. Three consecutive requests time out (8 s each, or fail instantly with HTTP 500). After the third, `_record_failure` opens the circuit and stamps `_opened_at`.
2. With `LLM_FALLBACK_MODEL_NAME` set, that third request is answered by the fallback model immediately; later requests go straight to the fallback. Without a fallback, they get "I'm still processing your request, please give me a moment." with no waiting.
3. After 15 s the next request is the probe. If the primary answers, the circuit closes; if it fails, the cooldown restarts.

### Example C: no API key

1. Neither key nor `USE_LOCAL_LLM` is set, so the backend is `MockLLMBackend`.
2. The user types "find me a flight to Goa". The filler appears at once; the mock reads the last message, matches `flight` and returns `search_flights` with BLR to DEL (not Goa).
3. The planner validates and registers the call; the scripted environment's tool produces a result; `summarize_tool_result` reads it out. The user hears a flights summary for BLR to DEL.
4. The user then says "no wait, make it Mumbai". Tier 1 acts (epoch bump, cancellation, acknowledgement). The mock again returns BLR to DEL. The mechanics of interruption are all visible; the content is canned.

## 13. Known issues and inconsistencies

- `.env.example` ships an active placeholder `OPENROUTER_API_KEY` and an active `LLM_MODEL_NAME`; a plain copy to `.env` selects OpenRouter with a fake key instead of the mock (section 3).
- `OpenRouterBackend` bypasses `validate_and_repair` and the tool call rejection handling that Groq has; `LocalQwenBackend` has no 429 retry.
- `LLMResponse.latency_s` exists but the backends do not set it; latency is recorded in metrics instead.
- Only the first tool call in a model response is used.
- `README.md` says the circuit breaker opens on "failures"; the constant is named `max_consecutive_timeouts` but counts all failures, including errors.
- The first two failures return the static message even when a fallback model is configured; the fallback starts at the third.
- Abandoned requests continue in a worker thread until the urllib timeout, so a timeout does not instantly stop provider usage.
- The only Groq model confirmed for the organisation has a 200k tokens per day cap (from `CLAUDE.md`); a full live eval or a demo recording can exhaust it, after which every turn yields the "snag" message until the quota resets.
