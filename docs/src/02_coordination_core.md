# Coordination Core: Epochs, Session State, Idempotency, Tool Router and the Virtual Clock

This document covers the package `agent/coordination/` (`state_machine.py`, `canonical.py`, `idempotency.py`, `tool_router.py`, `slot_validation.py`, `fault_injection.py`) and the injectable time source `agent/clock.py`. These modules contain no network or model code. They are the deterministic core that makes the claim "Kairos cancels stale work instead of finishing the wrong thing" checkable. The orchestration that drives them (`agent/coordinator.py`) is described in `03_coordinator_event_flow.md`.

## 1. The problem and the design in one page

A full-duplex agent has a hard concurrency problem. The user says "book a flight to Delhi", the agent starts a tool call that takes several seconds, and half a second later the user says "no, Mumbai". At that moment there are three pieces of work that may be racing:

- a tool call already running in the background (the Delhi booking),
- an LLM planning request that may still be in progress (it may be planning the Delhi request, or the Mumbai one),
- state that the UI and the next prompt will read (slots such as `destination`).

If any result from the Delhi branch lands after the correction, the user sees a stale answer, or worse, a state-changing booking is executed twice or for the wrong city. The coordination core solves this with four cooperating mechanisms:

1. **Epochs.** Each session owns an integer `epoch`. Every tool call is tagged with the epoch it started under. A correction or interrupt calls `SessionState.bump_epoch()`, which cancels every older in-flight call. Anything that finishes later from an older epoch is discarded.
2. **Slot patching with undo.** Slots (the structured facts of the conversation) are patched when a call is dispatched, so the UI shows them while the call is in flight. If the call is cancelled, its slot changes are rolled back, so an abandoned value ("Mumbai" before "actually, Goa") cannot linger in the snapshot.
3. **Idempotency keys.** State-changing calls get a key derived from tool name, intent, canonicalized slots, canonicalized arguments and the epoch. A second call with the same key is refused.
4. **Validation.** Slot patches and tool arguments are validated before they touch state, and invalid ones become clarification questions rather than actions.

All of this is pure synchronous Python operating on in-memory objects, which is why it is cheap to unit test and why the eval harness can run it under a virtual clock.

## 2. Epoch model and session state (`agent/coordination/state_machine.py`)

### Purpose

`SessionState` is the single source of truth for one conversation: the epoch counter, the current intent, the slots, the registry of in-flight tool calls, the idempotency store, a ring buffer of historical states for rollback, and (lazily) the two memory tiers. `InFlightCall` is a pydantic model describing one registered tool call.

### How it works

**Construction.** `SessionState(session_id, max_history=10)` starts with `epoch = 1`, `intent = None`, empty `slots`, an empty `in_flight_calls` dict, a fresh `IdempotencyStore`, `last_activity = clock.now()` and `pending_clarification = None`. It immediately saves an initial snapshot into `_history` via `_save_to_history()`. The history is a list capped at `max_history` entries (oldest popped first). Memory objects (`scratchpad`, `graph_memory`) start as `None`; `ensure_memory()` attaches a `GraphMemory` and a `TurnScratchpad` with function-local imports, to avoid an import cycle between `state_machine.py` and `agent/memory/*`. The coordinator calls `ensure_memory()` for every session it creates; a bare `SessionState()` constructed in a test never has memory and behaves exactly as before.

**Epoch bump.** `bump_epoch(reason)` increments `epoch` and walks `in_flight_calls`. For every call whose `epoch < self.epoch` and whose status is `pending` or `running` it does four things, in this order:

1. If the call has an `asyncio_task` that is not done, it calls `task.cancel()`. The real work stops at its next `await`.
2. It sets `status = "cancelled"`.
3. It calls `revert_call_slots(call_id)`, undoing the call's slot changes.
4. It builds a `ToolCancelAction(session_id, epoch=<new epoch>, call_id, tool_name, reason)` and, if memory is attached, calls `scratchpad.mark_aborted(...)` so entity candidates that came from the abandoned call are dropped from the scratchpad.

The method returns the list of `ToolCancelAction` objects. It does not emit them: the caller (the coordinator) pushes them to the action queue. This keeps `SessionState` free of I/O. Note that the cancel action carries the new epoch while the cancelled call keeps its original epoch.

**Registering a call.** `register_tool_call(call_id, tool_name, arguments, is_state_modifying, task)`:

1. If the call is state-modifying, compute `idempotency_key = IdempotencyStore.generate_key(intent, slots, epoch, tool_name, arguments)`. If the store already `exists(key)` the method returns `None` (duplicate, nothing dispatched). Otherwise the key is registered with status `in_flight`.
2. Create an `InFlightCall` with status `pending`, tagged with the current epoch.
3. Return a `ToolCallAction` carrying the epoch, call id, tool name, arguments, the `is_state_modifying` flag and the key.

Read-only calls skip the idempotency check entirely, so an identical search can be repeated.

**Attaching and completing.** `attach_task(call_id, task)` stores the background `asyncio.Task` and sets the call to `running`. `complete_tool_call(call_id, result, error)` returns `False` when the call is unknown, when its status is `cancelled`, or when `call.epoch < self.epoch`; in all of those cases the result must be dropped by the caller. Otherwise it marks the call `completed`, discards the undo record (the slot values become permanent) and completes the idempotency entry with the result. This function is the guard that makes "results from an older epoch are dropped" true: there are two independent defences, the status flag set by `bump_epoch` and the epoch comparison.

**Slots, patches and rollback.** `patch_slots(slot_diff, new_intent=None)`:

1. `validate_patch(slot_diff)` runs first (see section 5); it raises before anything is mutated.
2. The intent (if given) and slots are updated, and a snapshot is appended to the history ring.
3. `validate_state(self.slots)` checks cross-field rules on the result. If it fails, `rollback_snapshot()` pops the just-saved snapshot, restores the previous slots and intent, and the `SlotPatchError` is re-raised.

The caller therefore gets either the whole patch, or an unchanged session and an exception whose `user_message` is safe to show the user.

`stage_call_slots(call_id, values)` is the dispatch-time variant used by the planner. It records each slot's previous value (the sentinel `MISSING` from `agent/memory/tool_slots.py` when the slot did not exist), calls `patch_slots(values)`, and only after a successful patch stores `_call_slot_undo[call_id] = {slot: (previous, value_set)}`. If the patch is rejected, no undo record exists. `revert_call_slots(call_id)` pops the undo record and, for each slot, restores the previous value (or deletes the slot) only if the slot still holds exactly the value that call set. That guard matters: if call B has since overwritten the slot, reverting call A must not clobber B's value.

**Snapshot.** `get_snapshot()` builds a `StateSnapshotAction` with the epoch, intent, a copy of the slots, the list of calls still `pending` or `running` (as `InFlightCallInfo`) and an ISO-8601 UTC timestamp (note: this one uses `datetime.now`, not `clock.now()`). Snapshots are what the web and Android clients render as the "state" panel.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `InFlightCall` | `agent/coordination/state_machine.py` | Pydantic record of one dispatched call: id, tool, epoch, arguments, status, task handle, state-modifying flag, idempotency key |
| `SessionState` | `agent/coordination/state_machine.py` | Per-session epoch, slots, intent, in-flight registry, history ring, memory handles |
| `SessionState.bump_epoch` | `agent/coordination/state_machine.py` | Increment epoch, cancel stale calls, revert their slots, return `ToolCancelAction`s |
| `SessionState.register_tool_call` | `agent/coordination/state_machine.py` | Epoch-tag a call and apply the idempotency check; returns `None` for duplicates |
| `SessionState.complete_tool_call` | `agent/coordination/state_machine.py` | Accept or reject a result depending on status and epoch |
| `SessionState.patch_slots` | `agent/coordination/state_machine.py` | Validated diff patch with rollback on bad resulting state |
| `SessionState.stage_call_slots` / `revert_call_slots` | `agent/coordination/state_machine.py` | Dispatch-time slot patch with per-call undo record |
| `SessionState.get_snapshot` | `agent/coordination/state_machine.py` | Build the `StateSnapshotAction` sent to clients |
| `SessionState.touch` | `agent/coordination/state_machine.py` | Refresh `last_activity` for idle eviction |

### Configuration

There are no environment variables here. The only knob is the constructor argument `max_history` (default 10), the depth of the rollback ring. Idle eviction (`SESSION_TTL_S`, `SESSION_EVICT_INTERVAL_S`) lives in the coordinator and uses `last_activity`.

### Interactions

- The coordinator creates sessions, calls `bump_epoch`, `attach_task`, `complete_tool_call` and `get_snapshot` (see `03_coordinator_event_flow.md`).
- The planner (`agent/slow_path/planner.py`) calls `stage_call_slots`, `register_tool_call` and `revert_call_slots`, and compares `session.epoch` with the epoch the plan started under.
- The scratchpad and graph memory read and write session slots (`scratchpad.commit()` applies the final slot diff through `patch_slots`).
- The action schemas (`agent/schemas/actions.py`) are imported here, so the state machine builds the wire-format actions directly.

### Failure modes and guarantees

- A stale result can only be accepted if both defences fail: the status would have to be something other than `cancelled` and the epoch equal to the session's. `bump_epoch` sets both.
- `bump_epoch` only cancels calls that are `pending` or `running`; a call that has already completed keeps its result and its slot changes. This is intentional (a finished booking cannot be un-booked by cancelling a task); undoing it requires the `cancel_booking` tool.
- Task cancellation is cooperative. `task.cancel()` raises `CancelledError` at the next `await`. The tool handlers shipped with the router are plain `asyncio.sleep` coroutines, so they cancel promptly; a blocking synchronous handler would not.
- The rollback ring is bounded to `max_history` entries. `rollback_snapshot()` returns `False` when there is nothing to roll back to (only the initial snapshot).
- `revert_call_slots` restores a slot to its previous value only if it still holds the cancelled call's value, so concurrent legitimate updates survive.

### Tests that cover it

`tests/test_state_machine.py` (initialization, epoch bump cancelling stale calls, stale completion discarded, idempotency blocking duplicates, slot diff patching and rollback), `tests/test_coordinator_flow.py` (an interrupt cancels a running task and the cancel action has epoch 2), `tests/test_session_eviction.py` (activity clock and eviction), plus the eval scenarios `correction_mid_booking` and `rapid_fire_corrections` in `agent/eval/scenarios.py`.

### Worked example

Session starts at epoch 1. The planner stages `{origin: "BLR", destination: "DEL"}` for `call_a` (previous values `MISSING`, `MISSING`), registers `book_flight`, and the coordinator attaches the task. State: `slots = {origin: BLR, destination: DEL}`, `call_a` is `running` at epoch 1. An interrupt arrives: `bump_epoch("user_correction: ...")` sets epoch 2, cancels the task, marks `call_a` cancelled, `revert_call_slots("call_a")` deletes both slots (they still hold the values `call_a` set), and returns one `ToolCancelAction(epoch=2, call_id="call_a", ...)`. If the Delhi booking handler somehow still posts a `ToolResultEvent`, `complete_tool_call("call_a")` sees status `cancelled` and returns `False`.

## 3. Canonical forms (`agent/coordination/canonical.py`)

### Purpose

An LLM phrases the same request differently from call to call ("Mumbai" one time, "BOM" the next, " delhi " with stray whitespace). If idempotency were keyed on raw strings, those would look like different requests and a double booking could result. `canonical.py` maps equivalent values to one canonical form before hashing or comparing.

### How it works

`canonical_value(key, value)` applies only to strings; other types pass through unchanged. For a string it strips, lower-cases and collapses internal whitespace. If `key` is in `LOCATION_KEYS` (`origin`, `destination`, `city`, `from`, `to`, `location`) it then looks the result up in `_CITY_TO_IATA`; a hit returns the IATA code (for example `"new delhi"` and `"delhi"` both give `DEL`, `"bombay"` and `"mumbai"` both give `BOM`). If there is no table hit but the value is exactly three letters, it is upper-cased and treated as an airport code. Anything else falls back to the normalized lower-case string. `canonical_mapping(values)` applies this to every item of a dict.

The table is deliberately modest: about 20 Indian and about 22 international city names and aliases. The module docstring says so, and says to extend it or swap in a geocoder. Unknown places still compare case and whitespace-insensitively, which is strictly better than raw comparison. One honest limitation: a three-letter word that is not an airport code (for a location-keyed argument) is upper-cased and treated as a code.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `LOCATION_KEYS` | `agent/coordination/canonical.py` | Argument names treated as places |
| `_CITY_TO_IATA` | `agent/coordination/canonical.py` | City/alias to IATA code table |
| `canonical_value` | `agent/coordination/canonical.py` | Canonicalize one value |
| `canonical_mapping` | `agent/coordination/canonical.py` | Canonicalize a dict of values |

### Configuration

None. Extending the city table means editing `_CITY_TO_IATA`.

### Interactions

`idempotency.py` canonicalizes slots and arguments before hashing; `slot_validation.validate_state` canonicalizes `origin` and `destination` before comparing them, so "Mumbai to BOM" is rejected as a same-place trip.

### Failure modes and guarantees

A city missing from the table is compared by lower-cased text only: "Pune" and "PNQ" collapse (both are in the table) but a rarely used city and its code would not. The consequence is a missed duplicate, never a false duplicate of two different cities, since the mapping is many-to-one only for known aliases of the same airport.

### Tests that cover it

`tests/test_canonical.py`: city names and airport codes canonicalize together; unknown places and non-location keys only normalize case and space; the idempotency key ignores phrasing but not substance; a rephrased state-changing call is blocked by the session.

## 4. Idempotency (`agent/coordination/idempotency.py`)

### Purpose

Guarantee zero duplicate state-changing calls under retries, interruptions and races. Evaluation counts duplicate state changes as a hard failure (CI requires zero), so this is enforced in code rather than left to the prompt.

### How it works

`IdempotencyStore` is a per-session dict `key -> {"call_id", "result", "status"}`. `generate_key(intent, slots, epoch, tool_name="", arguments=None)` is a static method that:

1. canonicalizes `slots` with `canonical_mapping` and JSON-encodes them with sorted keys,
2. builds the string `"{tool_name}:{intent or 'none'}:{slots_json}:{epoch}"`,
3. if `arguments` is non-empty, appends `":{canonical arguments json}"`,
4. returns the SHA-256 hex digest.

The module docstring quotes the spec formula `hash(intent + sorted(slot_values) + epoch)`. The implementation extends it with `tool_name` and `arguments`; the in-code comment explains why: without the arguments, booking DEL to BOM and booking DEL to GOA in the same state produced the same key and the second booking was silently dropped as a "duplicate". Passing no `arguments` preserves the original key shape for old callers.

`exists(key)`, `register(key, call_id, result=None, status="in_flight")`, `complete(key, result)`, `get(key)` and `clear()` are thin dict operations. `SessionState.register_tool_call` is the only place keys are generated in production code.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `IdempotencyStore` | `agent/coordination/idempotency.py` | Per-session key registry |
| `IdempotencyStore.generate_key` | `agent/coordination/idempotency.py` | Deterministic SHA-256 over tool, intent, canonical slots, epoch, canonical arguments |
| `IdempotencyStore.exists` / `register` / `complete` | `agent/coordination/idempotency.py` | Duplicate check, in-flight registration, completion record |

### Configuration

None.

### Interactions

`SessionState.register_tool_call` calls it for state-modifying tools only (as decided by `ToolRouter.is_state_modifying`). The planner stages slots before registering, because the key hashes the slot values: two identical requests then produce the same key, two different ones do not. If registration returns `None`, the planner calls `revert_call_slots` so the duplicate leaves no state behind.

### Failure modes and guarantees

- The epoch is part of the key. That is deliberate: after a correction bumps the epoch, re-issuing the same booking is legitimate (the old one was cancelled). The flip side is that the key only protects against duplicates inside one epoch. Cross-epoch duplicates are prevented by cancellation, not by the key.
- A key stays registered even if its call is later cancelled. Inside the same epoch that cannot matter, because a cancellation always comes with an epoch bump.
- The store is in memory and per session; it is dropped with the session on eviction.

### Tests that cover it

`tests/test_state_machine.py::test_idempotency_prevents_duplicate_state_modifying_calls`, `tests/test_canonical.py` (rephrasing does not defeat the key, different substance does), and the eval scenario `duplicate_booking_race`.

### Worked example

"Book Delhi to Mumbai" then, 100 ms later and in the same epoch, the user repeats it as "book DEL to BOM". The first call stages slots `{origin: Delhi, destination: Mumbai}`; the second stages `{origin: DEL, destination: BOM}`, which canonicalize to the same values; `generate_key` returns the same digest, `exists` is true, `register_tool_call` returns `None`, the planner reverts the staged slots and nothing is dispatched.

## 5. Slot validation (`agent/coordination/slot_validation.py`)

### Purpose

Spec section 7.4 asks for snapshot versioning with rollback on a failed patch. A bad LLM argument (a 10 KB string, an impossible date, 400 nights) must never become session state, and the user must get a question they can answer instead of a stack trace.

### How it works

Two functions and one exception.

`validate_patch(diff)` runs before any mutation and raises `SlotPatchError` on the first problem. For each item it checks:

- the key is a non-empty string,
- `None` values are skipped (allowed),
- the value is a `str`, `int` or `float`; booleans and other types (lists, dicts) are rejected,
- strings are at most `MAX_TEXT_LEN = 200` characters and contain no control characters (regex `[\x00-\x08\x0b-\x1f\x7f]`),
- the slot `nights` must be an integer (floats and strings are rejected) in `1..MAX_NIGHTS` with `MAX_NIGHTS = 60`,
- the slot `date`, when it matches `YYYY-MM-DD`, must be a real calendar date and the year must be in 2000..2100. Free text such as "next Friday" is allowed.

`validate_state(slots)` checks one cross-field rule on the state a patch would produce: `origin` and `destination` must not canonicalize to the same place.

`SlotPatchError(field, message, user_message)` is a `ValueError` subclass; `user_message` is a sentence safe to speak ("How many nights did you mean? (1 to 60)").

### Key types and functions

| Name | File | Role |
|---|---|---|
| `validate_patch` | `agent/coordination/slot_validation.py` | Pre-apply checks on types, sizes, ranges, dates |
| `validate_state` | `agent/coordination/slot_validation.py` | Post-apply cross-field check (origin differs from destination) |
| `SlotPatchError` | `agent/coordination/slot_validation.py` | Carries the field, an internal message and a user-safe message |
| `MAX_TEXT_LEN`, `MAX_NIGHTS` | `agent/coordination/slot_validation.py` | Limits (200 characters, 60 nights) |

### Configuration

The limits are module constants, not environment variables.

### Interactions

`SessionState.patch_slots` calls both validators. The planner (`agent/slow_path/planner.py`) catches `SlotPatchError` around `stage_call_slots` and returns a `ClarificationAction` carrying `user_message` plus a snapshot, with nothing registered or dispatched. The planner also validates the tool call itself against the router's JSON schema before staging slots (section 6).

### Failure modes and guarantees

Either the whole patch applies or the session is unchanged. A call whose staging fails leaves no undo record (so `revert_call_slots` is a no-op). A limitation: validation covers only the slot names named above (`nights`, `date`, origin/destination); other slots get only type, length and control-character checks.

### Tests that cover it

`tests/test_slot_validation.py`: bad patches rejected, good ones pass, a rejected patch leaves state untouched, a bad resulting state rolls back through the snapshot ring, a bad staged call leaves no undo record, unknown tool is reported, invalid tool calls become clarifications and not dispatches, a valid call still dispatches.

## 6. Tool router (`agent/coordination/tool_router.py`)

### Purpose

The router is the registry of tools the agent can call. It gives the LLM a function-calling manifest, validates arguments against a JSON Schema, classifies each tool as read-only or state-modifying (which drives idempotency), and executes the handler.

### How it works

`ToolDefinition` holds `name`, `description`, `parameters_schema`, `is_state_modifying` and an async `handler`. `validate_args` calls `jsonschema.validate`. `ToolRouter(register_defaults=True)` registers the default tools in `register_default_tools()`. The router exposes:

- `register_tool(...)` to add or replace a tool (tests and the eval environment replace handlers this way),
- `get_tool_manifests()`, which returns a list of OpenAI-style `{"type": "function", "function": {...}, "is_state_modifying": ...}` entries for the LLM,
- `validate_call(tool_name, arguments)`, which raises `UnknownToolError` (a `KeyError`) for an unregistered tool, or `jsonschema.ValidationError` for bad arguments,
- `is_state_modifying(tool_name)`, which returns the registered flag; for an unregistered name it falls back to a name heuristic: names starting with `book`, `cancel`, `delete`, `create`, `update`, `buy`, `pay` or `send` are assumed state-modifying (the conservative direction),
- `execute_tool(tool_name, arguments)`, which awaits `handler(**arguments)` and raises `ValueError` if the tool or its handler is missing.

The default tool set:

| Tool | State-modifying | Notes |
|---|---|---|
| `search_flights` | no | `origin`, `destination` required; simulated 3.0 s latency, returns 3 canned flights |
| `book_flight` | yes | `origin`, `destination` required; 2.5 s; returns booking id `FL-98214` |
| `search_hotels` | no | `city` required; 3.0 s |
| `book_hotel` | yes | `city` required; 2.5 s |
| `check_weather` | no | `city` required; 2.0 s |
| `cancel_booking` | yes | `booking_id` required; 2.0 s |
| `export_artifact` | yes | Handler is a stub; the coordinator intercepts the call and runs `exporter.export_file` |
| `set_timer` | no | `seconds` integer 1 to 300; sleeps for that long, then reports finished |
| `analyze_frame` | no | Handler is a stub; the coordinator intercepts and calls the vision backend |
| `spawn_agent` | no | `name`, `role`, `goal` required; the coordinator intercepts and runs an autonomous worker |

The flights, hotels, weather and booking tools are simulations: they sleep and return canned data, so the demo and the evaluation are deterministic. The sleeps are what give the user time to interrupt. The `book_flight` description tells the model it needs only origin and destination and should not ask for a date, a prompt-level guard against needless clarification. Three tools (`export_artifact`, `analyze_frame`, `spawn_agent`) have placeholder handlers because the coordinator special-cases them by name in `_execute_tool_task`: they need per-session context (artifacts, frame buffer, worker streaming) that a stateless router does not have.

The read versus state-modifying split matters in three places: idempotency (only state-modifying calls get a key), the interrupt policy (cancelling a read-only search is harmless, a stale state-modifying call must never complete), and the memory layer (`SKIP_TOOLS` in `agent/memory/tool_slots.py` stops three tools from turning their arguments into slots).

### Key types and functions

| Name | File | Role |
|---|---|---|
| `ToolDefinition` | `agent/coordination/tool_router.py` | One tool: schema, flag, handler |
| `ToolRouter` | `agent/coordination/tool_router.py` | Registry, manifests, validation, execution |
| `ToolRouter.get_tool_manifests` | `agent/coordination/tool_router.py` | Function-calling schema list given to the LLM |
| `ToolRouter.validate_call` | `agent/coordination/tool_router.py` | JSON Schema check; raises `UnknownToolError` for unknown tools |
| `ToolRouter.is_state_modifying` | `agent/coordination/tool_router.py` | Registered flag, or verb heuristic for unknown names |
| `ToolRouter.execute_tool` | `agent/coordination/tool_router.py` | Awaits the handler |
| `UnknownToolError` | `agent/coordination/tool_router.py` | The model asked for an unregistered tool |

### Configuration

No environment variables. Tools are added in code with `register_tool`.

### Interactions

The planner passes `get_tool_manifests()` to the LLM (omitting `analyze_frame` for continuation turns), validates the chosen call with `validate_call`, and asks `is_state_modifying`. The coordinator calls `execute_tool` for ordinary tools and `is_state_modifying` in `dispatch_tool_call`. The eval environment (`agent/eval/environment.py`) wraps the same router with scripted latencies and fault injection.

### Failure modes and guarantees

- A hallucinated tool name or malformed arguments fail `validate_call` before anything is dispatched; the planner turns that into a clarification question.
- A handler exception is caught by the coordinator, becomes a `ToolResultEvent` with `error` set, and the user hears a failure notice (never "done").
- Validation is only as strict as each schema: for example `book_flight` accepts any strings for `origin` and `destination`; semantic checks (same origin and destination) are in `slot_validation`.

### Tests that cover it

`tests/test_slot_validation.py` (unknown tool and invalid arguments), `tests/test_coordinator_flow.py` and `tests/test_adversarial_timing.py` (custom tools registered through `register_tool`), `tests/test_tool_replies.py` (tool results turned into replies), `tests/test_agent_workers.py` (the `spawn_agent` path), `tests/test_export.py` (the `export_artifact` path).

## 7. Fault injection (`agent/coordination/fault_injection.py`)

### Purpose

Spec section 7.3: the evaluation environment injects deterministic latency and faults into tools. This module is the local test double that mirrors it, so the same failure behaviour can be exercised in unit tests and in eval scenarios.

### How it works

`FaultInjectionConfig(delay_seconds=0.0, failure_rate=0.0, timeout_seconds=None, malformed_response_rate=0.0)` is a plain value holder. `FaultInjectedToolHandler(real_handler, config)` wraps an async handler; calling it increments `call_count` and then, in order:

1. sleeps `delay_seconds` if positive,
2. if `timeout_seconds` is set: raises `asyncio.TimeoutError("Simulated tool timeout")` when it is <= 0, otherwise sleeps that long (see the caveat below),
3. with probability `failure_rate` raises `RuntimeError("Simulated tool backend 500 error")`,
4. awaits the real handler,
5. with probability `malformed_response_rate` returns the string `{unparseable_malformed_json: missing_brace` instead of the real result.

Because every sleep is `asyncio.sleep`, it runs correctly under the virtual clock.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `FaultInjectionConfig` | `agent/coordination/fault_injection.py` | Delay, failure rate, timeout, malformed rate |
| `FaultInjectedToolHandler` | `agent/coordination/fault_injection.py` | Async wrapper that applies the faults around a real handler |

### Configuration

Constructor arguments only. Used from the eval scenarios (`agent/eval/scenarios.py` imports `FaultInjectionConfig`; one scenario sets `failure_rate=1.0` on `search_flights`) via `agent/eval/environment.py`.

### Interactions

The coordinator's `_execute_tool_task` handles the three outcomes. An exception becomes an error `ToolResultEvent`. A malformed string result is caught by the check "every tool returns a JSON object": a non-dict result is converted into the error "the service returned an unreadable response", so a garbled payload can never be reported as success.

### Failure modes and guarantees

- Honest caveat: `timeout_seconds > 0` does not raise a timeout; it just sleeps that many extra seconds. Only a value `<= 0` raises `TimeoutError` immediately. Hard timeouts for real tools are not implemented in this module.
- `failure_rate` and `malformed_response_rate` use the global `random` module, so non-extreme rates are not reproducible unless the caller seeds it. The eval scenarios use 1.0 (always) for determinism.

### Tests that cover it

`tests/test_adversarial_timing.py::test_fault_injected_tool_latency_and_cancellation` (injected 400 ms latency, interrupt mid-flight, the handler body never runs); eval scenarios that set `faults` (for instance a failing `search_flights`) run through `tests/test_eval_suite.py` and the CI gate.

## 8. The virtual clock (`agent/clock.py`)

### Purpose

The evaluator may drive the agent with a virtual-clock harness, and the project's own eval harness runs its whole mock-mode suite this way (CLAUDE.md quotes under 1 s). Anything in the agent that reads or waits on time has to be injectable.

### How it works

The module exposes three functions:

- `now()` returns wall-clock seconds, or inside `run_virtual()` the loop's virtual time added to a wall-clock anchor (`wall_start`), so timestamps look real but are consistent with virtual sleeps,
- `monotonic()` returns `time.monotonic()` normally, or the loop's virtual time,
- `is_virtual()` is true inside `run_virtual()`; callers use it to avoid worker threads, because a thread blocked on real work would see time race ahead.

`run_virtual(coro)` creates a `VirtualTimeLoop` (an `asyncio.SelectorEventLoop` subclass whose `time()` returns `_vtime`), publishes it in the module global `_virtual_loop`, runs the coroutine to completion, then restores the real clock. Time advances through `_JumpingSelector`, which wraps the loop's real selector: when the loop calls `select(timeout)`, the wrapper polls with timeout 0, and if no I/O is ready and `timeout > 0` it adds `max(timeout, 1e-6)` to `_vtime` instead of blocking. In other words, whenever every task is idle the loop jumps straight to the next scheduled timer. If `timeout is None` (nothing scheduled) it blocks on the real selector, because only an external thread wakeup could help. The `1e-6` floor prevents a livelock where a timeout smaller than float resolution at the current time would leave virtual time unchanged.

The convention enforced across the codebase: agent code uses `clock.now()` and `clock.monotonic()` instead of `time.*`, and all delays are `asyncio.sleep` or `asyncio.wait_for`. Defaults like `created_at: float = Field(default_factory=clock.now)` in `InFlightCall` and the action/event base classes follow the rule.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `now()` / `monotonic()` | `agent/clock.py` | Time sources used everywhere in agent code |
| `is_virtual()` | `agent/clock.py` | Detect virtual mode (keep work on the loop; skip real audio and models) |
| `run_virtual(coro)` | `agent/clock.py` | Run a coroutine on a virtual-time loop |
| `VirtualTimeLoop` | `agent/clock.py` | Event loop with a settable virtual `time()` |
| `_JumpingSelector` | `agent/clock.py` | Selector wrapper that advances time instead of sleeping |

### Configuration

None. The eval CLI enables it with `python -m agent.eval --llm mock --virtual`; `--virtual` is rejected with a live LLM because real network latency cannot be simulated (`agent/eval/__main__.py`).

### Interactions

The coordinator uses `clock.is_virtual()` to run the export write inline rather than in `asyncio.to_thread`. The eval harness (`agent/eval/runner.py`) runs scenarios in virtual time; voice scenarios need real audio and models and are skipped (they are not part of the gate). Not everything is on the virtual clock: `agent/warmup.py` uses `time.time()` for its total duration and `get_snapshot()` uses `datetime.now`, which is harmless because neither participates in ordering or latency scoring.

### Failure modes and guarantees

- Only valid for work that lives entirely on the event loop (mock LLM, simulated tools). A worker thread (real Whisper, blocking HTTP) sees real time while the loop leaps ahead.
- The global `_virtual_loop` means one virtual run at a time per process; it is cleared in a `finally` block.
- A sleeping coroutine that never gets a wakeup and has no timers would block on the real selector (the `timeout is None` branch); this is the intended escape hatch for thread callbacks but would hang on a bug.

### Tests that cover it

`tests/test_virtual_clock.py`: the loop jumps over a one-hour sleep in under a second while `now()` and `monotonic()` stay consistent (and `wait_for` timeouts fire), and eval scores for selected scenarios (`correction_mid_search`, `rapid_fire_corrections`, `duplicate_booking_race`, `vision_interrupt`) match between the virtual and real clock. `tests/test_eval_suite.py` and the CI command `python -m agent.eval --llm mock --virtual --set all --fail-under 97 --min-scenario 95` use it end to end.

### Worked example

`await asyncio.sleep(3600)` inside `run_virtual`: the loop schedules a timer 3600 s ahead, finds no ready I/O, calls `select(3600)`, the wrapper adds 3600 to `_vtime` and returns no events, and the timer fires immediately. `clock.now()` advances by 3600 while the wall time elapsed is microseconds. A `book_flight` tool with 2.5 s latency and a user interrupt scripted at 0.8 s therefore play out in the same order and with the same measured latencies as in real time.

## 9. How the pieces fit together

```
 planner / coordinator
        |
        |  1. validate_call(tool, args)            tool_router.py  (JSON Schema)
        |  2. is_state_modifying(tool)             tool_router.py
        |  3. stage_call_slots(call_id, slots) --> slot_validation.py (patch + state)
        |  4. register_tool_call(...)          --> idempotency.py + canonical.py
        |         returns ToolCallAction or None (duplicate)
        v
 asyncio task runs handler (optionally wrapped by fault_injection.py)
        |
        |  user correction  -->  bump_epoch(): cancel task, revert slots, ToolCancelAction
        |
        v
 ToolResultEvent --> complete_tool_call(): False if cancelled or epoch older (drop)
        |
 time everywhere: clock.now() / clock.monotonic()  (virtual in the eval harness)
```

The invariants this layer is built to preserve, and which the trace logger and eval scorer check from the outside, are: epochs only increase; a cancelled call never completes; a state-changing call with the same canonical request in one epoch is dispatched at most once; a rejected slot patch leaves state unchanged; and an abandoned value never survives in the snapshot.
