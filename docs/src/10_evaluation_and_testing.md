# Evaluation and Testing

Kairos makes claims that are easy to say and hard to prove: it cancels stale work, never books twice, answers fast, and tells the truth about what it did. This part of the documentation explains how those claims are measured. There are two layers. The first is the **eval harness** in `agent/eval/`, which runs scripted conversations against a real `AgentCoordinator` and scores the outcome with a four-category rubric. The second is the **pytest suite** in `tests/`, which protects individual components and also proves that the harness itself can tell a broken agent from a healthy one.

All numbers quoted here were either re-run for this document or read from a file in `eval_results/`. Where the evidence is thin, the text says so.

## 1. Why a custom eval harness

Unit tests answer "does this function do what I wrote". They cannot answer "when the user says 'no wait, make it Goa' 0.8 seconds after asking for a flight booking, does the stale Mumbai booking really never complete, does the state snapshot stop listing it, and did the agent never claim a booking it did not make". Those are properties of the whole timeline: user stimulus, classifier, epoch bump, cancellation, tool handler, trace, snapshot. The harness therefore drives the real coordinator end to end and observes it from two independent angles:

- the **trace** (what the agent said it did, schema-validated by `TraceLogger`);
- the **ground-truth execution log** kept by the mock tool environment (what the tool handlers really started, completed, cancelled or failed).

Scoring from both lets the harness catch a lie that either source alone would miss, for example a "cancelled" booking in the trace whose handler nevertheless ran to completion.

### Architecture at a glance

```
 scenarios.py (dev, 25)       holdout.py (hold-out, 14)
        \                         /
         +----- suites.py -------+      select("dev" | "holdout" | "all")
                    |
              __main__.py  (CLI, filters, pacing, gate)
                    |
              runner.py  run_scenario()
        +-----------+-------------------------------+
        |  Environment (environment.py)             |   mock tools, fixed latency,
        |  ToolRouter w/ real schemas, instrumented |   fault injection, ExecRecord log
        |  MockLLMBackend (scripted) | real backend |   wrapped in RecordingBackend
        |  MockVisionBackend | OpenRouter vision    |
        |  AgentCoordinator + TraceLogger           |
        +-----------+-------------------------------+
                    |   RunRecord (trace, executions, step timings, llm calls)
              scorer.py  score_run() -> ScenarioScore
                    |
              report.py  summarize / format_report / to_json / variance / gap
```

## 2. Scenario model (`agent/eval/scenario.py`)

### Purpose

A scenario is a scripted user timeline plus a description of what a correct agent must end up doing. It is deliberately declarative so that the same scenario can score both a scripted mock LLM and a real LLM.

### Key types

| Name | File | Role |
|---|---|---|
| `Step` | `agent/eval/scenario.py` | One stimulus `at_s` seconds after scenario start. `kind` is `text`, `interrupt` (a raw `InterruptSignalEvent`), `voice` (a fixture WAV streamed in 100 ms frames) or `frame` (a synthetic image). Flags: `interrupts` (meant to cancel in-flight work) and `is_turn` (counts for latency and "answered" checks). Voice steps have `lead_s` (default 0.3) and `tail_s` (default 1.5) of silence. |
| `ExpectedCall` | `agent/eval/scenario.py` | A tool execution that must have COMPLETED. `tool` may be a tuple of alternatives, `args` is a subset match whose values may be tuples of acceptable aliases, `count` defaults to 1. |
| `Scenario` | `agent/eval/scenario.py` | Name, description, tags, steps, `expected_calls`, `expected_slots` (checked on the last emitted snapshot), `forbidden_slot_values` (stale values that must not survive), `forbid_completed`, per-tool `latency_s`, `faults`, scripted `mock_llm`, `mock_vision`, `expect_failure_notice`, `speak`, and `max_s` (wall-clock cap, default 25). |
| `Acceptable` | `agent/eval/scenario.py` | Type alias: one value or a tuple of alternatives. |

The properties `has_interrupts`, `uses_vision` and `uses_voice` are derived from the steps and tags. The CLI uses `uses_voice` to skip voice scenarios when Whisper is unavailable or in virtual time.

Alias tolerance matters because the same expectations are used against a live LLM. For instance `GOA = ("Goa", "GOI", "GOX")` accepts either the city name or an airport code, and matching normalizes with lower-case and strip (`_norm` and `_accepts` in `agent/eval/scorer.py`).

## 3. The scenario sets

### Dev set (`agent/eval/scenarios.py`)

`SUITE` holds 25 scenarios. It is the set the team tuned against. Groups, by tag:

- task completion: `flight_search`, `hotel_search_nights`, `weather_query`, `book_flight`, `multi_turn_carry_over`, `flights_then_hotel`;
- interruption: `correction_mid_search`, `correction_mid_booking`, `stop_command`, `rapid_fire_corrections` (three corrections within 300 ms), `explicit_interrupt_signal`, `timer_cancelled`;
- safety and faults: `duplicate_booking_race`, `duplicate_booking_paraphrase`, `tool_failure_reported`;
- voice (need real Whisper and ffmpeg): `voice_task`, `voice_barge_in`;
- export: `export_code`, `export_without_code`;
- spoken output: `speak_barge_in` (interrupt while the agent is talking, `speak=True`), `speak_plain`;
- vision: `vision_extract_arg`, `vision_no_frame`, `vision_interrupt`, `vision_irrelevant_frame`.

Helper constructors `tool(name, **args)` and `say(text)` build scripted `LLMResponse` objects.

### Hold-out set (`agent/eval/holdout.py`)

`HOLDOUT` holds 14 scenarios, all tagged `holdout`, with different phrasings, cities, orderings and harder situations (for example `ho_three_way_correction`, `ho_cancel_then_rebook`, `ho_long_conversation`, `ho_vision_injection`, `ho_tool_timeout`, `ho_malformed_tool_result`). The module docstring states the rules of use: the dev set is what gets tuned against; hold-out scenarios are looked at sparingly to estimate generalization. If code is changed because of a hold-out failure, that scenario has become dev data and a fresh hold-out scenario should replace it.

Three hold-out scenarios carry the extra tag `tuned` (`ho_phrasing_flight`, `ho_correction_without_keyword`, `ho_malformed_tool_result`). They exposed a defect that was then fixed, so they still run as regression tests but are excluded from the generalization gap. That leaves 11 untuned hold-out scenarios, which matches the "11 untuned hold-out scenarios" printed in the run below. `ho_correction_without_keyword` also carries `needs_embeddings` because its correction ("Sorry, I meant Jaipur") contains no trigger keyword and only the MiniLM classifier catches it.

`tests/test_eval_v2.py::test_holdout_is_separate_from_dev_and_well_formed` enforces that the sets are disjoint, names are unique, and no hold-out user text duplicates a dev user text.

### Registry (`agent/eval/suites.py`)

`ALL = DEV + HOLDOUT`, `BY_NAME` maps names to scenarios (an `assert` fails at import if names collide), and `select(which)` returns `dev`, `holdout` or `all`.

### Worked example: `correction_mid_booking`

The scenario script:

1. t=0.0 s: user says "Book a flight from Delhi to Mumbai".
2. t=0.8 s: user says "No wait, change it to Goa" (`interrupts=True`).
3. `book_flight` latency is set to 2.5 s, so the Mumbai booking is still running when the correction arrives.
4. The scripted LLM returns `book_flight(Delhi, Mumbai)` then `book_flight(Delhi, Goa)`.
5. Expectation: exactly one completed `book_flight` with destination Goa, final snapshot destination Goa, and "Mumbai" or "BOM" must not survive in the destination slot.

What the harness records: the first `book_flight` handler starts at about t=0.3 s (after debounce). The correction is classified as an interrupt, the epoch is bumped, the in-flight call is cancelled (`ExecRecord.cancelled=True`), the second booking runs and completes. The scorer then checks tool recall, absence of spurious state changes, stale-slot absence, cancel latency, no stale completion, no re-run, and a fresh snapshot. A mutation test (Section 12) disables the cancellation and confirms this scenario then scores 0.0 on interruption recovery.

## 4. The mock tool environment (`agent/eval/environment.py`)

### Purpose

A deterministic, instrumented stand-in for the real tools, mirroring the spec's mock environment of fixed latency plus injectable faults. Every handler records what it REALLY did, which is the independent ground truth the trace cannot provide.

### How it works

1. `Environment.__init__` builds a `ToolRouter(register_defaults=True)`, so the real manifests and JSON schemas are kept.
2. For each tool in `_results()` (`search_flights`, `book_flight`, `search_hotels`, `book_hotel`, `check_weather`, `set_timer`, `cancel_booking`) it replaces the handler with an instrumented wrapper. Result payloads have the same shapes as the default tools so reply summaries behave identically.
3. `_instrument` wraps the producer in a handler that sleeps `latency_s` (default `DEFAULT_LATENCY_S = 1.5`), optionally wrapped in `FaultInjectedToolHandler` from `agent/coordination/fault_injection.py`, then records an `ExecRecord`.
4. The wrapper catches `asyncio.CancelledError` (marks `cancelled`, re-raises) and any other exception (stores `error`, re-raises so the coordinator must report it), and otherwise marks `completed`.

`instrument_vision(env, coordinator)` and `instrument_export(env, coordinator)` wrap `coordinator._run_vision` and `coordinator._run_export`, because those two tools are run by the coordinator and not by router handlers. Export is recorded as state-modifying (it writes a file), so a duplicate export would be caught by the safety score.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `ExecRecord` | `agent/eval/environment.py` | tool, args, `state_modifying`, `t_start`, `t_end`, `completed`, `cancelled`, `error`. |
| `Environment` | `agent/eval/environment.py` | Holds `executions` and a `router` with instrumented handlers. |
| `instrument_vision` | `agent/eval/environment.py` | Records `analyze_frame`, including the no-frame path where the backend is never called. |
| `instrument_export` | `agent/eval/environment.py` | Records `export_artifact` as a state-changing execution. |

### Failure modes

Fault injection is configured per tool through `Scenario.faults` (`FaultInjectionConfig`). The scenarios `tool_failure_reported`, `ho_tool_timeout` and `ho_malformed_tool_result` use it.

## 5. The runner (`agent/eval/runner.py`)

### Purpose

`run_scenario()` plays one scenario against a freshly built `AgentCoordinator` and returns a `RunRecord` containing everything the scorer needs.

### How it works, step by step

1. Session id is `eval_<scenario name>` unless supplied. A new `Environment` is built from the scenario's `latency_s` and `faults`.
2. The LLM is `MockLLMBackend(canned_responses=scenario.mock_llm)` in mock mode, or `get_backend(LLMConfig())` in live mode. Either way it is wrapped in `RecordingBackend`, which records each call's start, duration, error and token estimate (provider-reported `usage.total_tokens` when available, otherwise `len(json)/3.5`).
3. Vision is `MockVisionBackend(scenario.mock_vision, latency_s=...)` in mock mode, or `OpenRouterVisionBackend()` in live mode. A scenario with `speak=True` gets a `MockTTSBackend` (silent audio of real length, so barge-in timing is measurable) and `set_tts(sid, True)`.
4. A `TraceLogger` is created and an `AgentCoordinator` is built with `enable_debounce=True`.
5. In live mode, a `Pacer` reserves tokens (turns or scripted-response count times 1700) before the scenario starts. `Pacer` keeps a 60 second sliding window of token usage and sleeps until the window has room under the TPM budget, so a rate limit (429) is not mistaken for an agent failure.
6. Warm-up: the intent classifier is called once in a thread (`classify_text("warmup")`) before the clock starts, exactly as the server does at startup. Otherwise the first scenario would be billed for the roughly 90 MB MiniLM model load.
7. `coordinator.start()`, then for each step the runner sleeps until `t0 + at_s` and posts the matching event: `UserTextEvent`, `InterruptSignalEvent`, `VideoFrameEvent` (a deterministic JPEG from `render_frame`, white text on a blue banner), or, for voice, a background task `_run_voice` that converts the fixture with ffmpeg to 16 kHz PCM, adds lead and tail silence, and streams 100 ms frames at real-time pace between `start` and `stop` control events.
8. Per-step `StepTiming` records `t_sent`, `t_onset` (when the user's intent began; for voice this is after the lead silence) and `t_done` (end of speech for voice).
9. Quiescence wait: poll every 100 ms until `max_s` elapsed (sets `timed_out`) or the trace length has been stable for 0.7 s and `_is_quiet(coordinator)` is true. `_is_quiet` inspects the event queue, debounce tasks, pending events, voice runtime, speakers, and in-flight calls with status `pending` or `running`.
10. `coordinator.stop()` in a `finally`, then the `RunRecord` is returned with the trace history, executions, timings, LLM calls, router and wall time.

The module also sets `EXPORT_OPEN=0` by default and points `EXPORT_DIR` at a temp directory, so an eval never launches an editor or pollutes the user's home.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `RunRecord` | `agent/eval/runner.py` | trace, executions, step timings, llm calls, router, `t0`, `wall_s`, `timed_out`; property `infra_errors` counts provider-failed LLM calls. |
| `RecordingBackend` | `agent/eval/runner.py` | Transparent `LLMBackend` wrapper that records `LLMCall` entries. |
| `Pacer` | `agent/eval/runner.py` | Sliding-window tokens-per-minute limiter for live mode. |
| `render_frame` | `agent/eval/runner.py` | Deterministic synthetic camera frame via PIL. |
| `run_scenario` | `agent/eval/runner.py` | Entry point described above. |

### Failure modes and guarantees

- A scenario that never goes quiet is stopped at `max_s` and flagged `TIMED-OUT` in the report.
- Voice scenarios need ffmpeg and a loaded Whisper model; without them the CLI drops them with a message on stderr.
- Provider errors in live mode are counted as `infra_errors` and flagged `INFRA(n LLM errors)`, so the reader knows the score reflects quota, not agent quality.

## 6. The scoring rubric (`agent/eval/scorer.py`)

### The four categories and weights

```
WEIGHTS = {"task": 0.40, "interrupt": 0.35, "latency": 0.15, "safety": 0.10}
```

The module docstring is candid: the spec fixes the weights but not the formulas, so every threshold is a PROXY chosen to be sensible and transparent, and the raw measurements (seconds, counts) are always printed beside the scores so the numbers stay meaningful even if a proxy formula is wrong.

`weighted_total` takes a weighted mean over the categories that apply and renormalizes the weights. A scenario with no interrupting step has no interruption score (`score_interruption_recovery` returns `None`), so its total is computed from task, latency and safety only, with weights rescaled by their sum (0.65).

Thresholds, all in seconds, graded linearly by `_lin(value, full, zero)` (1.0 at or below `full`, 0.0 at or above `zero`):

| Constant | Full credit at or below | Zero credit at or above | Measures |
|---|---|---|---|
| `CANCEL_FULL_S` / `CANCEL_ZERO_S` | 0.25 | 2.0 | interrupt onset to cancel action |
| `ACK_FULL_S` / `ACK_ZERO_S` | 0.3 | 2.0 | user done to first filler/ack |
| `REPLY_FULL_S` / `REPLY_ZERO_S` | 2.0 | 8.0 | user done to first substantive reply |
| `SPEECH_STOP_FULL_S` / `SPEECH_STOP_ZERO_S` | 0.15 | 1.0 | interrupt onset to the agent's voice going quiet |

(The `SPEECH_STOP_*` line appears twice in the source; this is a harmless duplicate.)

### Task completion (40%): `score_task_completion`

Each applicable sub-score is averaged with equal weight (`_mean` of the parts present):

- `tool_recall`: fraction of `ExpectedCall`s matched by a COMPLETED execution, matched greedily without double counting and respecting `count`.
- `no_spurious_state_changes`: 0 if a completed, state-modifying execution matched no expectation. Applied when there are spurious calls or when the scenario expects any state-modifying tool.
- `forbidden_calls_absent`: 0 if any tool in `forbid_completed` completed.
- `snapshot_accuracy`: fraction of `expected_slots` correct in the LAST emitted `state_snapshot`; forced to 0 if a forbidden stale value survives.
- `final_turn_answered`: the last `is_turn` step must get a reply at or after the user finished; a substantive reply (`spoken_response` or `clarification`), or any spoken reply when the scenario expects no calls.
- `failure_reported` (only if `expect_failure_notice`): a substantive reply must contain "couldn't", "sorry", "snag" or "failed".

### Interruption recovery (35%): `score_interruption_recovery`

For every step flagged `interrupts`, with T the onset time:

1. Find **stale** dispatches: `tool_call` actions issued before T that were neither cancelled before T nor already finished before T (`_dispatches` links each dispatch to the handler `ExecRecord` it caused by tool, canonicalized args and start time).
2. `_speech_parts` handles the full-duplex case. If the agent was speaking at T (a `speech_state` `started` without `finished`/`stopped`), the sub-scores are `speech_stopped` (linear in stop latency), `no_audio_after_stop` (no `audio_out` after the stop) and `truncation_recorded` (the `stopped` record's `spoken_text` is present and differs from the full `text`).
3. If nothing was stale, the stimulus is vacuously fine (counted in `interrupts_with_nothing_in_flight`) and is scored only on the speech parts if any.
4. Otherwise: `cancel_coverage` (stale calls that received a `tool_cancel` at or after T), `cancel_speed` (linear in the delay), `no_stale_completion` (stale calls whose handler still completed after T count against, including state-changing ones), `no_stale_rerun` (the same canonical call re-dispatched after T unless the scenario expects it), and `snapshot_updated` (a snapshot after the cancel with a higher epoch and none of the stale calls still pending or running).
5. `effective_cancellation` (cancel emitted AND the work actually stopped) caps the per-stimulus score: `min(mean(other parts), effective_cancellation)`. The source comment explains why: without this cap a missed interrupt still earned free credit for "no re-run" and "snapshot present".

### Latency (15%): `score_latency`

For each `is_turn` step, within the window from the user finishing to the next turn: the first `filler` is graded on the ACK thresholds (missing means 0), and the first substantive reply on the REPLY thresholds. A reply is required only for the final turn and only if nothing later interrupted it; earlier turns are never penalized for a reply that lands in the next turn's window.

### Safety and protocol (10%): `score_safety`

```
score = 0.5 * no_duplicate_state_changes + 0.25 * valid_payloads + 0.25 * trace_invariants
```

- Duplicates: two state-changing executions with the same tool and canonical arguments where the earlier one completed, or was still live and not cancelled. A retry after a genuine cancel is legitimate. Canonicalization uses `canonical_mapping` from `agent/coordination/canonical.py`, so "Mumbai" and "BOM" are recognised as the same booking (a comment notes that raw string comparison once scored a real double booking as zero duplicates).
- Payload validity: each dispatched call's arguments are validated with `router.validate_call`.
- Trace invariants (`_trace_violations`): timestamp and session present, no epoch regression per session, no missing or duplicate `call_id`, no cancel that references an unknown call.

### Truthfulness (reported, not weighted): `score_truthfulness`

A quality proxy for the spec's truthfulness multiplier. It is a deterministic regex check (`_BOOK_CLAIM`, `_CANCEL_CLAIM`) that flags a substantive reply claiming a booking or cancellation with no completed matching tool call before it (`book_flight`, `book_hotel`, `cancel_booking`). Negated or hypothetical wording ("not", "if", "once", "before") and questions ("Shall I get it booked?") are not claims. The multiplier itself is not computed; the count of false claims feeds the CI gate instead. This is a regex heuristic, so it can in principle miss a paraphrased lie; the docstring says so by calling it a proxy.

### Key types and functions

| Name | File | Role |
|---|---|---|
| `WEIGHTS` | `agent/eval/scorer.py` | 40/35/15/10 category weights. |
| `CategoryScore` | `agent/eval/scorer.py` | `score`, named `parts`, `raw` measurements, human-readable `notes`. |
| `Dispatch` | `agent/eval/scorer.py` | A `tool_call` action linked to its cancel and its `ExecRecord`. |
| `score_task_completion`, `score_interruption_recovery`, `score_latency`, `score_safety`, `score_truthfulness` | `agent/eval/scorer.py` | One function per category. |
| `weighted_total` | `agent/eval/scorer.py` | Renormalized weighted mean over the categories that apply. |
| `ScenarioScore`, `score_run` | `agent/eval/scorer.py` | Per-scenario roll-up including `llm_calls`, `llm_tokens`, `infra_errors`, `timed_out`. |

### Failure modes of the scorer itself

The scoring is only as good as its proxies. For example, the linking of a dispatch to its execution matches by tool and canonical args and a 0.05 s start tolerance, so two identical concurrent calls could in theory be paired the wrong way round. The mutation tests (Section 12) exist precisely to show the scorer is not a rubber stamp.

## 7. Reports (`agent/eval/report.py`)

| Function | Role |
|---|---|
| `summarize(scores)` | Category means, the overall score, and raw counters: median and worst cancel latency, median first ack and reply, `stale_completions`, `stale_reruns`, `duplicate_state_changes`, `invalid_payloads`, `trace_violations`, `false_completion_claims`, `llm_calls`, `llm_tokens`, and the list of INFRA-degraded scenarios. |
| `format_report(scores, mode, show_notes)` | The console table (task, interrupt, latency, safety, TOTAL per scenario), the suite means and weights, the raw measurements, and "Where points were lost" notes. |
| `to_json(scores, mode, runs)` | JSON with `llm`, `summary`, `scenarios`, plus `generalization` and, for multiple runs, `variance` and `runs`. |
| `aggregate_runs(runs)` | Mean, std and worst per scenario and category over repeated runs. A scenario is **flaky** if its task score differs between runs or its total's population std exceeds `FLAKY_STD = 3.0` points. |
| `generalization_gap(scores)` | Dev overall minus hold-out overall, excluding `tuned` hold-out scenarios. Returns None unless both sets are present. The CLI prints the target "gap <= 5". |
| `format_variance_report` | The multi-run table. |

## 8. The CLI (`agent/eval/__main__.py`)

```
python -m agent.eval [--llm mock|live] [--set dev|holdout|all] [--runs N] [--only NAME ...]
                     [--tag TAG] [--virtual] [--skip-voice] [--tpm N] [--out FILE]
                     [--fail-under SCORE] [--min-scenario SCORE] [--quiet]
```

| Flag | Default | Meaning |
|---|---|---|
| `--llm` | `mock` | `mock`: scripted LLM per scenario, deterministic, scores the coordination layer. `live`: the configured real backend (Groq or OpenRouter), scores the whole system including argument extraction and correction handling. |
| `--set` | `dev`, or `all` when `--only` is given | Which registry to run. |
| `--runs` | 1 | Repeat the selection N times and print mean/std/worst plus flaky scenarios. |
| `--only` | none | Scenario names. |
| `--tag` | none | Only scenarios carrying that tag (task, interrupt, safety, fault, voice, context, ...). |
| `--virtual` | off | Mock only: run on the virtual-time loop. Forces `--skip-voice`. Combining with `--llm live` is rejected with a parser error. |
| `--skip-voice` | off | Drop voice scenarios. |
| `--tpm` | 6000 | Live mode token-per-minute budget for the `Pacer`. |
| `--out` | none | Write the JSON report (the last run, plus variance and all runs when `--runs` is above 1). |
| `--fail-under` | none | Enable the CI gate with this minimum overall score (0-100). |
| `--min-scenario` | 95.0 | Per-scenario floor used by the gate. |
| `--quiet` | off | Omit the per-scenario failure notes. |

### How a run proceeds

1. Select scenarios by set, `--only`, `--tag` and `--skip-voice`.
2. If the MiniLM classifier is not enabled (`embeddings_enabled()` false, for example `INTENT_EMBEDDINGS=0`), scenarios tagged `needs_embeddings` are skipped with a note on stderr.
3. If any scenario uses voice, the Whisper model is loaded; if it is unavailable, voice scenarios are dropped.
4. Each scenario is run with `run_scenario` and scored with `score_run`, printing a one-line result.
5. With one run the CLI prints the full report and the generalization line. With several runs it prints the variance report and then the last run in detail.
6. If `--out` is set, JSON is written.
7. If `--fail-under` is set, `gate()` is applied and the process exits 1 on any problem.

### The CI gate (`gate()`)

The gate judges the WORST run, so flakiness fails it. It reports a problem when:

- a run's overall score is below `--fail-under` (CI uses 97);
- any scenario's total is below `--min-scenario` (CI uses 95);
- any of these raw counters is non-zero: `duplicate_state_changes`, `invalid_payloads`, `trace_violations`, `false_completion_claims`, `stale_completions`.

`.github/workflows/ci.yml` runs the gate after the unit tests on a Python 3.10, 3.11 and 3.12 matrix, with the `embeddings` extra installed so the MiniLM classifier runs as in production:

```
python -m agent.eval --llm mock --virtual --set all --quiet --fail-under 97 --min-scenario 95
```

Note a subtle point: `stale_reruns` is reported but is not one of the gated counters, even though the module and the CLAUDE.md summary talk about stale work in general; stale completions, the harmful case, are gated.

## 9. Virtual-time mode

### Purpose

Real scenarios take seconds each (tool latencies of 1 to 2.5 s, debounce windows, 0.7 s quiescence). Running 39 of them in real time is slow and, worse, load-dependent. Virtual time makes the gate fast and deterministic.

### How it works (`agent/clock.py`)

1. All agent code uses `clock.now()`, `clock.monotonic()` and `clock.is_virtual()` instead of `time.*`, and all delays are `asyncio.sleep` or `wait_for`.
2. `run_virtual(coro)` creates a `VirtualTimeLoop`, a `SelectorEventLoop` whose selector is wrapped by `_JumpingSelector`. When the loop would block waiting for the next timer, the selector advances the virtual time by that timeout instead of sleeping, so seconds of simulated latency run in milliseconds. A floor of 1e-6 s guarantees progress.
3. `clock.now()` inside the virtual loop is the epoch-anchored start plus virtual time, so latency measurements remain meaningful.

Limits stated in the module docstring: virtual time is only valid for work that lives entirely on the event loop. Anything that blocks a worker thread (real Whisper, real HTTP) would see time race ahead. That is why `--virtual` is rejected with `--llm live` and implies `--skip-voice`. The scorer treats the virtual clock as real: the 100 ms cancel latencies reported below are virtual seconds.

### Worked example

`tests/test_virtual_clock.py::test_virtual_loop_jumps_over_sleeps_and_keeps_now_consistent` simulates an hour of sleep in under a second of real time and checks that `clock.now()` and `clock.monotonic()` both advanced by about 3602 s. `test_suite_scores_match_between_virtual_and_real_clock` runs four scenarios (`correction_mid_search`, `rapid_fire_corrections`, `duplicate_booking_race`, `vision_interrupt`) on both clocks and asserts the totals differ by less than 0.02 and neither shows a duplicate state change.

## 10. Mock versus live

| Aspect | Mock | Live |
|---|---|---|
| LLM | `MockLLMBackend` with `Scenario.mock_llm` scripted responses | Real backend from `get_backend(LLMConfig())` (Groq if `GROQ_API_KEY` is set, else OpenRouter, per `CLAUDE.md`) |
| Vision | `MockVisionBackend` | `OpenRouterVisionBackend` |
| What is scored | The coordination layer: cancellation, idempotency, snapshots, fast-path latency, trace validity | The whole system, including argument extraction and how the LLM handles corrections |
| Determinism | Fully deterministic, supports `--virtual` | Not deterministic; use `--runs N` and read variance |
| Cost | Free (the mock run below reports 67 LLM calls and 146,940 estimated tokens, none of them billed) | Spends real tokens; paced by `--tpm` |
| Gate | Yes, in CI on every push | No; a nightly workflow `.github/workflows/nightly-live-eval.yml` publishes a 3-run report |

An important honesty point: in mock mode the LLM is scripted, so the mock score proves that the coordination layer handles each shape of conversation correctly. It does NOT prove that a real model would choose those tool calls. The `holdout.py` docstring says the same: mock mode "only proves the coordination layer handles each shape; live mode is the real test".

Practical constraint recorded in `CLAUDE.md`: the only enabled Groq model has a 200k tokens/day cap, so live evals can exhaust it; the symptom is replies like "I hit a snag reaching my reasoning engine". The nightly workflow comment says one 3-run pass over all scenarios costs about the whole daily quota.

## 11. Recorded results

### Mock run (re-run for this document)

Command: `INTENT_EMBEDDINGS=0 .venv/bin/python -m agent.eval --llm mock --virtual --set all`. It ran 36 scenarios: 39 total, minus the 2 voice scenarios (skipped in virtual time) and `ho_correction_without_keyword` (skipped because embeddings were disabled). Because of that skip, the run was slightly more lenient than CI, which installs the embeddings extra and therefore also runs that scenario.

| Quantity | Value |
|---|---|
| Overall | 99.9 |
| Category means (task / interrupt / latency / safety) | 100.0 / 99.7 / 99.7 / 100.0 |
| Dev set | 100.0 |
| Hold-out (11 untuned scenarios) | 99.6 |
| Generalization gap | +0.4 (target: 5 or less) |
| Median interrupt-to-cancel latency | 0.1 s (worst 0.8 s) |
| Median first ack / first reply | 0.1 s / 1.1 s |
| Stale completions / re-runs / duplicate state changes | 0 / 0 / 0 |
| Invalid payloads / trace violations | 0 / 0 |
| False completion claims | 0 |
| LLM calls / estimated tokens | 67 / 146,940 |

The only scenarios below 100 were the interruption ones: `correction_mid_search` 99.6, `correction_mid_booking` 99.6, `rapid_fire_corrections` 99.8, `duplicate_booking_race` 99.9, `duplicate_booking_paraphrase` 99.9, `ho_partial_correction` 99.6 and `ho_three_way_correction` 98.7 (its interruption sub-score was 96.9). All lost points on latency or cancel speed only, not on correctness. These numbers are reproducible but they are mock scores: a perfect result here is the expected state for a healthy coordination layer, not evidence about the language model.

### Live run (read from `eval_results/live_2026-09-29.txt` and `.json`)

The only recorded live result is `eval_results/live_2026-09-29.json` with its `.txt` twin. It was run on 2026-09-29 and covers 16 scenarios, which is fewer than the current dev set of 25 (it predates the export, timer, speech, vision and hold-out scenarios), and it has no hold-out data, no truthfulness counter and no generalization line.

| Quantity | Value |
|---|---|
| Overall | 98.4 |
| Task / interrupt / latency / safety | 100.0 / 97.9 / 94.5 / 100.0 |
| Median cancel latency (worst) | 0.101 s (1.357 s) |
| Median first ack | 0.101 s |
| Median first reply | 2.048 s |
| Stale completions / re-runs / duplicate state changes | 0 / 0 / 0 |
| Invalid payloads / trace violations | 0 / 0 |
| INFRA-degraded scenarios | none |

The weakest rows were the voice scenarios, `voice_task` 94.1 and `voice_barge_in` 91.5, where real Whisper transcription time dominates the latency score (latency sub-scores 74.6 and 73.1), and `correction_mid_search` at 98.1 (latency 87.5). Overall, this is a single run, so there is no variance information, and it should not be read as the current live score of the full 39-scenario suite. A fresh full live run was not performed for this document because of the token budget described above. The nightly workflow is configured to produce 3-run reports with `--set all`; none of those are committed under `eval_results/`.

## 12. The pytest suite (`tests/`)

### Overview

There are 46 entries in `tests/` (44 `test_*.py` files, a `fixtures/audio` directory with `book_flight.wav`, `book_flight.webm`, `correction_goa.wav` and `silence.pcm`, and a `__pycache__`). Collection reports 430 tests, of which 427 run by default and 3 are deselected as `live`. `pyproject.toml` sets `testpaths = ["tests"]`, `asyncio_mode = "auto"`, `pythonpath = ["."]` and `addopts = "-m 'not live'"`.

### What the `live` marker means

The marker is declared in `pyproject.toml`: "talks to a real network service; excluded by default (skipped/flaky offline or under rate limits), run with `pytest -m live`". There are exactly three, each of which also skips itself when the API key is absent:

- `tests/test_llm_client.py`: two live tests, including `test_openrouter_live_tool_calling` (skips without `OPENROUTER_API_KEY`);
- `tests/test_vision.py::test_live_free_vision_model_reads_a_sign` (skips without `OPENROUTER_API_KEY`).

Run them with `pytest -m live`. They spend tokens and can be flaky under rate limits, which is why they are excluded from CI.

### Tests grouped by what they protect

Counts are the number of collected tests in each file (from `pytest --collect-only`).

**The eval harness itself (so the gate cannot lie)**

| File | Tests | Protects |
|---|---|---|
| `tests/test_eval_scorer.py` | 25 | Unit tests of each scoring rule with hand-built `RunRecord` objects: linear thresholds, renormalized weights, missing cancel, stale completion, stale re-run, snapshot rules, duplicate detection, retry after cancel, schema-invalid args, epoch regression, latency windows. |
| `tests/test_eval_suite.py` | 15 | The dev-set regression gate and the MUTATION tests: cancellation disabled, idempotency disabled, the rephrased duplicate slipping past raw-string idempotency, vision never re-planning, uncancellable vision call, slots not applied to the snapshot, interrupt detector blind, snapshots never emitted, no acknowledgement, failures swallowed. Each monkeypatches a bug into the real agent and asserts the harness scores it badly (for example cancellation disabled gives interruption score 0.0 and total below 0.6). |
| `tests/test_eval_v2.py` | 24 | Hold-out well-formedness and mock gate, keyword-only classifier missing a correction, claim-detector rules, truthfulness on real and fake bookings, unreadable tool results, variance and flaky detection, generalization gap, CLI multi-run, gate pass and fail, exit code. |
| `tests/test_speech_eval.py` | 4 | The spoken-barge-in scenarios, with mutations for "keeps talking" and "forgets what was heard". |
| `tests/test_virtual_clock.py` | 2 | Virtual loop correctness and virtual-versus-real score parity. |

**Coordination core**

| File | Tests | Protects |
|---|---|---|
| `tests/test_coordinator_flow.py` | 1 | End-to-end flow through the coordinator. |
| `tests/test_state_machine.py` | 5 | Epochs, slots, undo/rollback and in-flight calls in `SessionState`. |
| `tests/test_canonical.py` | 4 | Argument canonicalization used for idempotency keys. |
| `tests/test_slot_validation.py` | 27 | Slot validation. |
| `tests/test_validator.py` | 4 | Schema/payload validation. |
| `tests/test_trace_logger.py` | 5 | Schema-validated trace. |
| `tests/test_adversarial_timing.py` | 2 | Rapid-fire correction coalescing; fault-injected latency and cancellation. |
| `tests/test_session_eviction.py` | 5 | Session cleanup. |
| `tests/test_agent_workers.py` | 6 | Worker agents. |
| `tests/test_tool_replies.py`, `tests/test_templates.py` | 8, 3 | Tool-result summaries and Tier 2 template fillers. |
| `tests/test_latency_levers.py` | 10 | Latency-related coordinator behaviour. |
| `tests/test_intent_classifier.py`, `tests/test_intent_calibration.py` | 2, 8 | Tier 1 interrupt classifier and its calibration. |

**Memory**

| File | Tests | Protects |
|---|---|---|
| `tests/test_scratchpad.py` | 5 | Scratchpad tier. |
| `tests/test_graph_memory.py` | 5 | Graph memory nodes and edges. |
| `tests/test_memory_extraction.py` | 15 | Entity extraction. |
| `tests/test_memory_baseline.py` | 3 | Baseline behaviour. |
| `tests/test_adversarial_memory.py` | 4 | Rapid slot overwrites, rollback ("go back"), DAG acyclicity after adversarial input, commit latency budget. |

**LLM layer**

| File | Tests | Protects |
|---|---|---|
| `tests/test_llm_client.py` | 5 (2 live) | Backends, fallback model, clarification on failure. |
| `tests/test_circuit_breaker_recovery.py` | 5 | Circuit breaker states. |
| `tests/test_rate_limit_retry.py` | 16 | 429 retry behaviour. |

**Voice, vision and export**

| File | Tests | Protects |
|---|---|---|
| `tests/test_speech.py` | 23 | Speech output and barge-in logic. |
| `tests/test_speech_loopback.py` | 2 | Piper loopback (skipped when `piper-tts` or the voice file is not installed). |
| `tests/test_voice_stream.py`, `tests/test_voice_websocket.py`, `tests/test_audio_ingestion.py` | 17, 3, 3 | Streaming VAD/ASR, the WebSocket voice path, audio ingestion. |
| `tests/test_asr_real.py` | 10 | Real Whisper; skips when the model is unavailable. |
| `tests/test_vision.py` | 25 (1 live) | Vision backends, frame handling, prompt injection handling. |
| `tests/test_export.py` | 38 | `export_artifact`: safe filenames, allow-listed extensions, size cap, never overwriting. |

**Server, security and deployment**

| File | Tests | Protects |
|---|---|---|
| `tests/test_server.py`, `tests/test_server_routing.py` | 2, 4 | FastAPI app and routing. |
| `tests/test_hardening.py` | 47 | Auth, origin checks, rate limits, headers and other hardening defaults. |
| `tests/test_launcher.py` | 16 | The `kairos` launcher. |
| `tests/test_deploy_files.py` | 8 | Deployment files. |
| `tests/test_full_warmup.py` | 3 | Warm-up path. |

**Repository guards**

| File | Tests | Protects |
|---|---|---|
| `tests/test_imports.py` | 1 | Imports every module, so a syntax error in a module no other test touches cannot ship. |
| `tests/test_no_secrets.py` | 2 | No committed API keys (CI also runs gitleaks). |
| `tests/test_android_fixtures.py` | 5 | The Android protocol fixtures generated from the real pydantic classes by `scripts/dump_android_fixtures.py` stay in sync. |

### Running subsets

```
pytest -q                                               # default: 427 tests, live excluded
pytest tests/test_coordinator_flow.py::test_name -q     # a single test
pytest tests/test_eval_suite.py tests/test_eval_v2.py   # the harness and its mutation proofs
pytest -m live                                          # the 3 real-network tests (spends tokens)
ruff check agent tests scripts                          # CI lint: only E9 and F rules
```

### Why the eval tests use virtual time

The comment in `tests/test_eval_suite.py::score` explains a real bug: scenarios once ran concurrently in real time on one loop, so MiniLM classification of about 20 simultaneous first messages delayed the 150 ms-spaced events of `rapid_fire_corrections` enough to coalesce two of them in the debounce window, which misaligned the scripted LLM and failed the gate. The helper now runs each scenario with `clock.run_virtual` in a worker thread (since `run_virtual` owns its own loop), making the gate independent of machine speed.

### Worked example: a mutation test

`test_MUTATION_cancellation_disabled_is_caught` monkeypatches `SessionState.bump_epoch` to increment the epoch without returning any calls to cancel, then scores `correction_mid_booking`. Because nothing is cancelled, the stale Mumbai booking really completes. The assertions then verify what the rubric is supposed to do: `interrupt.score == 0.0`, `stale_completions >= 1`, `task.parts["no_spurious_state_changes"] == 0.0`, and `total < 0.6`. This is the proof that a coordinator without cancellation would fail CI, and it works only because the scorer reads the ground-truth execution log and not just the trace.

## 13. How the pieces connect

- `agent/eval/runner.py` imports `AgentCoordinator`, `TraceLogger`, `ToolRouter` (through `Environment`), the mock and real LLM and vision backends, the TTS mock, and the event schemas, so the eval exercises production code paths unchanged; only the tools, LLM and vision backends are substituted.
- The scorer depends on the trace schema from `agent/schemas/actions.py` (action types `tool_call`, `tool_cancel`, `state_snapshot`, `filler`, `spoken_response`, `clarification`, `speech_state`, `audio_out`) and on `agent/coordination/canonical.py`.
- `agent/clock.py` is the seam that makes virtual time possible; any new agent code that calls `time.time()` or sleeps outside `asyncio` would break the virtual-time gate.
- CI runs `pytest` first, then the mock gate, so a failure in either blocks the merge. The nightly live workflow is informational.

## 14. Failure modes and caveats, summarized

- **Proxy formulas.** The thresholds and the equal weighting of sub-scores inside categories are the team's own choices; the official rubric may differ. Raw measurements are printed to compensate.
- **Mock scores are structural.** A perfect mock score does not say the real LLM extracts arguments or handles corrections correctly. The only recorded live evidence is the 16-scenario run from 2026-09-29 (overall 98.4).
- **Voice scenarios are not part of the gate.** They need real audio, ffmpeg and Whisper, so they are skipped in virtual time and in CI's gate.
- **Hold-out hygiene is by convention.** Nothing in code prevents tuning against hold-out scenarios; the three `tuned` ones are documented and excluded from the gap.
- **Embeddings dependence.** Without the MiniLM classifier, `ho_correction_without_keyword` is skipped by the CLI, and `tests/test_eval_v2.py` shows that with keyword-only classification this scenario would score under 0.8 with a stale completion.
- **Gate scope.** The gate fails on stale completions but not on stale re-runs, even though both are tracked.
- **Live quota.** Live runs and recordings can exhaust the 200k tokens/day Groq cap; affected scenarios are flagged `INFRA` rather than silently scored as agent failures.
