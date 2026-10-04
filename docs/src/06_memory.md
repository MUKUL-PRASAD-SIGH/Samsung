# Memory: Scratchpad, Graph Memory, Context Builder and Tool Slots

## 1. Overview

Kairos keeps conversational state in two tiers, both attached lazily to a per-session `SessionState` (`agent/coordination/state_machine.py`) by `SessionState.ensure_memory()`. The coordinator calls `ensure_memory()` in `AgentCoordinator.get_or_create_session()`, so every session served by the real server has both tiers. A bare `SessionState()` built directly in a test has neither (`scratchpad` and `graph_memory` stay `None`), and every consumer guards for that, so older behaviour is unchanged.

The two tiers and the glue around them:

| Tier / piece | File | One-line role |
|---|---|---|
| L1 `TurnScratchpad` | `agent/memory/scratchpad.py` | Volatile, per-turn notebook: raw utterances, candidate slot diffs, entity candidates, aborted calls. Distilled into one `CanonicalTurn` on commit. |
| L2 `GraphMemory` | `agent/memory/graph_memory.py` | Persistent per-session DAG of turn, entity and artifact nodes joined by typed edges. Source of the chat history sent to the LLM and of the graph the UI draws. |
| `context_builder` | `agent/memory/context_builder.py` | Turns `SessionState` plus `GraphMemory` into prompt text and chat messages for the planner. |
| `tool_slots` | `agent/memory/tool_slots.py` | Deterministic mapping from tool-call arguments to slots/entities, with the `SKIP_TOOLS` exclusion list. |

An invariant runs through all of it: slot state is owned by `SessionState` and nothing in `agent/memory/` writes `session.slots` directly. Every mutation goes through `SessionState.patch_slots()`, which validates the patch (`agent/coordination/slot_validation.py`), appends to the history ring buffer and rolls back if the resulting state is invalid. This keeps one source of truth for slots, and it is why the memory module can sit beside the epoch model without a second copy of the state.

## 2. Why this design

A real-time agent that can be interrupted has a nasty memory problem. A request like "book Delhi to Mumbai" may be cancelled half way by "actually, Goa". The cancelled call produced no completed turn, so it must not appear in the conversation history, and the abandoned value "Mumbai" must not linger in slots. Yet the model, seeing only "actually, make it Goa", needs to know what is being corrected. The memory layer solves three related problems:

- Keep abandoned work out of durable state (slots and history).
- Still surface that abandoned work to the LLM as a one-off note, so it can apply the correction instead of asking the user to repeat themselves.
- Record what the user actually heard when a spoken reply was cut off, so the model does not assume information was delivered that never was.

The module docstring in `agent/memory/context_builder.py` also records the baseline: before this module existed, the planner sent the LLM only the intent, the slots and the current text, with no multi-turn history at all. `tests/test_memory_baseline.py` pins that baseline (`test_baseline_has_no_conversation_history`) and measures the token delta of enabling memory.

## 3. Tier 1: TurnScratchpad

### 3.1 Purpose

`TurnScratchpad` is the "L1 working cache". It absorbs the noisy, high-velocity inputs of the current turn (barge-in fragments, repeated corrections, entity guesses, cancelled calls) and, on commit, collapses them into one clean `CanonicalTurn`. It is scoped to the current utterance only and is emptied after every commit.

### 3.2 Data types

| Name | File | Role |
|---|---|---|
| `SlotDelta` | `agent/memory/scratchpad.py` | One candidate slot change: `slot_name`, `original_value`, `revised_value`, `epoch`, `timestamp` (from `clock.now`). |
| `InFlightCallRecord` | `agent/memory/scratchpad.py` | Record of a call the turn started: `call_id`, `tool_name`, `arguments`, `status`, `epoch`. Used for aborted calls. |
| `EntityCandidate` | `agent/memory/scratchpad.py` | A value worth remembering: `entity_type`, `key`, `value`, `epoch`, `source_call_id`, and `previous_value` (the slot value it replaced). |
| `CanonicalTurn` | `agent/memory/scratchpad.py` | The distilled result of a turn: ids, epoch, `cleaned_user_intent`, `final_slots`, `agent_response`, `artifacts_produced`, `entity_candidates`, `overrides`, `intent_shift`, `aborted_calls_count`. |
| `TurnScratchpad` | `agent/memory/scratchpad.py` | The scratchpad itself. |

`EntityCandidate.previous_value` uses two sentinels defined in `agent/memory/tool_slots.py`. `MISSING` means "this slot had no value before". `UNTRACKED` means "the previous value was not recorded", which is the default and applies to entities that are only patched at commit time (for example those from a model-supplied `MEMORY_UPDATE`).

### 3.3 How it works

The coordinator and planner feed the scratchpad through these calls:

1. `append_utterance_chunk(text)`. `AgentCoordinator._handle_user_text` calls it for every user message (typed or transcribed) before classification. `raw_chunks` therefore holds every utterance of the exchange that has not yet been committed. A message that is interrupted before completion is never committed, so its text stays in the list and is later available as an "earlier request that did not finish".
2. `record_entity_candidate(...)`. The planner (`agent/slow_path/planner.py`) calls it in two places. After a tool call is registered, it records one candidate per scalar tool argument, with `call_id` and `previous_value` filled in (see section 6). Separately, if the LLM reply contains a `MEMORY_UPDATE:` line (parsed in `agent/llm_client.py`), each entity in it is recorded without a call id and with `previous_value=UNTRACKED`. A malformed entity is logged and skipped.
3. `record_slot_diff(slot_name, new_val)` and `record_intent_shift(text)` exist for candidate slot changes and an intent change. `record_slot_diff` reads the current slot value as `original_value` and does not touch `session.slots` until commit. In the production flow read in this task, the planner only calls `record_entity_candidate` and `record_intent_shift`; `record_slot_diff` is exercised by `tests/test_scratchpad.py`.
4. `mark_aborted(call_id, tool_name, reason, arguments)`. Called from `SessionState.bump_epoch()` for each in-flight call it cancels. It does two things: it deletes every entity candidate whose `source_call_id` equals the cancelled call (so "Mumbai" from an abandoned request can never be committed), and it appends an `InFlightCallRecord` with status `cancelled` and the call's arguments, which is what the "interrupted work" note is built from.
5. `commit(agent_response, artifacts)`. Called by `AgentCoordinator._commit_turn_and_emit_graph`.

### 3.4 What `commit()` does

`commit()` is the only place the class mutates session state:

1. It builds `final_diff` with last-write-wins semantics: deltas first, then entity candidates (a candidate overwrites a delta on the same key).
2. It computes `overrides`, the dictionary of slot key to the previous value for every slot this turn changed from a different earlier value. For an entity from a tool call, whose slot was already patched at dispatch, it uses the recorded `previous_value` (ignoring `MISSING` and equal values). For anything else it compares against the current `session.slots`.
3. If there is a diff or an intent shift, it calls `session.patch_slots(final_diff, new_intent=...)`. This reuses the same method that `tests/test_state_machine.py` covers and that `tests/test_scratchpad.py::test_scratchpad_commit_equals_direct_patch_slots` checks for equivalence.
4. It builds the `CanonicalTurn`. `cleaned_user_intent` is the last raw chunk only (`self.raw_chunks[-1:]`), `final_slots` is a copy of the session slots after the patch, and `turn_id` has the form `<session_id>_turn_<n>_<millis>`.
5. It purges: `raw_chunks`, `slot_deltas`, `aborted_calls`, `entity_candidates` and `intent_shift` are reset and `turn_epoch_start` is set to the current epoch.

`commit()` can raise `SlotPatchError` from `patch_slots` if the final diff is invalid. In the tool-call path the arguments were already validated at dispatch (`stage_call_slots`), so this mostly concerns `MEMORY_UPDATE` entities.

### 3.5 Worked example: a correction

The user says "book a flight from Delhi to Mumbai", then 300 ms later "actually make it Goa".

1. `_handle_user_text` appends the first utterance to `raw_chunks`. The planner calls the LLM, which returns a `book_flight` tool call. `entities_from_tool_call` yields `origin=Delhi` and `destination=Mumbai`. `stage_call_slots` patches both into `session.slots` and returns their previous values (`MISSING` for both). The planner registers the call and records two entity candidates with `source_call_id=call_1`.
2. The second utterance arrives. `_handle_user_text` appends it to `raw_chunks` (now two items). The interrupt classifier fires, the coordinator calls `bump_epoch()`, which cancels `call_1`, calls `revert_call_slots("call_1")` (restoring the slots), and calls `scratchpad.mark_aborted(...)`. Both entity candidates of `call_1` are dropped and one `InFlightCallRecord` with the arguments is kept.
3. The planner now builds the prompt. `build_interrupted_work_note` (section 5) sees the earlier chunk and the aborted call and tells the model: "Earlier requests in this exchange that did not finish: ...; Tool calls cancelled by the user's newest message: book_flight(origin=Delhi, destination=Mumbai)...".
4. The model re-issues `book_flight` with `destination=Goa`. Slots are staged again, with new candidates.
5. When the tool result arrives, `_handle_tool_result` commits the turn. `cleaned_user_intent` is "actually make it Goa", `aborted_calls_count` is 1, and `overrides` is empty, because the first call's slots were reverted and the previous value for `destination` is `MISSING`. The Mumbai value therefore never became a durable slot and no SUPERSEDES edge is created for it. A SUPERSEDES edge appears only when a value that an earlier committed turn established is replaced (section 4.4).

## 4. Tier 2: GraphMemory

### 4.1 Purpose

`GraphMemory` is a persistent, cross-turn graph (documented as a DAG) of what happened in the session. It serves three consumers: the planner (it provides the chat history, via `get_subgraph_prompt_context`), the web and Android UIs (it provides node/edge payloads for the `graph_update` action), and the speech layer (`mark_response_truncated` records what the user heard).

### 4.2 Node and edge types

| Type | Class | Fields |
|---|---|---|
| Turn node | `TurnNode` | `id`, `turn_index`, `intent`, `user_prompt`, `agent_response`, `epoch`, `slots_snapshot`, `timestamp`, `pruned`, `spoken_text` |
| Entity node | `EntityNode` | `id` (prefix `ent_`), `entity_type`, `key`, `value`, `source_turn_id`, `timestamp` |
| Artifact node | `ArtifactNode` | `id` (prefix `art_`), `title`, `language`, `content_hash`, `source_turn_id`, `timestamp` |
| Edge | `Edge` | `source`, `target`, `edge_type` |

The edge types are listed in the module constant `EDGE_TYPES`:

| Edge type | Direction | Meaning | Created by |
|---|---|---|---|
| `NEXT_TURN` | previous head to new turn | Temporal order. | `insert_turn` |
| `SUPERSEDES` | new turn to the old turn | The new turn replaced a slot value an earlier turn established (a real override). | `insert_turn` |
| `REFERENCES` | turn to entity | The turn mentioned or set this entity. | `insert_turn` |
| `PRODUCED` | turn to artifact | The turn produced this artifact. | `link_artifact` |
| `BRANCHES_FROM` | rollback branch node to target turn | A rollback jumped back to this turn. | `rollback_to_node` |

Node ids are generated by `_new_id(prefix)` (a counter plus 6 hex characters of a UUID). Turn ids come from the scratchpad. `content_hash` is Python's built-in `hash()` of the content string, stored as a string; it is salted per process, so it identifies an artifact within a run but is not a stable fingerprint across restarts.

### 4.3 insert_turn

`insert_turn(canonical_turn)` does the following, in order:

1. Creates a `TurnNode` from the canonical turn. `turn_index` is the current number of turns in `_turn_order`, `intent` is read from the live `session.intent`, and `slots_snapshot` is a copy of the turn's `final_slots`.
2. Adds a `NEXT_TURN` edge from the current head, if any.
3. Computes SUPERSEDES edges (section 4.4).
4. Appends the node to `_turn_order` and makes it the head.
5. For each entity candidate calls `link_entity` and adds a `REFERENCES` edge. Note that candidates are stored as separate entity nodes per occurrence; they are not deduplicated across turns.
6. For each produced artifact calls `link_artifact`, which adds a `PRODUCED` edge.
7. Runs `_would_cycle()`; if the ordering edges form a cycle it raises `GraphMemoryError`.

### 4.4 SUPERSEDES

SUPERSEDES marks a real correction: the new turn changed a slot value that an earlier turn had established (the module comment gives the example "destination Mumbai to Goa"). The input is `canonical_turn.overrides`, computed by the scratchpad. For each overridden key and old value, `insert_turn` looks for the turn to point at:

1. Preferred: the turn whose entity introduced the old value. It scans entity nodes in reverse for the same `key` and `value`, whose source turn exists and is not pruned. The reason stated in the code is that later turns merely inherit the value in their snapshots.
2. Fallback: the latest non-pruned turn whose `slots_snapshot[key]` equals the old value.

Each distinct target gets one `SUPERSEDES` edge from the new turn, in sorted id order. Overrides that cannot be matched to any turn create no edge. `tests/test_memory_extraction.py::test_correction_supersedes_the_turn_that_set_the_old_value` covers this end to end.

### 4.5 spoken_text and truncated replies

A reply can be cut off when the user barges in over a spoken answer. `SessionSpeaker` (`agent/speech.py`) reports the words that were actually spoken, and the coordinator wires `on_truncated` to `_record_truncation`, which calls `GraphMemory.mark_response_truncated(full_text, spoken_text)`. That method walks the turns newest first and sets `spoken_text` on the first one whose `agent_response == full_text` and whose `spoken_text` is still `None`. It returns `True` if a turn was found. A `None` `spoken_text` means the full reply was delivered.

The effect shows up in `get_subgraph_prompt_context`. For a truncated turn the assistant message is not the full reply but:

- `"<heard words> [interrupted by the user; the rest was never heard]"` when some words were heard, or
- `"[interrupted by the user before this was heard]"` when nothing was.

So the next prompt reflects what the user could have known. The same `spoken_text` is also emitted on the `speech_state: stopped` action (`agent/schemas/actions.py`, `SpeechStateAction`), and the eval scorer (`agent/eval/scorer.py`) checks that a cut-off reply was recorded as truncated. A limitation worth knowing: matching is by exact reply text, so two identical replies resolve to the most recent one that has not yet been marked.

### 4.6 Reading the graph

- `get_active_thread()` starts at the head and walks backward along `NEXT_TURN` edges (falling back to `BRANCHES_FROM` when a branch node has no predecessor), skips pruned nodes and returns the turns in chronological order. It carries a `visited` guard against loops. The edge search is a linear scan, so the cost per call grows with the number of edges; sessions are bounded by `SESSION_TTL_S` eviction and the history window is small.
- `get_subgraph_prompt_context(current_intent, max_turns=5)` takes the last `max_turns` active turns and emits alternating `user`/`assistant` chat messages, applying the `spoken_text` rule above. The `current_intent` argument is accepted but not used in the function body.
- `get_entities_for_turn(turn_id)` returns the entity nodes created by a turn; the coordinator uses it to work out which nodes are new.
- `to_graph_payload()` serializes all nodes and edges as dictionaries (turn labels are the first 40 characters of the prompt; entity labels are `key=value`).

### 4.7 Rollback to an arbitrary turn

`rollback_to_node(target_turn_id)` jumps the conversation back to an earlier turn. It deliberately does not use `SessionState.rollback_snapshot()`, whose semantics are a single-step LIFO pop pinned by `tests/test_state_machine.py`. Instead it marks every turn after the target `pruned`, applies the target's `slots_snapshot` through `patch_slots` (the same primitive used everywhere else), appends a synthetic branch `TurnNode` whose prompt reads `[rollback to <id>]`, adds a `BRANCHES_FROM` edge, and makes the branch node the head. In the code read for this task, `rollback_to_node` and `get_active_thread` are called only from `GraphMemory` itself and from tests (`tests/test_graph_memory.py::test_rollback_to_node_restores_slots_via_patch_slots`, `tests/test_adversarial_memory.py::test_conversation_rollback_forget_and_go_back`); no coordinator path invokes a rollback, so this is a tested capability rather than a user-visible feature.

### 4.8 Acyclicity

`_would_cycle()` runs a DFS over `NEXT_TURN` and `BRANCHES_FROM` edges only. `SUPERSEDES` and `REFERENCES` are intentionally excluded: they point backward in time next to a forward `NEXT_TURN` edge between the same pair of turns, so including them would flag every ordinary override as a two-cycle. `tests/test_graph_memory.py::test_no_cycles_in_conversation_dag` and `tests/test_adversarial_memory.py::test_dag_has_no_cycles_after_adversarial_sequence` cover it.

### 4.9 How the graph reaches the UI

`AgentCoordinator._commit_turn_and_emit_graph` commits the scratchpad, inserts the turn, then computes the set of new node ids (the turn, its entities, its artifacts). It builds `GraphNodePayload` and `GraphEdgePayload` for new nodes and for edges touching them, and emits a `GraphUpdateAction`. The clients therefore receive incremental updates, not the whole graph each time. The Android client has a matching reducer (`android/app/.../state/ChatReducer.kt`) and the web UI renders the graph in `frontend/src/App.jsx`.

### 4.10 Worked example: two turns, an override and an interruption

1. Turn 1: "search flights Delhi to Mumbai". Result arrives; `commit` creates `CanonicalTurn` with entity candidates origin and destination. `insert_turn` makes `TurnNode` T1, entities E1 (origin), E2 (destination = Mumbai), and `REFERENCES` edges T1 to E1 and E2.
2. Turn 2: "now book it to Goa". The planner stages `destination=Goa`, `previous_value=Mumbai`. At commit, `overrides = {"destination": "Mumbai"}`. `insert_turn` adds `NEXT_TURN` T1 to T2, looks up the entity with key `destination` and value `Mumbai`, finds E2 whose source turn is T1, and adds `SUPERSEDES` T2 to T1.
3. The agent speaks "Your flight ... is confirmed ..." and the user talks over it after four words. `SessionSpeaker` stops and reports the spoken words; `mark_response_truncated` sets `T2.spoken_text`.
4. Turn 3's prompt now contains: user "search flights Delhi to Mumbai", assistant (full reply), user "now book it to Goa", assistant `"<four words> [interrupted by the user; the rest was never heard]"`.

## 5. Context builder

`agent/memory/context_builder.py` is the single place `agent/slow_path/planner.py` uses to obtain memory-derived prompt content.

| Function | Role |
|---|---|
| `build_interrupted_work_note(session)` | Builds the one-off note about abandoned work (below). Returns an empty string if the session has no scratchpad or nothing to report. |
| `build_context_block(session)` | Returns `"Current session intent: <intent or unknown>. Current slots: <slots dict>."`, with the note appended if there is one. It is placed at the end of the planner's system prompt. |
| `build_history_messages(session, max_turns=5)` | Returns the prior chat messages from `GraphMemory.get_subgraph_prompt_context`, or `[]` if the session has no graph memory (the pre-memory behaviour). |
| `build_resolved_entity_state(session)` | Returns `"Resolved entities: k=v, ..."` from the slots. Not referenced anywhere else in `agent/` in the code read, so it appears to be unused. |

### 5.1 The interrupted-work note

`build_interrupted_work_note` exists because an interrupted request is never committed and its slot changes are rolled back, so without the note the model would see "Actually make it Goa" with nothing to correct and would ask the user to repeat themselves. It builds the note from the scratchpad:

- Earlier utterances: all `raw_chunks` except the last (the last chunk is the newest message), stripped, non-empty, and only the last three are shown.
- Cancelled calls: aborted calls that have arguments, formatted as `tool(k=v, ...)`, last three only.
- A closing instruction: "If the newest message corrects or adjusts that work, apply the correction and re-issue the tool call with the updated arguments instead of asking the user to repeat or confirm."

Because the scratchpad is purged on every commit, the note exists only while an exchange is still unfinished. After the corrected request commits, the note disappears and the history comes from the graph instead.

### 5.2 Full prompt layout

The planner builds its message list as: one system message (persona and tool-use rules, the `MEMORY_UPDATE` line format, then `build_context_block`), then the history messages, then the current user text. When the continuation after `analyze_frame` runs, the user message gets a "[Vision result for the image the user is sharing: ...]" suffix. History is limited to 5 turns by default, so the prompt stays small regardless of session length.

## 6. tool_slots and SKIP_TOOLS

### 6.1 Purpose

The comment at the top of `agent/memory/tool_slots.py` explains the reason: live probing showed the LLM returns a tool call with no text for nearly every action turn, so a text-only `MEMORY_UPDATE` could never capture the entities. The arguments the model chose for the tool are already the resolved parameters (for example "Mumbai" then "Goa" after a correction), so the code records them as entities directly, with no dependence on the model following an output format.

### 6.2 entities_from_tool_call

`entities_from_tool_call(tool_name, arguments)` returns a list of `(entity_type, key, value)` tuples:

- It returns `[]` if the tool is in `SKIP_TOOLS` or there are no arguments.
- It keeps only scalar arguments: `str`, `int` or `float`. Booleans, lists, dicts and `None` are skipped. Empty or whitespace-only strings are skipped.
- The entity type comes from `ENTITY_TYPE_BY_KEY`, falling back to `PARAM`.

| Argument key | Entity type |
|---|---|
| `origin`, `destination`, `city` | `LOCATION` |
| `date` | `DATE` |
| `nights` | `QUANTITY` |
| `flight` | `FLIGHT` |
| `hotel_name` | `HOTEL` |
| `booking_id` | `BOOKING` |
| any other key | `PARAM` |

### 6.3 SKIP_TOOLS

`SKIP_TOOLS = {"spawn_agent", "analyze_frame", "export_artifact"}`. Their arguments are not user facts:

- `spawn_agent` arguments describe the worker (name, role, goal, steps).
- `analyze_frame`'s argument is a question to the vision model.
- `export_artifact` can carry an entire source file in `content`. Slots appear in every snapshot, in every prompt (`build_context_block`) and in the idempotency key, so a whole file as a slot would bloat all three. The unit test `tests/test_memory_extraction.py::test_spawn_agent_arguments_are_not_user_entities` covers the spawn case.

Note that `set_timer` is not in `SKIP_TOOLS`, so a timer call writes `seconds` (as `PARAM`) and `label` (as `PARAM`) into the session slots like any other tool. This is how the code behaves as read; it is harmless for correctness but means timer values appear in the state snapshot.

### 6.4 The dispatch path that uses it

In `Planner.plan` (`agent/slow_path/planner.py`) for a `tool_call` response:

1. `tool_router.validate_call` checks the arguments against the tool's JSON schema; an invalid call is rejected and the user is asked for details.
2. `entities_from_tool_call` computes the entities.
3. `session.stage_call_slots(call_id, {key: value})` patches them into `session.slots` before registration. This ordering matters: the idempotency key hashes the slots, so two identical requests produce the same key and two different requests do not. `stage_call_slots` records an undo entry (`_call_slot_undo`) with each slot's previous value. A `SlotPatchError` (for example `nights` outside 1 to 60, or origin equal to destination) rejects the whole turn with a user-safe message.
4. `register_tool_call` registers the call. If it returns `None` (an idempotency duplicate), the planner calls `revert_call_slots` so nothing leaks.
5. Otherwise the planner records the entity candidates with `call_id` and `previous_value`.

If the call is later cancelled, `bump_epoch` calls `revert_call_slots` (restoring a slot only if it still holds the value that call set) and `mark_aborted` (discarding the candidates). If it completes, `complete_tool_call` drops the undo record, making the values permanent.

## 7. Configuration

The memory modules have no environment variables of their own. The knobs are constants and a few settings elsewhere:

| Knob | Where | Default | Effect |
|---|---|---|---|
| `max_turns` | `build_history_messages` and `get_subgraph_prompt_context` | 5 | Number of past turns sent to the LLM. |
| `max_history` | `SessionState.__init__` | 10 | Size of the slot-snapshot ring buffer used for `rollback_snapshot`. |
| Interrupted-work note length | `build_interrupted_work_note` | last 3 chunks, last 3 calls | Bounds the note. |
| `SESSION_TTL_S` | `agent/coordinator.py` | see README/settings | Idle sessions are evicted along with their graph (`evict_idle_sessions`); a session with in-flight work is never evicted. |
| `MAX_TEXT_LEN` / `MAX_NIGHTS` | `agent/coordination/slot_validation.py` | 200 / 60 | Limits on slot text length and nights. |

## 8. Interactions with other components

- `SessionState` creates and owns both tiers; slot patches, history and rollback live there.
- `bump_epoch()` calls `scratchpad.mark_aborted` for each cancelled call, linking the epoch model to memory hygiene.
- The planner reads the context builder and writes entity candidates and intent shifts.
- The coordinator appends utterances, commits turns (synchronously for spoken replies and clarifications, in `_handle_tool_result` for tool turns) and emits `GraphUpdateAction`. Because tool-call turns commit only when the result returns, a cancelled turn that never returns is never committed, which is the point.
- The speech layer feeds `mark_response_truncated` through `_record_truncation`.
- Idempotency (`agent/coordination/idempotency.py`) hashes slots, so the order "stage slots, then register call" is part of the memory contract.
- Workers produce artifacts; the coordinator passes them to `commit(artifacts=...)`, which makes `ArtifactNode` and `PRODUCED` edges. A separate in-memory list (`AgentCoordinator._artifacts`, last 10 per session) is what `export_artifact` reads; it is not part of the graph.

## 9. Failure modes and guarantees

- Abandoned values never reach durable state: aborted-call entities are dropped from the scratchpad and their slots reverted. Covered by `test_aborted_call_entities_never_leak_into_slots` and `test_coordinator_correction_flow_ends_with_only_the_corrected_slot`.
- An invalid or contradictory slot patch is refused before dispatch (or rolled back), and the session is left unchanged.
- Memory is optional: without `ensure_memory()`, `build_history_messages` returns `[]`, `build_interrupted_work_note` returns `""`, and `_commit_turn_and_emit_graph` is a no-op.
- A cycle in the ordering edges raises `GraphMemoryError` at insert time. Because the check runs after the turn has been added, the turn stays in the structures when the error is raised; in practice `insert_turn` only adds forward edges, so this is a defensive invariant.
- A rollback to an unknown turn id raises `GraphMemoryError`.
- Latency is budgeted by `tests/test_adversarial_memory.py::test_commit_and_insert_turn_latency_budget`.
- Limitations: `spoken_text` matching is by exact reply text; the active-thread walk and SUPERSEDES search are linear scans; `content_hash` uses process-salted `hash()`; entity nodes are not merged across turns.
- Items that look unused or inconsistent: `build_resolved_entity_state` has no caller, `current_intent` is an ignored parameter, and `rollback_to_node` has no production caller.

## 10. Tests that cover it

| Test file | What it covers |
|---|---|
| `tests/test_scratchpad.py` | Commit equals direct `patch_slots`, last-write-wins, purge after commit, aborted-call tracking via `bump_epoch`, unaffected bare `SessionState`. |
| `tests/test_graph_memory.py` | `NEXT_TURN` chain, entity and artifact linking, rollback through `patch_slots`, clean context after a correction, no cycles. |
| `tests/test_memory_extraction.py` | `entities_from_tool_call` typing and filtering, `SKIP_TOOLS`, `MEMORY_UPDATE` parsing variants, SUPERSEDES targeting, aborted-call isolation, idempotency with different arguments, end-to-end correction. |
| `tests/test_memory_baseline.py` | Baseline of no history, memory-enabled context staying flat and clean, measured token delta. |
| `tests/test_adversarial_memory.py` | Rapid slot overwrites, rollback ("forget and go back"), DAG invariants after adversarial sequences, commit and insert latency. |
| `tests/test_slot_validation.py` | Slot patch validation and rollback on bad state. |
| `tests/test_tool_replies.py::test_reply_is_recorded_in_conversation_graph` | Tool-turn replies land in the graph. |
| `agent/eval/scorer.py` (eval harness) | Checks that cut-off replies are recorded as truncated (`spoken_text`). |
