# Coordinator and Event Flow

This document explains how a single user input travels through Kairos from the moment it arrives at the coordinator until the last action is handed to the WebSocket. It covers `agent/coordinator.py` (the orchestrator), the event and action schemas (`agent/schemas/events.py`, `agent/schemas/actions.py`), the trace logger (`agent/trace_logger.py`), metrics (`agent/metrics.py`), structured logging (`agent/logging_setup.py`), start-up warm-up (`agent/warmup.py`) and the CLI entrypoint (`agent/main.py`). The epoch model, slots, idempotency and tool router that the coordinator drives are documented in `02_coordination_core.md` and are only referenced here when needed to explain the flow.

## 1. The big picture

The coordinator is a thin, single-process orchestrator built around two asyncio queues:

- an inbound **event queue** (`AgentCoordinator.event_queue`) that carries everything that happens to a session: typed text, audio chunks, video frames, explicit interrupts and tool results;
- an outbound **action queue** (`AgentCoordinator.action_queue`) that carries everything the agent wants the client to see or hear: fillers, tool calls, cancellations, spoken replies, state snapshots, transcripts, audio, graph updates and exported files.

One class, `AgentCoordinator`, owns both queues, a dictionary of per-session state (`self.sessions`, a `SessionState` per session id), the planner, the Tier 1 classifier, the tool router, the trace logger and optional speech, voice and vision helpers. The server (`agent/server.py`) creates exactly one coordinator for the process and a single dispatcher task that reads the action queue and fans each action out to the connections of its own session.

```
 client --WS--> server.py --post_event--> event_queue
                                              |
                                  _process_events (one loop task)
                                              |
                                        _handle_event
              +-----------+----------+----------+--------------+
              |           |          |          |              |
          INTERRUPT   USER_TEXT   AUDIO     VIDEO         TOOL_RESULT
              |           |       CHUNK      FRAME              |
      _handle_interrupt   |          |         |        _handle_tool_result
              |     debounce/turn    VAD,     ring            |
              |     _handle_user_text ASR     buffer          |
              |           |                                   |
              |   Tier1 classify -> Tier2 filler -> Tier3 planner (LLM)
              |           |                                   |
              +----> emit_action  <---- _dispatch_actions / _execute_tool_task
                          |
          TraceLogger.log_action (validate) -> action_queue -> metrics
                          |
          server dispatcher -> per-session inbox -> WebSocket -> client
```

The central design idea is that the event loop must never be blocked by slow work. The single `_process_events` task only routes events and does cheap bookkeeping. Anything slow (an LLM call, a tool call, Whisper transcription, a debounce wait) runs in its own asyncio task, and anything that could be stale when it finishes is checked against the session epoch before it takes effect.

## 2. Event and action schemas

### Purpose

`agent/schemas/events.py` and `agent/schemas/actions.py` define the typed vocabulary of the system as pydantic models. They are the contract between the clients (web and Android), the coordinator, the trace log and the evaluation harness. Every model derives from a small base (`BaseEvent`, `BaseAction`) that carries an id, the session id, a type tag and a timestamp taken from `agent.clock` (so virtual-time evaluations get virtual timestamps).

### How it works

Events flow in, actions flow out. Each concrete class fixes its type tag with a default value, so `UserTextEvent(session_id=..., text=...)` is automatically `event_type == EventType.USER_TEXT`. The coordinator dispatches on that tag in `_handle_event`, and the trace logger and metrics label records by it.

Events (`agent/schemas/events.py`):

| Event class | Type tag | Notable fields |
|---|---|---|
| `UserTextEvent` | `user_text` | `text`, `is_partial`, `barge_in_handled`, `immediate` |
| `AudioChunkEvent` | `audio_chunk` | `audio_bytes`, `format` (default `pcm_16khz`), `is_final`, `streaming`, `stream_control` (`start` or `stop`) |
| `VideoFrameEvent` | `video_frame` | `frame_data`, `mime` (default `image/jpeg`), `source` (`camera`, `screen`, `harness`), `frame_id` |
| `InterruptSignalEvent` | `interrupt_signal` | `reason` (default `user_barge_in`), `confidence` |
| `ToolResultEvent` | `tool_result` | `call_id`, `tool_name`, `epoch`, `result`, `error` |

Two flags on `UserTextEvent` matter for the flow. `immediate=True` marks a complete utterance (a voice endpoint) that needs no burst-coalescing window. `barge_in_handled=True` records that a voice barge-in already bumped the epoch from a partial transcript, so the final text must not bump it a second time.

Actions (`agent/schemas/actions.py`), all with `action_id`, `session_id`, `epoch`, `timestamp` and a free-form `payload` dict inherited from `BaseAction`:

| Action class | Type tag (wire value) | Purpose |
|---|---|---|
| `FillerAction` | `filler` | Tier 2 instant acknowledgement text |
| `SpokenResponseAction` | `spoken_response` | A reply (`text`, `is_final`) |
| `ClarificationAction` | `clarification` | A question to the user (`question`, `target_slot`) |
| `ToolCallAction` | `tool_call` | A dispatched call (`call_id`, `tool_name`, `arguments`, `is_state_modifying`, `idempotency_key`) |
| `ToolCancelAction` | `tool_cancel` | A cancelled call (`call_id`, `tool_name`, `reason`, default `epoch_stale`) |
| `StateSnapshotAction` | `state_snapshot` | Intent, slots and the in-flight calls (`InFlightCallInfo` list) |
| `AgentStepAction` | `agent_step` | Progress of an autonomous worker, may carry an `artifact` |
| `TranscriptAction` | `transcript` | What ASR heard, partial or final (`is_partial`, `utterance_id`) |
| `VoiceActivityAction` | `voice_activity` | `listening`, `speech_start`, `speech_end`, `barge_in`, `idle` |
| `AudioOutAction` | `audio_out` | One synthesized sentence (`seq`, `audio_b64`, `is_last`) |
| `SpeechStateAction` | `speech_state` | Spoken reply lifecycle: started, ducked, resumed, finished, stopped (with `spoken_text`) |
| `FileExportedAction` | `file_exported` | An exported file (`filename`, `path`, `editor_uri`, `download_path`, `preview`) |
| `GraphUpdateAction` | `graph_update` | Memory graph delta (`GraphNodePayload`, `GraphEdgePayload`, `op` of `append` or `full`) |

Note that the Python class `GraphUpdateAction` uses the enum member `ActionType.GRAPH_SNAPSHOT`, whose wire value is `graph_update`; clients must key on the string value.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `EventType`, `ActionType` | `agent/schemas/events.py`, `agent/schemas/actions.py` | String enums for dispatch and labelling |
| `BaseEvent`, `BaseAction` | same | Common envelope with uuid ids and clock-based timestamps |
| `InFlightCallInfo` | `agent/schemas/actions.py` | Compact record of a pending or running call inside a snapshot |
| `GraphNodePayload`, `GraphEdgePayload` | `agent/schemas/actions.py` | Wire shape of memory graph nodes and edges |
| package exports | `agent/schemas/__init__.py` | Re-exports the core subset only |

### Configuration

None. The schemas read no environment variables. The only external dependency is `agent.clock`.

### Interactions

- The coordinator builds actions and consumes events; `SessionState` builds `ToolCallAction`, `ToolCancelAction` and `StateSnapshotAction` itself.
- `scripts/dump_android_fixtures.py` serializes the real classes into fixtures that the Kotlin `Protocol.kt` parses; any change to an action must be mirrored there (see `CLAUDE.md`, checked by `tests/test_android_fixtures.py`).
- The trace logger validates the dumped form of each action.

### Failure modes and guarantees

- Pydantic rejects malformed construction at the point of creation, so a bad action cannot enter the queue silently.
- `agent/schemas/__init__.py` exports only the original action types (filler, spoken, clarification, tool call, cancel, snapshot) and not the later ones (`AgentStepAction`, `TranscriptAction`, `VoiceActivityAction`, `AudioOutAction`, `SpeechStateAction`, `FileExportedAction`, graph payloads). This is not a bug, because the coordinator imports them from `agent.schemas.actions` directly, but the package-level export list is incomplete.
- Bytes in events (audio, frames) are not JSON-serializable; the trace logger replaces them with size markers (section 7).

### Tests that cover it

`tests/test_android_fixtures.py` (protocol contract), `tests/test_trace_logger.py` (schema of the trace records built from them), `tests/test_imports.py` (every module imports).

## 3. The coordinator (`agent/coordinator.py`)

### Purpose

`AgentCoordinator` is the single place where an input becomes a reaction. It exists so that the three latency tiers (Tier 1 interrupt classification, Tier 2 instant acknowledgement, Tier 3 LLM planning) run in the right order and so that stale work is cancelled instead of completed. It does not decide what to do (the planner does) and does not know how to book a flight (the tool router does). It decides when things run, which epoch they belong to, and what the client is told.

### How it works: the pipeline, step by step

**Construction.** `AgentCoordinator.__init__` creates both queues and wires its collaborators: a `ToolRouter`, a `TraceLogger`, an `IntentClassifier` (with `use_embeddings=embeddings_enabled()`), an LLM backend from `get_backend(LLMConfig())`, a `Planner`, an `ASRProcessor`, a vision backend and an optional TTS backend. Every collaborator can be injected, which is how tests and the evaluation harness substitute mocks. Constructor knobs: `enable_debounce` (default `True`) and `debounce_window_s` (default `0.10`).

**Lifecycle.** `start()` launches two background tasks: `_process_events` (the main loop) and `_evict_idle_sessions_loop`. `stop()` cancels both, cancels pending debounce timers, bumps every session's epoch with reason `system_shutdown` (which cancels every in-flight tool task), cancels the turn tasks and voice tasks, closes the speakers and clears the voice and audio state.

**Entry: `post_event`.** Every inbound event passes through `post_event`, which does four things before queueing:

1. `trace_logger.log_event(event)` records it;
2. it calls `session.touch()` for an existing session (resets the idle clock) and increments `agent_events_total{type}`;
3. for non-empty `USER_TEXT` it calls `_stop_speech(session_id, "user_spoke")`, so the agent stops talking at once and does not wait out the debounce window;
4. for `USER_TEXT` and `INTERRUPT_SIGNAL` it stores `self._reaction[session_id] = [now, False, False]`. This starts the clock for two latency metrics: first acknowledgement and interrupt-to-cancel.

Then it puts the event on `event_queue`.

**The loop: `_process_events` and `_handle_event`.** The loop awaits `event_queue.get()` and calls `_handle_event`, catching and logging any exception so one bad event cannot kill the loop. `_handle_event` fetches or creates the `SessionState` (`get_or_create_session` also calls `session.ensure_memory()` so every session has a scratchpad and a graph memory) and sets two `contextvars` (`session_id_var`, `epoch_var`) so every log line from this handling carries the session and epoch. Then it routes by event type:

- `INTERRUPT_SIGNAL` goes straight to `_handle_interrupt` (no debounce, no classification: an explicit interrupt is trusted);
- `USER_TEXT` goes to one of three paths: `immediate` events spawn a turn task now, otherwise (when debounce is enabled and the window is positive) `_queue_debounced_user_text`, otherwise `_handle_user_text` is awaited inline;
- `AUDIO_CHUNK` goes to `_handle_audio_chunk`;
- `VIDEO_FRAME` goes to `_handle_video_frame` (buffering only);
- `TOOL_RESULT` goes to `_handle_tool_result`.

### 3.1 Debounce: coalescing rapid-fire corrections

`_queue_debounced_user_text` implements the burst rule from the design notes (section 7.5 in code comments). For each session it appends the event to `self._pending_events[sid]`, cancels any running debounce timer for that session and starts a new one (`_flush_after_delay`). The timer sleeps `debounce_window_s`; if it is cancelled (a newer event arrived inside the window) it returns silently, otherwise it pops the pending list and calls `self._spawn_turn(self._handle_user_text(session, events[-1]))`. In other words, only the last event of a burst is planned.

Two details are deliberate and documented in the code. First, planning runs in its own task via `_spawn_turn`, not inside the timer task, because cancelling the timer for a newer message must never kill a plan that is already in progress (an earlier version silently lost the earlier request). Whether a running plan is still wanted is decided by the epoch check in the planner, not by task cancellation. Second, `_spawn_turn` keeps a strong reference in `self._turn_tasks` and logs any exception from the task in a done-callback.

A consequence worth knowing: for a burst such as "Book flight to NYC", "Actually wait make that Boston", "No sorry, Chicago", only the text "No sorry, Chicago" reaches classification and the planner; the earlier texts are neither classified nor appended to the scratchpad (`append_utterance_chunk` is called inside `_handle_user_text`). The test `tests/test_adversarial_timing.py::test_rapid_fire_corrections_coalescing` asserts that exactly one filler is emitted and the epoch is at most 2. The planner's view of the earlier texts therefore depends on what memory already holds, and this is a design trade-off rather than a verified guarantee that the final text alone is enough context.

A second cost: because the debounce delay is spent before classification, a typed correction cannot cancel a running tool earlier than `debounce_window_s` (default 100 ms) after it was posted. Explicit `InterruptSignalEvent`s and voice barge-ins skip this delay.

### 3.2 The turn: `_handle_user_text`

This is the heart of the coordinator. In order:

1. **Memory and speech.** Append the text to the scratchpad (`append_utterance_chunk`) and stop any speech in progress.
2. **Work check.** `has_work` is true if any in-flight call is `pending` or `running`.
3. **Pending yes/no answer.** If `session.pending_clarification` is set, a previous mid-band utterance asked "Do you want me to change what I'm working on?". `_yes_no` (two regexes `_YES` and `_NO` on the start of the text) decides: "yes" replaces the text with the original utterance and forces an interrupt; "no" emits `SpokenResponseAction("Okay, I'll carry on as before.")` and ends the turn; anything else is treated as a fresh request.
4. **Tier 1 classification.** `_classify(text)` returns the classifier dict; `trace_logger.log_classification` records it. `is_interrupt` is true if the classifier said so, or `event.barge_in_handled`, or the yes-answer forced it.
5. **Mid-band clarification.** If the classifier says `needs_clarification`, there is work in flight and it is not already an interrupt, the coordinator stores `pending_clarification` and emits a `ClarificationAction` asking yes/no, then returns without touching the epoch. This is how an ambiguous utterance avoids thrashing the epoch.
6. **Interrupt.** If `is_interrupt and not event.barge_in_handled`, call `session.bump_epoch(reason=f"user_correction: {text}")` and emit one `ToolCancelAction` per cancelled call.
7. **Tier 2 filler.** `generate_filler(intent, slots, is_interruption)` yields the text instantly (an interruption always gets `INTERRUPT_ACKS[0]`, "Got it, changing that."). It is emitted as a `FillerAction` stamped with the (possibly new) epoch.
8. **Tier 3 planning.** `self._active_plans[sid]` is incremented around `self.planner.plan(event, session, plan_epoch=session.epoch, on_slow=self._slow_notice(...))`. `plan_epoch` is the epoch the plan started under; the planner returns an empty list if the epoch moved while the LLM was thinking. `_slow_notice` returns a callback that the LLM client invokes at its soft deadline; it emits "Still working on that -- one moment." only if the epoch is unchanged.
9. **Dispatch.** An empty plan means the turn was superseded: nothing is emitted and no turn is committed. Otherwise `_dispatch_actions` runs.
10. **Commit.** If the plan contained no tool call (a spoken reply or clarification), the turn is complete now and `_commit_turn_and_emit_graph` commits it. Turns with tool calls commit later, in `_handle_tool_result`.

### 3.3 Tier 1 classification: `_classify`

`_classify` is deliberately inline (not `to_thread`). Its comment records a real bug found by the `rapid_fire_corrections` scenario: with a thread hop, a later correction could finish classification first, reorder the epoch bumps and let the stale plan win (it booked "Boston" instead of "Chicago"). If the classifier is `ready`, the call runs synchronously (about 5 ms of CPU). If MiniLM has not loaded yet, `_classify` schedules `clf.aload()` in the background (guarded by `_classifier_loading`) and answers immediately with `classify_text(text, force_keywords=True)`, so the fast path never waits for a model import. The classifier internals (anchors, calibrated weights, thresholds, rules) belong to `04_fast_and_slow_path.md`.

### 3.4 Interrupt handling: `_handle_interrupt`

Used for `InterruptSignalEvent` (the UI stop button, the CLI `interrupt` command). It calls `_stop_speech(..., "interrupt")`, then `session.bump_epoch(reason=...)` and emits the resulting `ToolCancelAction`s. If something was actually cancelled, or a plan is awaiting the LLM (`_active_plans > 0`), it emits an interruption `FillerAction` so that a bare "stop" is not met with silence. Finally it emits a fresh `StateSnapshotAction` showing the cancelled calls gone from the in-flight list.

There are three different paths that can bump the epoch, and the coordinator takes care to bump only once per user intent:

| Path | Trigger | Where |
|---|---|---|
| Explicit interrupt | `InterruptSignalEvent` | `_handle_interrupt` |
| Text correction | Classifier says interrupt | `_handle_user_text` step 6 |
| Voice barge-in | Partial transcript reads as a correction | `_maybe_voice_barge_in`, which sets `barge_in_handled` so the final text skips step 6 |

### 3.5 Tool execution

`_dispatch_actions(session, actions, origin_text)` iterates over the plan. For a `ToolCallAction` it sets `turn_has_tool_call`, remembers the originating user text in `self._call_origin[call_id]`, emits the action and spawns `_execute_tool_task(...)` as a task, which it attaches to the in-flight call with `session.attach_task(call_id, task)` (so `bump_epoch` can cancel it). Other actions (spoken response, clarification, snapshot) are emitted in order. The planner has already registered the call in the session (`register_tool_call`), which is where the idempotency check happens; `dispatch_tool_call` is a second, public entry point that does registration itself and is used by tests and direct callers.

`_execute_tool_task` has four branches by tool name:

| Tool | Executed by |
|---|---|
| `spawn_agent` | `create_agent_worker(...)` from `agent.workers.registry`, `worker.execute(step_callback=...)`, each step emitted as an `AgentStepAction` |
| `export_artifact` | `_run_export`: content from the arguments or the newest artifact stored for the session; written by `exporter.export_file` in a worker thread (inline under the virtual clock) |
| `analyze_frame` | `_run_vision`: asks the vision backend about the newest buffered frame |
| anything else | `self.tool_router.execute_tool(name, arguments)` |

In every branch `asyncio.CancelledError` is caught and the task returns without posting anything: a cancelled call is silent. Any other exception becomes the `error` string. After the branch, a non-dict result with no error is converted into an error ("the service returned an unreadable response"), because reporting "Done" for a garbled payload would be a false completion claim. Finally the task posts a `ToolResultEvent` (with the epoch the call started under) back to the event queue through `post_event`. Results thus re-enter the same serialized loop as every other event.

### 3.6 Tool results: `_handle_tool_result`

1. `session.complete_tool_call(call_id, result, error)` returns False if the call was cancelled or belongs to an older epoch; the staleness decision is made from the session's own record of the call, not from the event's `epoch` field. A False result is logged ("Discarding stale or cancelled tool result") and the handler returns: no action, no reply, no commit. This is the second line of defence behind task cancellation, for the case where a result slipped into the queue just before the cancel.
2. A successful `export_artifact` emits a `FileExportedAction`.
3. An `analyze_frame` result is an observation, not an answer: the coordinator emits a snapshot and spawns `_continue_after_observation`, which re-plans the original user request with the observation attached (one step only; the continuation is not offered `analyze_frame` again, so it cannot loop).
4. Otherwise a visible reply is composed: `summarize_tool_error` for an error, an "Agent '...' has successfully finished building ..." message for a worker artifact, or `summarize_tool_result` for ordinary results (`agent/tool_summaries.py`). It is emitted as a `SpokenResponseAction`, followed by a state snapshot, then `_commit_turn_and_emit_graph`.

### 3.7 Committing a turn and the graph stream

`_commit_turn_and_emit_graph` is a no-op if the session has no scratchpad or graph memory. Otherwise it commits the scratchpad (`scratchpad.commit(agent_response, artifacts)`), inserts the resulting `CanonicalTurn` into `graph_memory` (`insert_turn`), collects the new turn node, its entities and its artifacts, and emits one `GraphUpdateAction(op="append")` with only the new nodes and the edges touching them. Details of the memory model are in `06_memory.md`.

### 3.8 `emit_action`: the single outbound gate

Every action leaves through `emit_action`, which does, in order:

1. `trace_logger.log_action(action)`: validates invariants and the schema, may raise in strict mode (section 7);
2. `action_queue.put(action)`;
3. `agent_actions_total{type}` increment;
4. for an `AgentStepAction` with an artifact, append it to `self._artifacts[session_id]` (capped at the newest 10; this is what a later `export_artifact` without content exports);
5. reaction timing: the first `FillerAction` after the last user text or interrupt observes `agent_first_ack_seconds`; the first `ToolCancelAction` observes `agent_interrupt_cancel_seconds`;
6. if the action is spoken (`filler`, `spoken_response`, `clarification`) and the session opted into speech, hand its text to `SessionSpeaker.enqueue(text, epoch)`.

Because the trace validation is the first thing in `emit_action`, a violating action never reaches the client in strict mode.

### 3.9 Voice runtime hooks

Voice is covered in depth in `08_voice_vision_speech.md`; this section describes only how the coordinator hosts it. Audio arrives as `AudioChunkEvent`. `_handle_audio_chunk` forks on `event.streaming or event.stream_control`:

- **Push-to-talk (legacy path).** Bytes accumulate in `self._audio_buffers[sid]`. When `is_final` is set, or the buffer reaches 48,000 bytes (1.5 s of 16 kHz PCM16), the buffer is transcribed with `asyncio.to_thread(asr_processor.transcribe_audio_bytes, ...)`, a `TranscriptAction` is emitted, and non-empty text becomes a `UserTextEvent` that goes through the same debounce or direct path as typed text.
- **Streaming path.** `_handle_voice_stream` owns a `_VoiceRuntime` per session (a `VoiceStream` built on `make_vad()`, plus partial/final tasks and bookkeeping sets and dicts). `stream_control == "start"` creates (or resets) the runtime and emits `VoiceActivityAction("listening")`; audio before a start is ignored; `"stop"` flushes the stream, drops the runtime and emits `idle`. Frames are fed to `runtime.stream.feed(...)`, which returns `SpeechStart`, `PartialDue` and `UtteranceEnd` events that `_dispatch_voice_events` handles.

What `_dispatch_voice_events` does:

| Voice event | Coordinator reaction |
|---|---|
| `SpeechStart` | Emit `speech_start`; if the agent is speaking, `speaker.duck()` (or stop at once if `VOICE_STOP_ON_SPEECH_START=1`) |
| `PartialDue` | If no partial is in flight, spawn `_voice_partial` (one at a time; skip if Whisper is busy) |
| `UtteranceEnd` | Mark the utterance finalized, emit `speech_end`, chain `_voice_final` after the previous final (`runtime.final_tail`) so utterances reach the planner in order |

`_voice_partial` transcribes in a thread, drops the result if the utterance already ended, discards it if `speaker.is_echo(text)` (the agent hearing itself), and while the agent is speaking requires either an interrupt word or two consecutive substantial partials (`runtime.talkover`) before `speaker.stop("user_spoke")`. It emits a partial `TranscriptAction`, adapts the endpoint with `endpoint_hint_ms` if `VOICE_ADAPTIVE_ENDPOINT` is on, then calls `_maybe_voice_barge_in`.

`_maybe_voice_barge_in` is the "cancel before the sentence ends" feature: if something is actually running and the partial classifies as an interrupt, it records the utterance in `barge_in_fired`, resets the reaction clock (marking the acknowledgement as already observed), bumps the epoch, emits the cancels, a `VoiceActivityAction("barge_in")` and a snapshot. It only fires once per utterance.

`_voice_final` waits for the previous final, reuses the last tail partial if it covered all but `VOICE_FINAL_REUSE_SLACK_MS` (default 100 ms) of the audio, otherwise runs Whisper again, emits the final transcript, drops echoes (and calls `speaker.resume()`), stops speech otherwise, and finally posts `UserTextEvent(text, barge_in_handled=handled, immediate=True)`. The text then goes through the normal queue, so voice and typed input share the same turn logic; `immediate=True` bypasses the debounce and `barge_in_handled` prevents a second epoch bump.

Speech opt-in is `set_tts(session_id, enabled)`: it creates a `SessionSpeaker` with callbacks `on_truncated` (records on the graph what the user actually heard via `mark_response_truncated`) and `on_activity` (raises the VAD threshold to `VOICE_SPEAKING_VAD_THRESHOLD`, default 0.75, while the agent talks). It returns False if no TTS backend exists.

### 3.10 Vision hooks

`_handle_video_frame` validates the frame (non-empty, no larger than `MAX_FRAME_BYTES`, MIME in `ALLOWED_MIME`) and appends it to a per-session ring buffer of `FRAMES_PER_SESSION = 3`. It does no inference. Vision only runs when the planner calls `analyze_frame`; `latest_frame` refuses frames older than `MAX_FRAME_AGE_S = 15` seconds so a stale image is never described as current.

### 3.11 Session eviction

`evict_idle_sessions` drops sessions idle longer than `SESSION_TTL_S` (default 3600 s; `<= 0` disables), checked every `SESSION_EVICT_INTERVAL_S` (default 300 s) by `_evict_idle_sessions_loop`. A session is never evicted while it has pending or running calls, an `_active_plans` entry or a live voice runtime. Eviction also clears the call origins, debounce timer, pending events, audio buffer, frames, reaction record, artifacts and speaker.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `AgentCoordinator` | `agent/coordinator.py` | Owns queues, sessions and the whole pipeline |
| `post_event` / `emit_action` | `agent/coordinator.py` | The single inbound gate and the single outbound gate |
| `_process_events`, `_handle_event` | `agent/coordinator.py` | Serialized main loop and router |
| `_queue_debounced_user_text`, `_spawn_turn` | `agent/coordinator.py` | Burst coalescing and turn task lifecycle |
| `_handle_user_text` | `agent/coordinator.py` | Tier 1, Tier 2 and Tier 3 sequence for a turn |
| `_handle_interrupt` | `agent/coordinator.py` | Explicit interrupt: bump, cancel, acknowledge, snapshot |
| `_dispatch_actions`, `dispatch_tool_call`, `_execute_tool_task` | `agent/coordinator.py` | Start tool calls as tasks and report results as events |
| `_handle_tool_result` | `agent/coordinator.py` | Stale-result discard, reply composition, turn commit |
| `_continue_after_observation` | `agent/coordinator.py` | One-step re-plan after a vision observation |
| `_commit_turn_and_emit_graph` | `agent/coordinator.py` | Commit scratchpad turn, stream graph delta |
| `_handle_voice_stream`, `_voice_partial`, `_voice_final`, `_maybe_voice_barge_in` | `agent/coordinator.py` | Voice runtime hooks |
| `_VoiceRuntime`, `_Frame` | `agent/coordinator.py` | Per-session voice state and buffered frame |
| `evict_idle_sessions` | `agent/coordinator.py` | Bounded memory for long-running servers |

### Configuration

| Setting | Default | Meaning |
|---|---|---|
| `enable_debounce` (constructor) | `True` | Coalesce typed text bursts |
| `debounce_window_s` (constructor) | `0.10` | Burst window in seconds |
| `SESSION_TTL_S` | `3600` | Idle seconds before a session is evicted (`<= 0` disables) |
| `SESSION_EVICT_INTERVAL_S` | `300` | How often the eviction pass runs |
| `VOICE_STOP_ON_SPEECH_START` | `0` | `1` stops the agent's voice the moment the VAD hears speech |
| `VOICE_SPEAKING_VAD_THRESHOLD` | `0.75` | VAD threshold applied while the agent is speaking |
| `VOICE_ADAPTIVE_ENDPOINT` | `1` | Adaptive end-of-utterance timing |
| `VOICE_FINAL_REUSE_SLACK_MS` | `100` | How much audio a reused partial may miss |
| `INTENT_EMBEDDINGS` | auto | Read by `embeddings_enabled()`: `1` forces MiniLM, `0` forces keywords |

Module constants without environment overrides: `FRAMES_PER_SESSION = 3`, `MAX_FRAME_AGE_S = 15.0`, `EXPORT_PREVIEW_CHARS = 6000`, the push-to-talk chunk threshold of 48,000 bytes, and the artifact cap of 10 per session.

### Interactions with other components

- `agent/coordination/state_machine.py` (`SessionState`): epoch, slots, in-flight calls, snapshots, idempotency; the coordinator never mutates these directly except through its methods.
- `agent/coordination/tool_router.py`: `execute_tool`, `is_state_modifying`; tool schemas are given to the planner.
- `agent/fast_path/` (classifier and templates) and `agent/slow_path/planner.py` for Tiers 1 to 3.
- `agent/llm_client.py` through the planner, and `agent/workers/` for `spawn_agent`.
- `agent/memory/` through the scratchpad and graph memory on each `SessionState`.
- `agent/multimodal/` and `agent/speech.py` for ASR, VAD, vision and TTS.
- `agent/server.py`: calls `post_event`, `get_next_action` (through the dispatcher), `set_tts`, `get_or_create_session`.

### Failure modes and guarantees

- **No stale completion.** A cancelled task returns silently; a result that still arrives is discarded by `complete_tool_call`; a plan finishing under an old epoch returns `[]`. All three must fail for a stale result to surface.
- **Loop survival.** `_process_events` logs and continues after an exception; `_spawn_turn` logs exceptions from turn tasks; eviction logs and continues.
- **Ordering.** Classification is inline so epoch bumps follow arrival order of turn tasks; voice finals are chained so utterances reach the planner in order.
- **Honest completion.** Unreadable tool payloads become errors, and the reply is produced from the real result (`summarize_tool_result`), never from a template that claims success.
- **Interrupt when the model is slow.** If an interrupt arrives while the LLM is still thinking, no tool call exists to cancel; the plan is discarded afterwards. `_handle_interrupt` still acknowledges because `_active_plans` is positive.
- **Single process, in memory.** Sessions, queues and traces are lost on restart; there is no persistence or multi-process sharing. Client slowness is handled in the server dispatcher (a full inbox drops the action with a warning), not in the coordinator.
- **Unused field.** `SessionState._lock` is created but not used by the coordinator path I read; consistency instead relies on the single-threaded event loop and on inline classification.

### Tests that cover it

| Test file | What it checks |
|---|---|
| `tests/test_coordinator_flow.py` | Mid-call interrupt: one `ToolCancelAction` at epoch 2, task really cancelled |
| `tests/test_adversarial_timing.py` | Rapid-fire coalescing (one filler, epoch <= 2), fault-injected latency and cancellation |
| `tests/test_session_eviction.py` | Eviction rules, in-flight/planning/voice sessions spared |
| `tests/test_voice_stream.py` | Streaming activity, partials, barge-in bumping the epoch once, non-interrupting speech |
| `tests/test_voice_websocket.py` | Streaming PCM over the WebSocket, disconnect frees the voice runtime |
| `tests/test_latency_levers.py` | Soft deadline, endpoint hints, tail-partial reuse in `_voice_final` |
| `tests/test_tool_replies.py`, `tests/test_export.py`, `tests/test_vision.py` | Reply composition, export and vision tool branches |
| `agent/eval` scenarios (`agent/eval/scenarios.py`, run by `tests/test_eval_suite.py`) | End-to-end behaviour in virtual time with a mock LLM |

## 4. End-to-end trace: a correction mid-task

This traces the scenario `correction_mid_booking` from `agent/eval/scenarios.py`, which is the "book Delhi... no, Mumbai" case with the destinations swapped: the user asks for a flight from Delhi to Mumbai, then 0.8 s later says "No wait, change it to Goa". The mock LLM returns `book_flight(Delhi, Mumbai)` for the first turn and `book_flight(Delhi, Goa)` for the second; the booking tool takes 2.5 s. The same code path handles "Book Delhi" then "no, Mumbai". Defaults are used: debounce on with a 0.10 s window, epoch starting at 1.

**t = 0.00 s, first message.**

1. The server calls `coordinator.post_event(UserTextEvent(text="Book a flight from Delhi to Mumbai"))`. The event is traced, counted, the session is touched, speech (none) is stopped and `_reaction[sid] = [t0, False, False]`.
2. `_process_events` takes it; `_handle_event` creates `SessionState` (epoch 1) and sets the log context. The event is not immediate, so `_queue_debounced_user_text` stores it and starts a 0.10 s timer.
3. t = 0.10 s: the timer fires and `_spawn_turn(_handle_user_text(session, event))` creates a turn task.
4. `_handle_user_text`: scratchpad chunk appended; `has_work` is False; `_classify` says continue ("book a flight..." has no retraction cues). `log_classification` writes a `classification` record.
5. Tier 2: no intent or slots yet, so `generate_filler` returns the generic filler ("Looking that up right now..."). `emit_action(FillerAction, epoch=1)` goes through the trace logger and onto the action queue; `agent_first_ack_seconds` is observed (about 0.10 s here, because the clock started at `post_event`).
6. Tier 3: `_active_plans[sid]` becomes 1; `planner.plan(..., plan_epoch=1)` calls the LLM. The planner validates the call, stages the slots (`origin=Delhi`, `destination=Mumbai`) with an undo record, and registers the call through `session.register_tool_call`. `book_flight` is state-modifying, so an idempotency key is computed from intent, slots, epoch, tool and canonical arguments and registered. The plan is `[ToolCallAction(call_x, epoch 1), StateSnapshotAction]`.
7. `_dispatch_actions` records `_call_origin[call_x]`, emits the `ToolCallAction`, creates the `_execute_tool_task` task for `book_flight` and attaches it to the in-flight call; then it emits the snapshot (slots show Mumbai, `in_flight_calls` lists `call_x`). `turn_has_tool_call` is True, so the turn is not committed yet. The tool task is sleeping inside the handler (2.5 s).

**t = 0.80 s, the correction.**

8. `post_event(UserTextEvent("No wait, change it to Goa"))` again resets `_reaction` (new t0) and goes through the same debounce path; at t = 0.90 s a second turn task starts `_handle_user_text`.
9. `has_work` is True (call_x is running). `pending_clarification` is None. `_classify` returns an interrupt: the keyword fallback matches `no` and `wait`, and in embedding mode the lexical features (opening "no"/"wait" and the repair words "change") feed the calibrated model, so the text scores above the high threshold and the decision is `act`. `needs_clarification` is False. `is_interrupt` is True.
10. `session.bump_epoch(reason="user_correction: No wait, change it to Goa")` runs (see `02_coordination_core.md`): the epoch becomes 2; call_x (epoch 1, still running) has its asyncio task cancelled, status set to `cancelled`, its slot changes reverted (Mumbai is removed from the snapshot slots), and the scratchpad's `mark_aborted` discards the entity candidates that came from that call. It returns one `ToolCancelAction(call_x, epoch=2, reason="user_correction: ...")`.
11. The coordinator emits that cancel. In `emit_action` the trace logger checks that call_x is a known call id and the epoch is not lower than before; `agent_interrupt_cancel_seconds` is observed (about 0.10 s: the debounce wait plus classification).
12. Meanwhile the cancelled tool task receives `CancelledError` inside `execute_tool`, logs "Tool task ... cancelled" and returns. It never posts a `ToolResultEvent`, so the booking handler's 2.5 s completion is never reported. The environment's execution record marks the call cancelled and the scorer verifies that no stale booking completed.
13. Tier 2: `generate_filler(is_interruption=True)` emits `FillerAction("Got it, changing that.", epoch=2)`. If the speaker is enabled this is also spoken, after the earlier speech was stopped at step 8.
14. Tier 3: `planner.plan(..., plan_epoch=2)` calls the LLM again. The call returns `book_flight(Delhi, Goa)`. The planner stages `destination=Goa`, registers the call with a new id (epoch 2, so a different idempotency key) and returns `[ToolCallAction(call_y, epoch 2), StateSnapshotAction]`. The epoch still equals `plan_epoch`, so the plan is accepted.
15. `_dispatch_actions` emits the call and the snapshot (slots `origin=Delhi`, `destination=Goa`, `in_flight_calls` lists call_y only) and starts the new tool task.

What if the LLM were slow during step 14 and the user interrupted again? The second `bump_epoch` would change `session.epoch`; when `plan` returned, the planner would see `session.epoch != plan_epoch` and return `[]`, and `_handle_user_text` would emit nothing and commit nothing. Nothing stale is dispatched.

**t = 0.90 + 2.5 s, completion.**

16. The new tool task finishes and `_execute_tool_task` posts `ToolResultEvent(call_y, epoch=2, result={booking_id, status: confirmed, ...})` through `post_event`.
17. `_handle_tool_result`: `complete_tool_call` finds call_y with status running and epoch equal to the session epoch, so it returns True and marks it completed (releasing its slot undo record and completing its idempotency entry). The origin text is popped.
18. A reply is composed with `summarize_tool_result("book_flight", result)`, emitted as `SpokenResponseAction`, followed by a snapshot with no in-flight calls. `_commit_turn_and_emit_graph` commits the scratchpad turn (the turn's entities now reflect Goa, Mumbai having been discarded as aborted), inserts it into the graph and emits a `GraphUpdateAction`.

Final action stream as the client sees it (epochs in brackets):

```
t=0.10  filler[1]  tool_call[1] call_x  state_snapshot[1]
t=0.90  tool_cancel[2] call_x
        filler[2] "Got it, changing that."
        tool_call[2] call_y  state_snapshot[2]
t=3.40  spoken_response[2]  state_snapshot[2]  graph_update[2]
```

The cancel is emitted before the filler because `_handle_user_text` bumps the epoch (step 6) before it generates the Tier 2 acknowledgement (step 7). The trace logger would reject any regression, such as an epoch-1 action emitted after an epoch-2 one for the same session.

A race worth naming: if the cancelled booking had already completed in the instant before step 10, its `ToolResultEvent` would be sitting in the event queue. `_handle_tool_result` would call `complete_tool_call`, see status `cancelled` and drop it. This is the case that the status check, not the task cancel, protects.

## 5. Failure behaviours across the flow

| Situation | What happens | Where |
|---|---|---|
| Interrupt while a tool runs | Task cancelled, cancel action emitted, slots reverted | `bump_epoch`, `_handle_user_text`, `_handle_interrupt` |
| Interrupt while the LLM is thinking | Plan returns `[]`, nothing dispatched; interrupt is acknowledged | `Planner.plan`, `_active_plans` |
| Result arrives after cancellation | Discarded with a log line | `_handle_tool_result` |
| Ambiguous correction ("hmm, maybe") while work runs | Yes/no clarification, no epoch change | `_handle_user_text` step 5 |
| Tool raises | `error` string, reply from `summarize_tool_error` | `_execute_tool_task` |
| Tool returns a non-dict | Treated as an error | `_execute_tool_task` |
| Classifier model still loading | Keyword fallback, load continues in the background | `_classify` |
| LLM slow | "Still working on that -- one moment." at the soft deadline, only if still current | `_slow_notice` |
| Client slow | Action dropped in the server dispatcher with a warning | `agent/server.py` |
| Trace record invalid | Strict: raises; non-strict: dropped and counted | `agent/trace_logger.py` |

## 6. `agent/main.py`: entrypoint and factory

### Purpose

`agent/main.py` is the minimal wiring used outside the server: a factory and a CLI loop for manual testing.

### How it works

`create_agent(log_file="trace.jsonl")` builds a `ToolRouter`, a `TraceLogger(log_file=...)` and returns `AgentCoordinator(tool_router=..., trace_logger=...)`. It is re-exported from `agent/__init__.py`. `main()` starts the coordinator, then reads stdin lines on a thread (`asyncio.to_thread(sys.stdin.readline)`). `quit` exits; `interrupt` posts an `InterruptSignalEvent(reason="manual_interrupt")`; anything else posts a `UserTextEvent` for the session `interactive_session`. After each line it waits 50 ms and prints every action currently in the action queue as `[ACTION] TYPE: json`. Run it with `python -m agent.main`.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `create_agent` | `agent/main.py` | Factory with a default trace file |
| `main` | `agent/main.py` | Interactive CLI loop |

### Configuration

The only knob is the `log_file` argument (default `trace.jsonl`, strict validation because `TraceLogger` defaults to `strict=True`). It is separate from the server's `TRACE_LOG_PATH`.

### Interactions and limits

The CLI drains the queue only 50 ms after each input, so output from slow tools (a 2.5 s booking) appears under a later prompt, not the one that caused it. The server does not use `create_agent`; it builds its own coordinator in `agent/server.py`. I found no test that exercises `main()` itself.

### Tests that cover it

`tests/test_imports.py` imports the module. No dedicated test was found.

## 7. Trace logger (`agent/trace_logger.py`)

### Purpose

The evaluation harness scores the system strictly from trace logs, so the logger is also a self-check: it refuses to record (or emit) behaviour that breaks the system's invariants. It makes interruption correctness auditable after the fact: every event in, every action out, and every classifier decision with its scores.

### How it works

`TraceLogger` keeps an in-memory list `trace_history` (bounded to `max_history`, default 5000, oldest dropped) and optionally appends JSON lines to `log_file`. It has three entry points:

1. `log_event(event)` builds a record with `record_type="event"` from the event's `model_dump` (minus the free-form payload) and validates the schema.
2. `log_action(action)` first runs `_validate_invariants(action)`, then builds an `action` record and validates the schema.
3. `log_classification(session_id, text, result, timestamp)` records a Tier 1 decision with its raw scores and features, so a threshold can be audited later.

Every record has a `schema_version` (`TRACE_SCHEMA_VERSION = 1`), a `record_type` (`event`, `action` or `classification`), the session id, a timestamp and a payload. It is validated against `TRACE_RECORD_SCHEMA` with `jsonschema.Draft202012Validator`; `additionalProperties` is false, so stray top-level keys fail.

Raw media never enters the trace: `_redact_bytes` replaces bytes values with a marker like `<1234 bytes>` and replaces the `audio_b64` field with the decoded byte count, keeping records small and JSON-serializable.

The invariants enforced by `_validate_invariants`:

1. Epochs must be non-decreasing per session: an action with a lower epoch than the last one for that session raises `TraceValidationError`.
2. A `ToolCallAction` must have a non-empty `call_id`; it is remembered for the session.
3. A `ToolCancelAction` must reference a call id previously seen as a `ToolCallAction` in the same session.

Persistence: with a `log_file`, `_append_record` opens the file in append mode and writes one JSON line per record. `_rotate_if_needed` renames the file to `<path>.1` once it exceeds `max_file_bytes` (default 50 MB), keeping one prior file.

The `strict` flag decides what a schema failure does. Strict (the class default, used by tests, the evaluation harness, the CLI) raises `TraceValidationError`. Non-strict drops the record, increments `dropped_count` and logs a warning. The server constructs its logger with `strict=os.getenv("TRACE_STRICT", "0") == "1"`, so live sessions are non-strict by default (availability over strictness); `dropped_count` is surfaced as the gauge `agent_trace_dropped_records` and as `trace_dropped_records` in `/health`. Note that this flag only covers the schema check: the invariant checks in `_validate_invariants` always raise.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `TraceLogger` | `agent/trace_logger.py` | Records, validates and persists the trace |
| `TraceValidationError` | `agent/trace_logger.py` | Raised for an invariant or (strict) schema failure |
| `TRACE_RECORD_SCHEMA`, `TRACE_SCHEMA_VERSION` | `agent/trace_logger.py` | Versioned JSON schema of every record |
| `_validate_invariants` | `agent/trace_logger.py` | Epoch monotonicity, call-id presence, cancel references a known call |
| `_redact_bytes` | `agent/trace_logger.py` | Replace media with size markers |
| `_rotate_if_needed` | `agent/trace_logger.py` | One-generation rotation at 50 MB |

### Configuration

| Setting | Default | Meaning |
|---|---|---|
| `TRACE_LOG_PATH` | unset | JSON-lines file for the server (in-memory only if unset) |
| `TRACE_STRICT` | `0` | `1` raises on a malformed record in the server |
| `max_history` (constructor) | `5000` | Records kept in memory |
| `max_file_bytes` (constructor) | `50 MiB` | Rotation size |
| `strict` (constructor) | `True` | Raise versus drop |

### Interactions with other components

The coordinator calls `log_event` in `post_event`, `log_action` in `emit_action` and `log_classification` in `_handle_user_text`. The evaluation scorer (`agent/eval/`) reads `trace_history` to detect stale completions, duplicate state changes and trace violations. The server reads `dropped_count` for metrics and `/health`.

### Failure modes and guarantees

- A violating action raises inside `emit_action`, before the action is queued. Under strict mode this surfaces in the task that emitted it (a turn task logs it); in the server's non-strict mode, schema failures are dropped but invariant violations still raise.
- Invariant 3 only checks that the call id is known. The module docstring also lists "epoch >= call epoch" for cancels, but the code does not compare against the call's own epoch (it only has the non-decreasing check on the session epoch). This is a small doc/code mismatch rather than a functional gap, since `bump_epoch` always emits cancels at the new epoch.
- The per-session dictionaries `_session_epochs` and `_registered_call_ids` are never cleared; session eviction in the coordinator does not notify the trace logger, so a very long-running server accumulates one small entry per session id ever seen. Memory growth is slow but unbounded.
- File writes are synchronous in the event loop thread and the file is opened per record; acceptable at demo volumes, a throughput limit otherwise.
- Redaction applies only to top-level fields of the dump (`bytes` values and `audio_b64`); nested bytes would fail JSON serialization when writing to a file.

### Worked example

For the trace in section 4, the log contains an `event` record for each `UserTextEvent` and the `ToolResultEvent`, a `classification` record for each user text (the second shows a high `confidence_score`, `decision: "act"`), and `action` records for the filler, tool call, snapshot, cancel and so on. If a bug emitted `ToolCancelAction(call_id="call_zzz")` for a call never dispatched, `_validate_invariants` would raise `TraceValidationError("Tool cancellation for unknown call_id ...")` before the cancel reached the client.

### Tests that cover it

`tests/test_trace_logger.py`: valid flow, epoch regression caught, cancel for unknown call caught, malformed schema rejected in strict mode, malformed schema dropped and counted in non-strict mode. `tests/test_eval_scorer.py` and the eval suite consume traces.

## 8. Metrics (`agent/metrics.py`)

### Purpose

A dependency-free Prometheus text-format exporter, focused on what an operator needs to judge this system: how fast the agent reacts, how the LLM provider behaves and what is being turned away.

### How it works

The module defines `Counter`, `Gauge` and `Histogram` on a small `_Metric` base (thread-safe through a lock per metric, labels as a tuple of strings), and a `Registry` that renders all registered metrics into text exposition format. A module-level `REGISTRY` holds the metric instances, which code anywhere in the process updates directly (`metrics.EVENTS.inc(type=...)`). `Histogram.observe` increments every bucket whose bound is at least the value (cumulative buckets), the sum and the count; rendering adds the `+Inf` bucket. Latency buckets (`LATENCY_BUCKETS`) run from 10 ms to 16 s. The server renders it at `GET /metrics` (bearer-protected when `AUTH_TOKEN` is set); just before rendering it sets `agent_sessions` and `agent_trace_dropped_records` as gauges "at scrape time".

The registered metrics:

| Metric | Type | Meaning |
|---|---|---|
| `agent_ws_connections` | gauge | Open WebSocket connections |
| `agent_sessions` | gauge | Sessions held in memory (set at scrape) |
| `agent_events_total{type}` | counter | Inbound events, labelled by event type |
| `agent_actions_total{type}` | counter | Outbound actions by type |
| `agent_first_ack_seconds` | histogram | Text or interrupt received to first filler emitted |
| `agent_interrupt_cancel_seconds` | histogram | Interrupting event received to first cancellation |
| `agent_speech_stop_seconds` | histogram | Interrupt noticed to the agent's voice stopped (observed in `agent/speech.py`) |
| `agent_llm_requests_total{outcome}` | counter | ok, timeout, error, rate_limited, fallback, circuit_open |
| `agent_llm_latency_seconds` | histogram | Latency of successful LLM requests |
| `agent_llm_circuit_open` | gauge | 1 while the circuit breaker is open |
| `agent_llm_soft_deadline_total` | counter | Requests that outlived the soft deadline |
| `agent_rejected_total{reason}` | counter | origin, auth, session_id, ip_limit, session_limit, rate_limit, too_large |
| `agent_trace_dropped_records` | gauge | Trace records dropped for failing validation |

The two headline reaction metrics are fed by `emit_action` using the `_reaction` record described in section 3: the first `FillerAction` after a user text or interrupt stops the first-ack timer, and the first `ToolCancelAction` stops the cancel timer. Both use `clock.monotonic()`, so they are measurable in virtual time. A voice barge-in resets the record with the acknowledgement already marked, so only the cancel latency is measured for it.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `Counter`, `Gauge`, `Histogram` | `agent/metrics.py` | Metric primitives with label support |
| `Registry`, `REGISTRY` | `agent/metrics.py` | Collects and renders all metrics |
| module-level metrics (`EVENTS`, `ACTIONS`, `FIRST_ACK`, `CANCEL_LATENCY`, ...) | `agent/metrics.py` | The instances other modules update |
| `LATENCY_BUCKETS` | `agent/metrics.py` | Shared histogram bounds |

### Configuration

No environment variables inside the module. Exposure is controlled by the server (`/metrics` path, the `metrics_enabled` setting in `agent/settings.py` and `AUTH_TOKEN`).

### Interactions

The coordinator updates events, actions, first-ack and cancel latency; `agent/llm_client.py` updates the `agent_llm_*` metrics; `agent/speech.py` updates speech-stop latency; `agent/server.py` updates connection, rejection and scrape-time gauges.

### Failure modes and guarantees

- Metrics are process-local and reset on restart; there is no multi-process aggregation.
- Counter and gauge increments take a lock; the read helpers (`value`, `count`) do not, which is fine for tests and approximate reading.
- Label values are escaped for backslash and quote when rendered.
- Because the reaction timers measure from `post_event`, the typed-text first-ack figure includes the debounce window (default 100 ms).

### Worked example

In the trace of section 4, scraping `/metrics` afterwards would show `agent_events_total{type="user_text"} 2`, `agent_events_total{type="tool_result"} 1`, `agent_actions_total{type="tool_cancel"} 1` and a count of 1 in `agent_interrupt_cancel_seconds` (the first cancel after the correction posted at t = 0.80 s).

### Tests that cover it

`tests/test_hardening.py` (the `/metrics` auth behaviour and `agent_rejected_total`), `tests/test_circuit_breaker_recovery.py` and `tests/test_rate_limit_retry.py` for the LLM metrics. I found no dedicated unit test of `Histogram` rendering.

## 9. Structured logging (`agent/logging_setup.py`)

### Purpose

Make a log line attributable: which session, which epoch and which request was being handled, without passing those through every function call. With concurrent sessions and many tasks per session, an epoch-tagged log is what lets an operator reconstruct why a result was discarded.

### How it works

Three `contextvars.ContextVar`s, `request_id_var`, `session_id_var` and `epoch_var`, hold the context. `ContextFilter` copies them onto every `LogRecord`. Because asyncio tasks copy the current context when they are created, tasks spawned from `_handle_event` (turn tasks, tool tasks, voice tasks) inherit the session id and epoch that were set in `_handle_event`; the server sets the request id for HTTP requests.

Two formatters render the record. `JsonFormatter` writes one JSON object per line with `ts` (UTC, millisecond), `level`, `logger`, `msg`, the three context fields when set, any `extra={...}` keys and an `exc` field for exceptions. `TextFormatter` appends `[sid=... epoch=... req=...]` to a human line. `setup_logging(fmt, level)` is idempotent: it removes any previous handler that it installed (tagged `_agent_handler`), installs a stderr handler with the filter and chosen formatter and sets the root level.

One subtlety: the epoch var is set when an event is handled, so a later log line in a long-running task shows the epoch at the time that task was created, not the current epoch. A log from a task that outlives an epoch bump therefore carries the older epoch, which is in fact useful when diagnosing stale work.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `request_id_var`, `session_id_var`, `epoch_var` | `agent/logging_setup.py` | Context carriers |
| `ContextFilter` | `agent/logging_setup.py` | Attach context to each record |
| `JsonFormatter`, `TextFormatter` | `agent/logging_setup.py` | JSON-lines and readable formats |
| `setup_logging` | `agent/logging_setup.py` | Idempotent root handler installation |

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LOG_FORMAT` | `text` | `json` for log shippers |
| `LOG_LEVEL` | `INFO` | Root level |

Both are read by `agent/settings.py` and passed to `setup_logging` in the server's lifespan hook.

### Interactions

Used by the coordinator (`_handle_event` sets the vars) and the server (startup and per-request id). Nothing else needs to know about it.

### Failure modes and guarantees

Calling `setup_logging` twice never duplicates lines. An unknown `LOG_LEVEL` falls back to `INFO`. Anything in a log call's `extra` that collides with a standard record attribute is ignored by the JSON formatter. Logging is synchronous to stderr.

### Tests that cover it

`tests/test_hardening.py` imports `JsonFormatter`, `TextFormatter`, `epoch_var`, `session_id_var` and `setup_logging` and tests the formats and the context fields.

## 10. Warm-up (`agent/warmup.py`)

### Purpose

The first user turn should not pay cold-start costs (importing MiniLM, loading the VAD and Whisper models, opening the TLS connection to the LLM provider). The module pre-loads each runtime model through the coordinator's own instances, so the thing that was warmed is the thing used at runtime.

### How it works

`run_full_warmup(coordinator, include_llm=True)` runs isolated, timed stages and returns a report:

1. `classifier`: `coordinator.intent_classifier.classify_text("warmup text ping")` in a thread (loads MiniLM if enabled);
2. `vad`: builds a `SileroStreamingVAD` and runs it on 512 zero samples;
3. `asr`: `coordinator.asr_processor.warmup`;
4. `llm` (only if `include_llm`): one tiny "ping" request through `coordinator.planner.client.backend.generate`, which opens the connection and primes the provider;
5. `vision`: `get_vision_backend()` as a configuration check (the hosted backend has nothing to preload);
6. `tts` (only if a TTS backend is present): `synthesize("Ready.")`.

Each stage is wrapped so that an exception is recorded as `{"ok": False, "error": "..."}` (truncated to 160 characters) and the next stage still runs. The report holds per-stage `ok` and `seconds`, plus `total_seconds` and `all_ok`, and is stored as `coordinator.warmup_report`, which `/health` returns under `warmup`.

In `agent/server.py`, the lifespan hook starts `run_full_warmup` as a background task at boot (so startup is not delayed) with `include_llm` true only when `WARMUP_LLM=1`, because the LLM ping spends provider quota. `POST /warmup` runs the full set including the LLM on demand; it is bearer-protected and rate limited by `warmup_min_interval_s` (default 30 s, `WARMUP_MIN_INTERVAL_S`).

`agent/warmup.py` also has `run_warmup_hook`, an earlier function that warms the classifier, templates, LLM, ASR and (optionally) vision with logging that refers to a "300s warm-up phase" of the original competition setup. I found no caller of it in the repository other than its own definition; the server and tests use `run_full_warmup`. Treat `run_warmup_hook` as legacy.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `run_full_warmup` | `agent/warmup.py` | Warm all runtime models, report timings |
| `run_warmup_hook` | `agent/warmup.py` | Older entry point; unused in the repository |

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `WARMUP_LLM` | unset | `1` includes the LLM ping in the boot warm-up |
| `WARMUP_MIN_INTERVAL_S` | `30` | Minimum seconds between `POST /warmup` calls |

### Interactions

Reads the coordinator's `intent_classifier`, `asr_processor`, `planner.client.backend` and `tts_backend`; writes `warmup_report`; consumed by `/health` and `/warmup`.

### Failure modes and guarantees

A failing stage never stops the others. The boot warm-up task is cancelled on shutdown. While the classifier is still loading, `_classify` already answers with the keyword heuristic, so a turn that arrives mid-warm-up does not wait. Warm-up is best effort: a failed stage simply means the corresponding first call is slow.

### Worked example

A server started with `WARMUP_LLM=0` ends with a report such as `classifier ok`, `vad ok`, `asr ok`, `llm` absent, `vision ok` and the totals; `GET /health` then shows the report. `POST /warmup` immediately after returns the same stages plus `llm`, and a second `POST /warmup` within 30 s returns the rate-limit error with `retry_after_s`.

### Tests that cover it

`tests/test_full_warmup.py`: every stage reported and the used instances warmed, one failing stage does not stop others, and the first call after warm-up is at steady state.

## 11. Summary of design choices

- **One serialized loop, many short tasks.** Routing is serialized for ordering; slow work is spawned. This makes the interrupt path latency independent of LLM or tool duration.
- **Epoch checks at three levels.** Task cancellation, result discard and plan discard each stop stale work independently, and the trace logger refuses epoch regressions at the output gate.
- **Everything outbound through one gate.** `emit_action` is the place for validation, metrics, artifact capture and speech, so those cannot be bypassed by a code path.
- **Observability as a first-class output.** Trace records (with classifier scores), reaction-latency histograms and epoch-tagged logs make "why did the agent do that" answerable from artefacts rather than from memory.
- **Known limitations.** Debounce plans only the last text of a burst and delays typed-text cancellation by the window; sessions and traces live in process memory; the trace logger's per-session dictionaries are not freed on eviction; `run_warmup_hook` and parts of the schema package exports are leftovers.
