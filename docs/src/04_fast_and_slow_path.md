# Fast Path, Slow Path and Reply Summaries (Tiers 1, 2 and 3)

## 1. Overview: why three tiers

Kairos has one hard product requirement: the user must feel that the agent reacts immediately, even though the real reasoning step is a network call to a large language model that takes seconds. The code answers this with a three tier design. Each tier has a different latency class, a different failure profile and a different job.

| Tier | Job | Code | Cost class | Can it be wrong? |
|---|---|---|---|---|
| Tier 1 | Decide whether an utterance is an interruption or correction of work in flight | `agent/fast_path/intent_classifier.py` | a few milliseconds of CPU, inline | yes, so it is confidence gated |
| Tier 2 | Say something at once (filler or acknowledgement) | `agent/fast_path/templates.py` | microseconds, no model | no claims of completion are ever made |
| Tier 3 | Understand the request, choose a tool and arguments, or reply | `agent/slow_path/planner.py` | one LLM round trip (seconds) | yes, so output is validated before dispatch |
| Reply summaries | Turn a finished tool result into a spoken sentence | `agent/tool_summaries.py` | microseconds, no LLM | deterministic |

The design principle, repeated in the module docstrings, is that nothing on the latency sensitive path waits for the LLM. The filler is emitted before the planner is even called. The summary of a tool result is built from a template instead of asking the model "please describe this result". The classifier never blocks on its own model load.

The three tiers are orchestrated by `AgentCoordinator._handle_user_text` in `agent/coordinator.py`. This document covers the tier components themselves and how that function wires them together. The coordinator, epoch model and session state are described in their own chapters; the LLM client that the planner calls is described in the next chapter (`05_llm_layer.md`).

### The data flow for one typed message

```
 user text
    |
    v
 debounce (0.10 s window, coalesces bursts)         coordinator._flush_after_delay
    |
    v
 _handle_user_text
    |-- pending "did you mean to change that?" answer? --> yes: replay original, force interrupt
    |                                                      no : "Okay, I'll carry on" and stop
    v
 Tier 1  _classify(text) -> {decision: act | clarify | continue, confidence_score, ...}
    |        logged to the trace (TraceLogger.log_classification)
    |
    |-- clarify AND work in flight AND not otherwise interrupt --> ask (yes / no), STOP
    |-- act --> session.bump_epoch(), cancel stale calls
    v
 Tier 2  generate_filler(intent, slots, is_interruption) -> FillerAction   (immediate)
    v
 Tier 3  planner.plan(event, session, plan_epoch)   -- LLM call, soft/hard deadlines
    |        stale (epoch moved on) ? -> return [] and emit nothing
    |        tool_call  -> validate -> stage slots -> register call -> ToolCallAction
    |        clarification / spoken_response -> action
    |        always: state snapshot
    v
 _dispatch_actions -> emit actions, spawn tool task
    ...later, tool result arrives...
 summarize_tool_result / summarize_tool_error -> SpokenResponseAction (no LLM)
```

### Latency budget

The code and docstrings give these targets and measures. They are targets stated in comments, not guarantees measured here; measured results come from the eval harness and the `/metrics` histograms.

| Stage | Budget stated in code | Where stated |
|---|---|---|
| Tier 1 classification | "< 5ms" (module docstring); "~5 ms of CPU" (coordinator comment); MiniLM about 3 ms per sentence on CPU | `intent_classifier.py`, `coordinator.py::_classify` |
| Tier 2 filler | "< 50ms" | `templates.py`, `coordinator.py` |
| Debounce window | 0.10 s default (`debounce_window_s`) | `coordinator.py` constructor |
| Soft LLM deadline | 2.0 s default, then a "Still working on that" progress line | `llm_client.py` (`LLM_SOFT_DEADLINE_S`) |
| Hard LLM deadline | 8 s Groq, 5 s OpenRouter, 2 s local | `llm_client.py` (`LLM_TIMEOUT_S`) |

The practical effect is that the first visible reaction (filler or interruption acknowledgement) is limited by the debounce window plus a few milliseconds of classification, not by the model.

## 2. Tier 1: the interrupt and intent classifier

### Purpose

`IntentClassifier` in `agent/fast_path/intent_classifier.py` answers one question about every user utterance: is the user stopping, cancelling or correcting the work that is already in flight, or is this an ordinary continuation? The label is deliberately narrow (see `agent/fast_path/intent_dataset.py`): INTERRUPT means retracting or correcting work in flight ("no wait, make it Mumbai", "stop", "scratch that"). Everything else is CONTINUE, including a fresh request, an additive detail, a question, small talk or a confirmation. A new request is not an interrupt by itself; it is the planner that decides whether it supersedes something.

The classifier is confidence gated (the spec section it cites is 7.2). It does not return a boolean; it returns one of three decisions.

| Decision | Condition | Coordinator behaviour |
|---|---|---|
| `act` | probability >= `high` | bump the epoch and cancel stale in-flight calls immediately |
| `clarify` | `mid` <= probability < `high` | if something is running, ask the user "Do you want me to change what I'm working on...? (yes / no)" and do not touch the epoch |
| `continue` | probability < `mid` | ordinary request or noise |

The reason for the middle band is cost asymmetry: a false `act` cancels real work the user wanted, while a false `continue` merely misses a correction (which the planner can still handle). So uncertain cases ask instead of thrashing the epoch.

### How it works, step by step

1. `classify_text(text)` lowercases and strips the input. Empty input returns `decision: continue`, score 0.0.
2. Unless `force_keywords=True`, `_load_model()` is called. It lazily loads the sentence-transformers model `all-MiniLM-L6-v2` once per process (guarded by `_MODEL_LOCK` and cached in `_MODEL_CACHE`), pinned to CPU (`INTENT_DEVICE`, default `cpu`) and `INTENT_THREADS` (default 2) torch threads. It also pre-encodes two anchor lists: `INTERRUPT_ANCHORS` (32 phrases such as "stop", "scratch that", "no I want the other date") and `CONTINUATION_ANCHORS` (24 phrases such as "yes please", "find me a flight"). If the import or load fails, the classifier logs a warning and permanently sets `use_embeddings = False` for that instance.
3. In embedding mode the query is encoded and two numbers are taken: the maximum cosine similarity against the interrupt anchors and against the continuation anchors.
4. `feature_vector()` builds an 8 element vector: the two similarities, five lexical flags from `lexical_features()`, and a bias of 1.0. The feature names are in `FEATURES`: `interrupt_similarity`, `continuation_similarity`, `strong_keyword`, `opening_keyword`, `repair_word`, `benign_no`, `strong_in_long`, `bias`.
5. `interrupt_probability()` applies a logistic function to the dot product of the vector and the weights loaded from `agent/fast_path/intent_weights.json`.
6. `apply_rules()` applies two linguistic overrides that, in the words of its docstring, no calibration data should be needed to get right. A benign "no" opener ("no problem", "no worries", "no thanks", "no rush"...) with no strong word is capped at probability 0.2 (rule label `benign_no`). A bare stop, meaning a strong word in an utterance of at most 3 words, is raised to at least 0.99 (rule label `bare_stop`).
7. `decide(score, high, mid)` maps the probability to `act`, `clarify` or `continue`.
8. The returned dictionary carries `is_interrupt`, `needs_clarification`, `confidence_score`, `decision`, `mode` ("embedding"), the `rule` that fired (or None), the full `features` dictionary and the raw similarities. The coordinator writes this to the trace so a threshold can be audited after the fact.

### The lexical features

MiniLM alone scores "no wait, make it Mumbai" only about 0.35 against the anchors because the sentence is mostly about the new value, not the retraction (this is documented in a code comment). So the lexical cues are combined with the similarities.

| Feature | Regex or rule | Meaning |
|---|---|---|
| `strong_keyword` | `_STRONG`: stop, cancel, abort, scratch that, hold on, never mind, forget it, hang on | words that retract work |
| `opening_keyword` | `_WEAK_OPENING`: no, nope, actually, wait, sorry, correction, oh wait, hey, only at the start | weak words that count only when they open the utterance |
| `repair_word` | `_REPAIR`: not, instead, meant, change, switch, make it, should be, wrong, other, different, correction, undo, back | marks a correction |
| `benign_no` | `_BENIGN_NO`: "no" followed by problem, worries, thanks, rush, need, hurry | pleasantries that open with "no" |
| `strong_in_long` | strong keyword and at least 6 words | "cancel my booking for Friday..." is an instruction, not an interruption |

### The keyword fallback

When embeddings are off, unavailable, or not yet loaded, the classifier uses a much simpler path: a whole word regex `_INTERRUPT_KEYWORDS` (no, stop, wait, cancel, actually, scratch that, instead, hold on). A match gives `decision: act` with score 0.85; no match gives `continue` with score 0.10. This mode never returns `clarify`, and its result has `mode: "keyword"`. The regex is whole word on purpose: a code comment explains that an older substring test (`" no" in text`) fired on "now", "north" and "nothing" and would have cancelled work on sentences like "flights now".

### When embeddings are used

`embeddings_enabled()` reads `INTENT_EMBEDDINGS`:

| Value | Result |
|---|---|
| `1`, `true`, `yes`, `on` | force MiniLM |
| `0`, `false`, `no`, `off` | force keyword heuristic |
| unset or `auto` (default) | MiniLM if the `sentence_transformers` package is importable, otherwise keywords |

`sentence-transformers` is an optional dependency (`pip install -e ".[embeddings]"`). Without it the system silently runs keyword mode. This matters for judges: keyword mode behaves differently from the calibrated model (no `clarify` band, no lexical weighting), and the eval harness can be run either way (`INTENT_EMBEDDINGS=0 python -m agent.eval --llm mock --virtual` skips the roughly 10 second model load).

### Never blocking on the model load

The coordinator wraps the classifier in `AgentCoordinator._classify`. If `classifier.ready` is true (keyword mode, or the model already loaded), it calls `classify_text` inline and deliberately does not yield to another thread. A code comment explains why: when turn handlers for events 150 ms apart run as separate tasks, a thread hop could let a later correction finish classification first, reorder the epoch bumps and let the stale plan win. This was found by the eval scenario `rapid_fire_corrections` (it booked "Boston" instead of "Chicago").

If the model is not ready yet, `_classify` starts `classifier.aload()` in the background (which runs `_load_model` through `asyncio.to_thread`) and answers this call with `classify_text(text, force_keywords=True)`. So during the first seconds after start, interrupts still work, with the weaker keyword heuristic. `README.md` notes that the server warms models at startup and `/health` reports progress.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `IntentClassifier` | `agent/fast_path/intent_classifier.py` | the classifier; `classify_text`, `aload`, `ready` |
| `INTERRUPT_ANCHORS`, `CONTINUATION_ANCHORS` | `agent/fast_path/intent_classifier.py` | anchor phrases, disjoint from the labelled set |
| `lexical_features`, `feature_vector` | `agent/fast_path/intent_classifier.py` | build the model input |
| `interrupt_probability` | `agent/fast_path/intent_classifier.py` | logistic scoring |
| `apply_rules`, `is_bare_stop` | `agent/fast_path/intent_classifier.py` | rule overrides after the model |
| `decide` | `agent/fast_path/intent_classifier.py` | confidence gate: act, clarify, continue |
| `embeddings_enabled` | `agent/fast_path/intent_classifier.py` | reads `INTENT_EMBEDDINGS` |
| `load_calibration` | `agent/fast_path/intent_classifier.py` | reads `intent_weights.json` |
| `AgentCoordinator._classify` | `agent/coordinator.py` | non blocking wrapper |

### Configuration

| Variable | Default | Effect |
|---|---|---|
| `INTENT_EMBEDDINGS` | `auto` | force or disable the MiniLM mode (see above) |
| `INTENT_THREADS` | `2` | torch intra-op threads while MiniLM runs |
| `INTENT_DEVICE` | `cpu` | device for the sentence-transformers model |
| constructor arguments `high_threshold`, `mid_threshold` | from `intent_weights.json` (0.6 and 0.25) | override thresholds (used by tests and calibration) |
| constructor argument `model_name` | `all-MiniLM-L6-v2` | embedding model |

### Failure modes and guarantees

- Model cannot load: falls back to keyword mode for that instance, with a logged warning. No exception reaches the coordinator.
- Empty text: always `continue`, score 0.
- Model not yet loaded: keyword answer now, model load in the background; never a stall of the event loop.
- Keyword mode has no `clarify` band, so a misjudged correction is either acted on (score 0.85) or missed.
- A shared module level cache means a coordinator per session or per test does not pay the load again.
- The classifier only sees the single utterance. It has no access to the conversation, so "no" as an answer to a question is handled by the coordinator (`pending_clarification` and `_yes_no`), not by the classifier.
- The `INTERRUPT_ANCHORS` list is a small curated list; the calibration set deliberately avoids duplicating it so measured accuracy is not memorisation (enforced by `labeled()` removing any utterance that equals an anchor, and tested).

## 3. Calibration: dataset, weights and thresholds

### Purpose

The two similarities and five lexical flags need weights and two thresholds. Rather than hand tuning, `agent/fast_path/calibrate.py` fits them from a labelled dataset in `agent/fast_path/intent_dataset.py` and writes `agent/fast_path/intent_weights.json`. The shipped JSON file is the result of running that script; the classifier only reads the file at construction time.

### The labelled dataset

`intent_dataset.py` provides `INTERRUPT` and `CONTINUE` lists written the way speech arrives, including truncated voice partials such as "no wa", "sto", "find me fli". The docstring is explicit about label meaning (see the Purpose section above). It contains deliberate hard negatives, for example "cancel my booking for Friday's flight after it's confirmed" (a request, not an interruption), "no problem", "nothing else for now", "nowhere in particular" and "now show me hotels" (words that start with "no" but are not retractions).

A small composition step (`_composed`) generates extra utterances from templates, such as slot corrections ("no, make it Goa") and fresh requests ("find flights to Pune"), using `random.Random(7)` so the output is deterministic. The module comment states honestly that these are composed so the set has breadth to calibrate on, without pretending they are independent samples.

`labeled()` returns de-duplicated `(utterance, is_interrupt)` pairs and removes anything that equals an anchor. `split(seed=13, dev_fraction=0.5)` returns a stratified, deterministic dev/test split.

### How calibration works

1. `featurize` classifies each utterance with the real classifier to obtain its similarities, then builds `feature_vector` rows.
2. `fit_logistic` runs plain gradient descent (5000 iterations, learning rate 0.5, L2 term on the non-bias weights) on the dev split only.
3. `probabilities` applies the same `apply_rules` overrides used at runtime, so thresholds are fitted to what actually runs.
4. `choose_thresholds` scans a grid from 0.20 to 0.95 in steps of 0.05. `high` is the lowest probability whose dev precision for `act` is at least 0.98 (fallback 0.9). `mid` is the lowest probability below `high` for which the `[mid, high)` band is at least 50 percent real interrupts.
5. `metrics` reports `act_precision`, `act_recall`, `clarify_n`, `clarify_precision`, `handled_recall` (interrupts either acted on or asked about), `false_act`, `false_clarify` and `accuracy_at_high`, on both dev and held-out test.
6. The script also reports a keyword baseline accuracy next to the embedding plus lexical accuracy on the test split.
7. With `--write`, it stores weights, thresholds and a `fit` block in `intent_weights.json`. With `--errors` it lists misclassified test utterances.

Run it with:

```
python -m agent.fast_path.calibrate            # report only
python -m agent.fast_path.calibrate --errors   # also list mistakes
python -m agent.fast_path.calibrate --write    # overwrite intent_weights.json
```

Calibration needs `numpy` and the sentence-transformers model (it constructs `IntentClassifier()` with embeddings).

### The shipped calibration

The committed `intent_weights.json` contains these values. The `fit` block records the dataset sizes and the held-out test metrics at fit time.

| Item | Value |
|---|---|
| weights, in `FEATURES` order | 2.1941, -1.0081, 1.7162, 1.8266, 1.9393, -0.502, -0.5744, -1.5339 |
| `high` | 0.6 |
| `mid` | 0.25 |
| `dev_n` / `test_n` | 90 / 92 |
| test `act_precision` | 1.0 |
| test `act_recall` | 0.949 |
| test `handled_recall` | 1.0 |
| test `false_act` / `false_clarify` | 0 / 4 |
| test `clarify_n` / `clarify_precision` | 6 / 0.333 |
| test `accuracy_at_high` | 0.978 |

Reading the weights: the interrupt similarity has a large positive weight, continuation similarity a negative one, each of the three positive lexical flags adds about 1.7 to 1.9, and the bias is negative so that plain sentences start well below 50 percent. `benign_no` and `strong_in_long` pull the score down.

An honest reading of these figures: the dataset is small (182 utterances in total) and is partly template generated, so the 100 percent act precision on 92 test items is a useful regression signal, not a claim of production accuracy. The `clarify` band is the cheap safety margin: four of the six clarified test items were not real interrupts, which costs the user one extra "yes / no" question and nothing else.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `INTERRUPT`, `CONTINUE` | `agent/fast_path/intent_dataset.py` | hand written labelled utterances |
| `_composed`, `labeled`, `split` | `agent/fast_path/intent_dataset.py` | augmentation, dedupe, dev/test split |
| `featurize`, `fit_logistic`, `probabilities`, `metrics`, `choose_thresholds`, `main` | `agent/fast_path/calibrate.py` | the fit and report pipeline |
| `intent_weights.json` | `agent/fast_path/intent_weights.json` | features, weights, `high`, `mid`, fit record |

### Failure modes

- The weights file is read at every `IntentClassifier()` construction with no fallback. A missing or corrupt file raises at coordinator construction.
- If `FEATURES` changes without refitting, the weight vector length no longer matches; `interrupt_probability` uses `zip`, which silently truncates. Refit and `--write` after any feature change.
- Calibration changes the thresholds, which the scenario eval can then detect (interrupt scenarios depend on `act`).

### Tests

- `tests/test_intent_calibration.py`: dataset is disjoint from the anchors and reasonably sized; held out accuracy beats the keyword baseline with no false acts; results carry raw scores and clear cases act; the mid band asks instead of bumping the epoch, then "yes" acts on the original text and "no" keeps work running; a mid band utterance with nothing running is an ordinary request; the classification is written to the trace; short spoken corrections act immediately. These tests need the embedding model for the calibration measurements.
- `tests/test_intent_classifier.py`: keyword mode behaviour ("stop please", "no actually wait, change date to next week" act; "and also book a window seat" continues; empty input).

### Worked example

Scenario: the user typed "find flights from Delhi to Mumbai" and a `search_flights` call is running. Then they say "no wait, make it Pune".

1. `_classify("no wait, make it Pune")` runs inline (the model is loaded). The lowercase text matches `_WEAK_OPENING` ("no"), has no `_STRONG` word, but matches `_REPAIR` ("make it"). Flags: opening 1, repair 1, strong 0, benign_no 0, strong_in_long 0.
2. Suppose the raw similarities are interrupt 0.35 and continuation 0.30 (the code comment says about 0.35 for this kind of sentence). Then z = 2.1941 x 0.35 + (-1.0081) x 0.30 + 1.8266 + 1.9393 - 1.5339 = 0.7679 - 0.3024 + 1.8266 + 1.9393 - 1.5339 = 2.6975, probability about 0.937. The similarity values are an assumption for illustration; the arithmetic uses the real weights.
3. No rule fires, and 0.937 >= `high` (0.6), so the decision is `act`.
4. `_handle_user_text` calls `session.bump_epoch(reason="user_correction: ...")` and emits the cancellations.
5. The filler is `generate_filler(..., is_interruption=True)`, which returns "Got it, changing that." immediately.
6. The planner is called with the new epoch; the LLM sees the corrected request and issues a new `search_flights` call. Any result from the earlier call would be dropped by the epoch model.

A contrasting example: "no worries" while work is running. `_BENIGN_NO` matches and there is no strong word, so `apply_rules` caps the probability at 0.2 (rule `benign_no`), below `mid` (0.25), giving `continue`. Another: "abort" is three words or fewer with a strong word, so the `bare_stop` rule raises it to 0.99 regardless of MiniLM.

## 4. Tier 2: filler and acknowledgement templates

### Purpose

`agent/fast_path/templates.py` produces the instant line the user sees or hears while the planner is still thinking. The module docstring states two goals: zero hallucination risk on false completion claims (a template can say "Looking up flights..." but never "I booked it"), and immediate response (under 50 ms).

### How it works

`generate_filler(intent, slots, is_interruption, deterministic)` is a pure function with no model and no I/O.

1. If `is_interruption` is true, it returns `INTERRUPT_ACKS[0]` ("Got it, changing that.") when `deterministic` is true, or a random choice among the four entries of `INTERRUPT_ACKS` ("Got it, changing that.", "Understood, switching gears.", "Cancelled, let's adjust that.", "Stopping that right away.") when false.
2. Otherwise it looks up `INTENT_TEMPLATES[intent]`. This table has entries for `search_flights`, `book_flight`, `search_hotels`, `book_hotel`, `check_weather` and `calendar_event`, each a short list of phrases, some with `{origin}`, `{destination}` or `{city}` placeholders.
3. Each template is tried in order with `template.format(**slots)`. A `KeyError` means a slot is missing, so that template is skipped. If the formatted text still contains `{` it is rejected as well. The first complete one is returned.
4. If none filled, the last template of that intent is used if it has no placeholder (for example "Searching available flights for you now..."). Otherwise a generic filler is returned ("Looking that up right now...", chosen with the same deterministic switch).

The coordinator calls it with `deterministic` left at its default of true, so in production the first complete template always wins and the output is predictable and testable.

### Where it is called

- `_handle_user_text` emits a `FillerAction` before the planner runs. It passes `session.intent` and `session.slots` from the previous state and `is_interruption=is_interrupt`. For a first message of a new conversation the intent and slots are empty, so the filler is the generic "Looking that up right now...". Note that the filler is therefore generic for fresh requests and specific only when the session already has an intent and slots.
- `_handle_interrupt` (the explicit interrupt event, such as a barge-in with no follow-up text) emits an acknowledgement only if something was actually cancelled or a plan was in flight, so a stray interrupt does not produce a spurious "Stopping that right away".
- The soft LLM deadline progress line "Still working on that -- one moment." is produced by `AgentCoordinator._slow_notice`, not by `templates.py`. It is suppressed if the epoch has moved on.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `generate_filler` | `agent/fast_path/templates.py` | pick an acknowledgement or filler |
| `INTENT_TEMPLATES` | `agent/fast_path/templates.py` | per-intent phrases with slot placeholders |
| `INTERRUPT_ACKS`, `GENERIC_FILLERS` | `agent/fast_path/templates.py` | fixed lists |

### Configuration

None. There are no environment variables. Choice is by arguments only.

### Failure modes and guarantees

- A template with a missing slot can never leak a raw `{placeholder}`; that case falls through to another template or the generic filler.
- Slot values are inserted as is. They come from validated slot patches, not raw user text.
- The filler is not tied to the real intent of the new utterance (the intent is taken before planning), so it can describe the previous task. This is a design trade off of acknowledging before understanding.
- Fillers carry the epoch at emit time; the client discards stale actions by epoch.

### Tests

`tests/test_templates.py`: slot filling, partial slot fallback, interruption acknowledgement.

## 5. Tier 3: the planner

### Purpose

`Planner` in `agent/slow_path/planner.py` is the only place where the LLM decides what to do. It builds the prompt, calls the LLM client, and converts the model's answer into the typed actions the coordinator can dispatch: a tool call, a clarification question or a spoken reply, always followed by a state snapshot. Its other responsibility is protection: it never registers or dispatches work that is stale, invalid or duplicated.

### Construction

`Planner(llm_backend, tool_router, config)` builds a `CircuitBreakerLLMClient` around the backend, with the optional fallback from `get_fallback_backend()` (see the LLM chapter). All deadline and retry behaviour therefore comes from `agent/llm_client.py`; the planner has no timing logic of its own, apart from passing `on_slow` through.

### How it works, step by step

`Planner.plan(event, session, plan_epoch, observation, on_slow)`:

1. **Build the messages.** One system message, then conversation history, then the user message.
   - The system prompt (inline in `planner.py`) tells the model: call the appropriate tool; call a booking tool directly when the user explicitly asks and has given details; treat a statement of need ("I need to get to...") as a search, not a booking; ask a clarifying question only when a required argument is missing; call `analyze_frame` when the user refers to something visible; use `export_artifact` for save, export or open in editor, and `set_timer` for countdowns; use `spawn_agent` to synthesize a bespoke worker for specialised tasks (schema, SVG, audit, code); and end the reply with a single `MEMORY_UPDATE: {...}` line if there are entities worth remembering.
   - It then appends `build_context_block(session)` from `agent/memory/context_builder.py`, which injects the state and memory context.
   - `build_history_messages(session)` supplies prior turns from the memory system; with no memory attached it returns an empty list and the request degrades to single turn.
   - The user message is the text itself. If an `observation` (a vision result) is given, it is appended as `[Vision result for the image the user is sharing: ...]`.
2. **Choose tools.** `tool_router.get_tool_manifests()` supplies the function schemas. If an observation is present, `analyze_frame` is filtered out so the continuation acts on what was seen instead of looking again (one step only, no loop).
3. **Call the LLM** through `self.client.generate(messages, tools=tools, on_slow=on_slow)`. This is the circuit breaker wrapper; it returns an `LLMResponse` even on timeout or error (a clarification message), so `plan` itself does not raise for provider failures.
4. **Stale plan check.** If `plan_epoch` is given and `session.epoch` has changed, the user interrupted while the model was thinking. The planner logs "Discarding stale plan" and returns an empty list, so nothing is emitted or registered. The coordinator treats an empty list as "superseded by a newer interrupt".
5. **Memory update.** If the response has a `memory_update` (parsed from the model text by `llm_client._extract_memory_update`) and the session has a scratchpad, each entity is recorded with `record_entity_candidate`, and an `intent_shift` is recorded with `record_intent_shift`. Malformed entities are logged and skipped.
6. **Branch on the response type.**
   - `tool_call`: described in detail below.
   - `clarification`: becomes a `ClarificationAction` with the model's question, or the default "Could you please clarify that detail?".
   - anything else (`spoken_response`): becomes a `SpokenResponseAction`, default text "I have processed your request.".
7. **Always append `session.get_snapshot()`** so the client state stays current.

### The tool call branch

For a `tool_call` with a tool name, the planner:

1. Generates a call identifier of the form `call_` plus 8 hex characters.
2. **Validates** with `tool_router.validate_call(tool_name, arguments)`, which raises for an unknown tool or arguments that fail the registered JSON schema. On failure it returns a `ClarificationAction` ("I couldn't put that request together properly -- could you give me the details again?") and a snapshot. An invalid call is never dispatched.
3. Reads `tool_router.is_state_modifying(tool_name)`.
4. **Stages slots first.** `entities_from_tool_call` (`agent/memory/tool_slots.py`) turns arguments into slot entities and `session.stage_call_slots(call_id, ...)` applies them, returning previous values. This must precede registration because the idempotency key hashes the slot values: two identical requests get the same key, two different ones do not. If the slot patch is refused (`SlotPatchError`), the session is unchanged, nothing is registered, and the user gets the error's `user_message` as a clarification.
5. **Registers** with `session.register_tool_call(call_id, tool_name, arguments, is_state_modifying)`. It returns a `ToolCallAction`, or `None` for a duplicate. For a duplicate, `session.revert_call_slots(call_id)` undoes the staged slots and nothing is dispatched; the user simply receives only a snapshot.
6. When registered, appends the `ToolCallAction`, and records the entities in the scratchpad linked to the call and with the previous value (so memory can mark real overrides).

Only the first tool call the model returns is used; the backends read `tool_calls[0]`. The planner dispatches at most one tool call per turn.

### The validator

`agent/slow_path/validator.py` is the repair layer for model output that is supposed to be JSON. Its stated purpose is that malformed or unvalidated output never dispatches.

`attempt_json_repair(raw_text)` tries, in order:

1. Strip markdown code fences (a fenced `json` block).
2. Direct `json.loads`.
3. Remove trailing commas before a closing brace or bracket, and if the text has single quotes and no double quotes, convert them.
4. Extract the outermost `{ ... }` and parse that.
5. If the optional `json_repair` package is installed, use `json_repair.loads` and accept a dict result.
6. Otherwise raise `ValueError` ("Unable to parse or repair JSON...").

`validate_and_repair(raw_output, schema=None)` calls the repair, then validates against a JSON schema with `jsonschema.validate` if a schema is given, raising `jsonschema.ValidationError` on mismatch.

In the current code this module is used inside the LLM client: `GroqBackend` and `LocalQwenBackend` pass the tool call argument string through `validate_and_repair` (with no schema). Schema validation of the arguments happens later in the planner through `tool_router.validate_call`. The `OpenRouterBackend` uses plain `json.loads` on the arguments and does not use the repair layer; a malformed argument string from an OpenRouter model therefore raises and is handled as a backend error by the circuit breaker. This is an inconsistency worth knowing about, and is noted again in the LLM chapter. The docstring's claim of "100% compliance" is aspirational; what is guaranteed is that anything that does not validate is not dispatched.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `Planner` | `agent/slow_path/planner.py` | builds the prompt, calls the LLM, makes actions |
| `Planner.plan` | `agent/slow_path/planner.py` | main entry: stale check, validate, stage slots, register |
| `Planner._reject` | `agent/slow_path/planner.py` | clarification plus snapshot for invalid input |
| `attempt_json_repair`, `validate_and_repair` | `agent/slow_path/validator.py` | JSON repair and schema validation |
| `ToolRouter.validate_call`, `is_state_modifying`, `get_tool_manifests` | `agent/coordination/tool_router.py` | schemas and validation |
| `build_context_block`, `build_history_messages` | `agent/memory/context_builder.py` | memory context for the prompt |
| `CircuitBreakerLLMClient` | `agent/llm_client.py` | deadlines, retry, fallback |

### Configuration

The planner has no environment variables of its own. It takes an optional `LLMConfig`, so everything is controlled through the `LLM_*` variables (next chapter). The system prompt is a constant in `planner.py`; changing agent behaviour means editing it.

### Interactions with other components

- Coordinator: `_handle_user_text` and `_continue_after_observation` call `plan`; `_dispatch_actions` emits the result and starts the tool tasks. The coordinator increments `_active_plans[sid]` around the call, which `_handle_interrupt` uses to decide whether to speak an acknowledgement.
- Session state: the planner uses `epoch`, `stage_call_slots`, `register_tool_call`, `revert_call_slots`, `get_snapshot` and `scratchpad`.
- Memory: `build_context_block`, `build_history_messages`, scratchpad entity candidates and intent shifts.
- Vision: `observation` is the output of `analyze_frame`; see the coordinator's `_continue_after_observation`.

### Failure modes and guarantees

| Situation | Behaviour |
|---|---|
| LLM timeout or provider error | the client returns a clarification text; planner emits a `ClarificationAction` |
| Epoch moved while thinking | empty list, nothing emitted, no turn committed |
| Unknown tool or invalid arguments | clarification, no registration |
| Slot patch refused | clarification with the specific message, session unchanged |
| Duplicate call (same idempotency key) | slots reverted, no dispatch, only a snapshot |
| Malformed `MEMORY_UPDATE` | marker removed from the visible text, update ignored |
| Model produces text but no tool | spoken response (or clarification if the backend said so) |

One property to be aware of: a provider failure shows up as a `ClarificationAction` with text such as "I hit a snag reaching my reasoning engine", because that is what the circuit breaker returns. This is how the 200k tokens per day cap on the Groq model manifests (see `CLAUDE.md`).

### Tests

- `tests/test_validator.py`: markdown fence repair, trailing comma repair, schema success and failure.
- `tests/test_coordinator_flow.py`, `tests/test_slot_validation.py`, `tests/test_adversarial_timing.py`, `tests/test_adversarial_memory.py`: the planner through the coordinator, including stale plans and slot rejection.
- `tests/test_agent_workers.py`: the `spawn_agent` path.
- The eval scenarios (`python -m agent.eval --llm mock --virtual`) exercise the planner with scripted LLM responses.

### Worked example

The user types "book a hotel in Paris for 2 nights". No work is running.

1. Debounce, then `_handle_user_text`. `_classify` returns `continue` (no strong words), so no epoch bump. The filler is "Looking that up right now..." (no intent yet).
2. `plan(event, session, plan_epoch=0)` builds messages (system prompt plus context block plus the user text) and passes the tool manifests.
3. Suppose the LLM answers with a `book_hotel` tool call, arguments `{"city": "Paris", "nights": 2}`. `validate_call` passes (the schema requires only `city`).
4. `entities_from_tool_call` yields slot entities such as `city`; `stage_call_slots` stages them under the new `call_...` id.
5. `register_tool_call` returns a `ToolCallAction`; the planner appends it and a snapshot. The scratchpad records the entity with its call id.
6. Meanwhile, if the user had said "stop" during step 2, the epoch would have changed, step 4's check on `session.epoch != plan_epoch` would return `[]`, and the booking would never have been registered.

## 6. Deterministic reply summaries

### Purpose

`agent/tool_summaries.py` turns a finished tool call into a sentence the agent speaks, without a second LLM round trip. The docstring explains the motivation: previously only artifact producing agent workers got a visible reply, leaving ordinary tool calls (flights, weather) stuck on the "Looking that up..." filler with no confirmation. A template reply keeps that on the latency sensitive path and removes any risk of the model inventing result details.

### How it works

`summarize_tool_result(tool_name, result)` looks the tool up in `_SUMMARIZERS` and, if the result is a dictionary, calls the summarizer. If the summarizer raises (malformed result shape) or the tool is unknown, it falls back to "Done — <tool name with spaces> completed.".

| Tool | Summarizer | What it says |
|---|---|---|
| `search_flights` | `_flights` | count, route, up to three options (airline, flight, departure, price) and the cheapest; "I couldn't find any flights for that route." when empty. Prices parsed by `_price` (strips `$` and commas, unparsable becomes infinity) |
| `search_hotels` | `_hotels` | count and up to three hotels with stars, price per night and rating; empty gives "I couldn't find any hotels there." |
| `book_flight` | `_book_flight` | flight, route, status (default "booked") and booking ID |
| `book_hotel` | `_book_hotel` | hotel, city, nights, status and reservation ID |
| `check_weather` | `_weather` | condition, city, temperature and humidity |
| `cancel_booking` | `_cancel` | booking ID, status and refund |
| `export_artifact` | `_export` | saved filename, size in KB, folder, and whether it was opened in VS Code or is ready in the Exports panel; or the error text if nothing was exported |
| `set_timer` | `_timer` | "Your <label> timer (<seconds> seconds) just finished." |

`summarize_tool_error(tool_name, error)` produces "Sorry, I couldn't complete <tool> (<error>). Want me to try again?". For `analyze_frame` it uses a dedicated message about the vision service being busy or unavailable.

### Where it is called

In `AgentCoordinator._handle_tool_result` (`agent/coordinator.py`), after a tool result arrives and has passed the epoch check (a result from an older epoch is dropped before this). The order of cases is: an error uses `summarize_tool_error`; a result with an `artifact` key (from an agent worker) gets a fixed "Agent '<name>' has successfully finished building '<title>'" message; every other result uses `summarize_tool_result`. The text is emitted as a `SpokenResponseAction`, followed by a snapshot, and committed to memory with `_commit_turn_and_emit_graph`. `analyze_frame` is an observation tool and is handled earlier: its result is fed back into the planner instead of being summarised.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `summarize_tool_result` | `agent/tool_summaries.py` | dispatch to the per tool summarizer, with generic fallback |
| `summarize_tool_error` | `agent/tool_summaries.py` | failure sentence |
| `_flights`, `_hotels`, `_book_flight`, `_book_hotel`, `_weather`, `_cancel`, `_export`, `_timer` | `agent/tool_summaries.py` | per tool templates |
| `_price` | `agent/tool_summaries.py` | tolerant price parsing for the cheapest option |

### Configuration

None.

### Failure modes and guarantees

- Missing keys render as `None` or `?` in the sentence rather than raising; a structural failure falls back to the generic "Done" sentence.
- The summary only states what the tool result contains, so it cannot claim a completion the tool did not report. The false completion claim check in the eval harness relies on this.
- The `search_flights` summary lists at most three flights and always names the cheapest across all of them.
- Not every tool has a summarizer (for example `spawn_agent` results take the artifact path, `analyze_frame` the observation path, and any other tool the generic "Done" message).

### Tests

`tests/test_tool_replies.py`: flight summary mentions the cheapest; other tool summaries; empty and malformed results fall back gracefully; error summary includes detail; and, through the coordinator, a plain tool call gets a spoken reply, a failed call reports the failure, a cancelled call stays silent, and the reply is recorded in the conversation graph.

### Worked example

`search_flights` returns three flights. `_flights` takes the first for the route, uses `min(..., key=_price)` for the cheapest, joins the first three as "Airline FLIGHT at TIME for PRICE", and returns for example "I found 3 flights from BLR to DEL: ...; ...; .... The cheapest is ... at ...." in one sentence pair, with no LLM call. The coordinator speaks it and the TTS, if enabled, reads it sentence by sentence.

## 7. How the tiers cooperate: three traces

### Trace A: a plain request, no interruption

1. Message arrives, debounce 0.10 s.
2. Tier 1: `continue`. Trace entry written with the score and features.
3. Tier 2: generic filler emitted at once.
4. Tier 3: planner returns a tool call plus snapshot; dispatch starts the tool task.
5. Tool result arrives: stale check passes; `summarize_tool_result` produces the reply; snapshot; turn committed.

### Trace B: a correction while the tool is running

1. Tier 1 returns `act` for the correction. The epoch is bumped; the running call is cancelled (`tool_cancel` action) and the snapshot shows it.
2. Tier 2 emits the interruption acknowledgement.
3. Tier 3 plans the corrected request under the new epoch.
4. If the old tool's result arrives late, its epoch is older, so it is dropped and never summarised or spoken.

### Trace C: an ambiguous utterance (mid band)

1. Tier 1 returns `clarify` (probability between 0.25 and 0.6), and a call is in flight.
2. The coordinator stores `session.pending_clarification` and asks the yes or no question; the epoch is untouched, the planner is not called.
3. The next utterance is interpreted by `_yes_no`. "Yes" replays the original text as a forced interrupt; "no" speaks "Okay, I'll carry on as before." and returns; anything else is treated as a new ordinary turn.
4. If nothing was running, a `clarify` result is treated as an ordinary request (there is nothing to interrupt), per the condition `needs_clarification and has_work and not is_interrupt`.

### Voice partials

For voice, the same classifier runs on partial transcripts (`coordinator.py`, around the barge-in code). A decision other than `continue` counts as strong evidence (two points) for a barge-in while the agent is speaking, so "no wait" cuts the speech immediately. Otherwise the transcript must be substantial for two consecutive partials. That logic is described with the voice chapter, but it explains why Tier 1 needs to be fast enough to run on every partial.

## 8. Known gaps and honest notes

- `README.md` and the architecture sketch label Tier 1 as "keyword mode". The code default is `auto`, which uses MiniLM when `sentence-transformers` is installed (it is in the `embeddings` extra) and keyword mode otherwise. The shipped calibration only applies to the embedding mode.
- The first filler of a new conversation is generic because the intent is taken from the session before the planner has run. Specific templates mostly appear on follow up turns once the session has an intent and slots.
- `INTENT_TEMPLATES` has no entries for `export_artifact`, `set_timer`, `spawn_agent` or `analyze_frame`; those use the generic filler.
- The validator docstring promises 100 percent protocol compliance. The practical guarantee is narrower: nothing that fails schema validation in `ToolRouter.validate_call` is dispatched.
- The OpenRouter backend does not use the validator (see the LLM chapter).
- The calibration set is small and partly synthetic; treat its metrics as regression evidence.
