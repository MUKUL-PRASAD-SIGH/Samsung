# Tools and Autonomous Workers

## 1. Overview

Everything the agent can actually do in the world is a tool call. The LLM planner (`agent/slow_path/planner.py`) is given the tool manifests, picks at most one tool per turn, and the coordinator (`agent/coordinator.py`) executes it in the background under the epoch model. There are 10 registered tools:

- Travel and lookup: `search_flights`, `book_flight`, `search_hotels`, `book_hotel`, `check_weather`, `cancel_booking`.
- General: `set_timer`, `analyze_frame`, `export_artifact`.
- Meta: `spawn_agent`, which creates an autonomous worker (`agent/workers/*`) that streams step-by-step thoughts and produces an artifact.

All ten are registered in `ToolRouter.register_default_tools()` in `agent/coordination/tool_router.py`. Three of them (`spawn_agent`, `analyze_frame`, `export_artifact`) have a placeholder handler in the router and are actually executed by special branches in `AgentCoordinator._execute_tool_task`, because they need coordinator-owned state (the vision backend and frame buffer, the per-session artifact list, the LLM backend and the action queue).

An honest framing for judges: the travel, weather and timer tools are simulated. They sleep for a fixed latency, then return canned data (for example `search_flights` always returns the same three flights). That is deliberate: they give the interruption machinery real, observable, cancellable latency to work against, and make the evaluation harness deterministic. The tool surface, schemas, read/write classification, idempotency, cancellation and result handling are real. `export_artifact` really writes files and `analyze_frame` really calls a vision model.

## 2. The tool router

`agent/coordination/tool_router.py` defines `ToolDefinition` (name, description, JSON-schema parameters, `is_state_modifying`, async handler) and `ToolRouter`.

### 2.1 Key types and functions

| Name | File | Role |
|---|---|---|
| `ToolDefinition` | `agent/coordination/tool_router.py` | One tool: schema, state-modifying flag, handler. `validate_args` runs `jsonschema.validate`. |
| `ToolRouter.register_tool` | same | Adds or replaces a tool by name (tests and the eval environment use it to override handlers). |
| `ToolRouter.get_tool_manifests` | same | Produces the OpenAI-style function list sent to the LLM, with an extra `is_state_modifying` field. |
| `ToolRouter.validate_call` | same | Validates arguments against the tool's schema; raises `UnknownToolError` for an unregistered tool. |
| `ToolRouter.is_state_modifying` | same | Read vs write classification. For an unknown tool, a name prefix heuristic (`book`, `cancel`, `delete`, `create`, `update`, `buy`, `pay`, `send`) decides. |
| `ToolRouter.execute_tool` | same | Awaits the tool's handler with the arguments. Raises `ValueError` if there is no handler. |
| `Planner.plan` | `agent/slow_path/planner.py` | Validates, stages slots, registers the call. |
| `AgentCoordinator._execute_tool_task` | `agent/coordinator.py` | Runs the tool (router handler or a special branch) and posts a `ToolResultEvent`. |
| `summarize_tool_result`, `summarize_tool_error` | `agent/tool_summaries.py` | Deterministic one-sentence replies for each tool result or failure. |

### 2.2 The path of one tool call

1. The planner asks the LLM (with the tool manifests) for a response. If the epoch changed while the LLM was thinking, the plan is discarded and nothing is dispatched.
2. If the response is a tool call, `ToolRouter.validate_call` checks the arguments against the JSON schema. An invalid or unknown call is never run; the planner replies with a request for the details again (`Planner._reject`). This is the guard against a model emitting a malformed call.
3. `entities_from_tool_call` and `SessionState.stage_call_slots` patch the call's arguments into the session slots (see the memory document, section 6). This happens before registration so the idempotency key reflects the new state. `SKIP_TOOLS` (`spawn_agent`, `analyze_frame`, `export_artifact`) are not turned into slots.
4. `SessionState.register_tool_call` tags the call with the current epoch, computes an idempotency key if the tool is state-modifying, and returns `None` for a duplicate. A `ToolCallAction` is returned otherwise.
5. `AgentCoordinator._dispatch_actions` emits the `ToolCallAction`, starts `_execute_tool_task` as an `asyncio` task and attaches it to the in-flight call (`attach_task`).
6. `_execute_tool_task` runs the tool and posts a `ToolResultEvent` carrying the epoch the call started under. Anything that is not a dict result is converted into an error ("the service returned an unreadable response"), so a garbled payload can never produce a false "Done".
7. `_handle_tool_result` calls `SessionState.complete_tool_call`. If the call was cancelled or belongs to an older epoch it returns `False` and the result is dropped (logged as a stale result). Otherwise a spoken reply is emitted (`summarize_tool_result` or `summarize_tool_error`, or an artifact announcement for workers), a state snapshot is sent and the turn is committed to memory.
8. If the user interrupts in between, `SessionState.bump_epoch()` cancels the task (`asyncio_task.cancel()`), reverts the call's slot changes and emits a `ToolCancelAction`. A cancelled task returns silently without posting a result.

Because every tool call carries its epoch, a result that arrives after a correction can never be shown as if it were current. `tests/test_tool_replies.py::test_cancelled_tool_call_stays_silent` covers the silent-cancel behaviour.

## 3. Tool reference table

Read vs state-modifying is the router's classification (`is_state_modifying`). "Idempotent how" describes what protects the tool against duplicate effects.

| Tool | Args (required in bold) | Kind | Returns | Idempotent how |
|---|---|---|---|---|
| `search_flights` | **origin**, **destination**, date (default "today") | Read | `{"flights": [3 items: flight, airline, origin, destination, price, departure]}` after a 3.0 s simulated delay | No key: it is read-only and safe to repeat. |
| `book_flight` | **origin**, **destination**, flight (default "6E-455") | State-modifying | `{"booking_id": "FL-98214", "status": "confirmed", origin, destination, flight}` after 2.5 s | Idempotency key over tool name, intent, canonical slots, epoch and canonical arguments. A repeat in the same epoch and state is skipped. |
| `search_hotels` | **city**, nights (integer) | Read | `{"hotels": [2 items: name, stars, price_per_night, rating]}` after 3.0 s | No key (read-only). |
| `book_hotel` | **city**, hotel_name, nights (integer) | State-modifying | `{"reservation_id": "HT-44012", "status": "confirmed", hotel, city, nights}` after 2.5 s | Same idempotency key as `book_flight`. |
| `check_weather` | **city** | Read | `{"city", "condition": "Sunny", "temp": "28°C", "humidity": "45%"}` after 2.0 s | No key (read-only). |
| `cancel_booking` | **booking_id** | State-modifying | `{"status": "cancelled", "booking_id", "refund": "processed"}` after 2.0 s | Same idempotency key. |
| `set_timer` | **seconds** (integer, 1 to 300), label | Read (flagged non-modifying) | `{"label", "seconds", "status": "finished"}` after sleeping `seconds` | No key. Cancellation instead: the sleeping task is cancelled by `bump_epoch`, and then it never announces. |
| `analyze_frame` | **question** | Read | `{"has_frame": true, "answer", "model", "frame_id", "frame_age_s"}`, or `{"has_frame": false, "answer": "", "note": ...}` | No key (read-only). Bounded by design: the continuation plan is not offered `analyze_frame` again. |
| `export_artifact` | filename, content, language, open_in (`vscode` or `none`); none required | State-modifying | `{"exported": true, filename, path, bytes, language, editor_uri, download_path, opened_with, folder, preview}` or `{"exported": false, "error"}` | Idempotency key as above, plus the file layer never overwrites (`name-1.ext`, ...). |
| `spawn_agent` | **name**, **role**, **goal**, system_prompt, steps (array), expected_artifact (`title`, `language`), component, language | Read (flagged non-modifying) | The worker's result dict, normally with `status`, `agent_name`, `role` and an `artifact` | No key; cancelled via the epoch. See section 7. |

Remarks on the table:

- The simulated delays come from `asyncio.sleep` in the handlers (3.0, 2.5, 3.0, 2.5, 2.0, 2.0 seconds), so under the virtual clock of the eval harness they cost no wall time.
- The idempotency key is computed in `IdempotencyStore.generate_key` (`agent/coordination/idempotency.py`) as a SHA-256 over `tool_name:intent:sorted canonical slots:epoch`, plus the canonical arguments when present. Canonicalisation (`agent/coordination/canonical.py`) lower-cases and normalises whitespace, and maps location-like keys (`origin`, `destination`, `city`, `from`, `to`, `location`) to IATA codes when the city is in a small table (for example Mumbai and Bombay both become `BOM`, three-letter alphabetic values are upper-cased). So "Mumbai" and "BOM" hash the same and a rephrased duplicate booking is caught. Unknown cities fall back to lower-case comparison.
- Because the epoch is part of the key, the same booking issued again after an interrupt (a new epoch) gets a new key. That is intended: the earlier call was cancelled, so the re-issued one is a legitimate new attempt. A cancelled call's key stays in the store but cannot match any new-epoch key.
- Different arguments are different keys, so booking DEL to BOM and then DEL to GOA are both allowed while an exact repeat is blocked. The regression test is `tests/test_memory_extraction.py::test_different_arguments_are_not_duplicates_but_identical_calls_are`.
- `IdempotencyStore.complete` is called when a call completes (`complete_tool_call`), recording the result against the key.
- `set_timer` and `spawn_agent` are flagged read-only, so they have no idempotency key. A duplicate `set_timer` request starts a second timer. This matches the description in the code (timers are cancellable) but means timers rely on cancellation, not dedup.
- The tool-level descriptions steer the LLM: `book_flight` tells the model it needs only origin and destination ("do not ask the user for a date or time first"), and the system prompt in the planner says that "I need to get to..." is not a request to book.

## 4. Travel, weather and booking tools

### 4.1 Purpose

These stand in for real external services. They exist so the agent has realistic tasks that take seconds, can be interrupted, and either read data or change external state. The classic scenario is "search flights to Mumbai", interrupted by "actually, Goa".

### 4.2 How they work

Each is an async function registered by `register_default_tools` with an `asyncio.sleep` latency and a fixed return value built from the arguments (hotel names are generated from the city, for example "The Grand <city>"). Reads and writes differ only by the `is_state_modifying` flag, which drives idempotency tracking in `register_tool_call`. Default argument values (flight "6E-455", hotel "Downtown Suites", 2 nights) apply when the model omits optional arguments.

The reply spoken to the user comes from `agent/tool_summaries.py`, for example `_flights` mentions how many flights were found, lists up to three and names the cheapest (computed by parsing the price string), `_book_flight` reads out the booking id, and `_cancel` reports the refund. If a result is empty or malformed the summariser falls back to "Done — <tool> completed." (`tests/test_tool_replies.py::test_empty_and_malformed_results_fall_back_gracefully`).

### 4.3 Worked example: duplicate and corrected booking

1. User: "book a flight from Delhi to Mumbai". The model calls `book_flight(origin="Delhi", destination="Mumbai")`. Schema validation passes. Slots `origin` and `destination` are staged. `register_tool_call` computes the key over `book_flight`, the intent, slots `{origin: DEL, destination: BOM}`, the epoch (say 1) and canonical arguments, then registers it. The task starts its 2.5 s sleep.
2. The same sentence is sent again 1 s later (a double tap or a retry). The planner stages the same slots, `register_tool_call` computes the identical key, `exists()` is true and `None` is returned. The planner calls `revert_call_slots` and returns no tool action, so a second booking is never made.
3. Instead, the user says "actually, Goa". The interrupt classifier bumps the epoch to 2: the running task is cancelled, a `ToolCancelAction` is emitted, the slots revert. The model issues `book_flight(destination="Goa")`, the key differs (new epoch and arguments), and the booking proceeds. The original result never appears.

## 5. set_timer

`set_timer(seconds, label)` has a schema with `seconds` constrained to integers 1 to 300 (so an out-of-range value is rejected by validation before it can run). The handler simply sleeps for `seconds` and returns `{"label", "seconds", "status": "finished"}`. When the result arrives, `summarize_tool_result` produces "Your <label> timer (<n> seconds) just finished." and it is spoken. While the timer runs it is an ordinary in-flight call, shown in the state snapshot, and cancelling it (an interrupt such as "never mind the timer") cancels the sleeping task, so the announcement never happens.

The eval harness has a scenario for this (`agent/eval/scenarios.py`, using `set_timer` with `forbid_completed` after a cancel), and `tests/test_export.py::test_a_timer_finishes_announces_itself_and_can_be_cancelled` runs both paths on the virtual clock: with no cancel, exactly one spoken response mentioning "tea" and "30 seconds"; with a cancel, none.

## 6. analyze_frame (vision)

### 6.1 Purpose

Lets the agent answer questions about what the user is showing on the camera or screen: "what is on this sign?", "book a flight to the city on this poster".

### 6.2 How it works

1. The client sends video frames as `VideoFrameEvent`s. `AgentCoordinator._handle_video_frame` only buffers them: a deque of `FRAMES_PER_SESSION = 3` per session. Frames that are empty, larger than `MAX_FRAME_BYTES` (2,000,000, from `agent/multimodal/vision.py`) or with a MIME type outside `ALLOWED_MIME` are dropped with a warning. No inference runs on arrival, so text-only turns never touch the vision model (`tests/test_vision.py::test_text_only_turns_never_touch_the_vision_model`).
2. When the model calls `analyze_frame(question)`, `_execute_tool_task` routes to `AgentCoordinator._run_vision`. It takes `latest_frame(session_id)`, which returns `None` if the newest frame is older than `MAX_FRAME_AGE_S = 15.0` seconds. In that case the result is `{"has_frame": False, "answer": "", "note": "No camera or screen frame is being shared right now."}`, an honest note instead of a hallucinated answer.
3. Otherwise `vision_backend.analyze(data, mime, question)` is awaited and the result reports `answer`, `model`, `frame_id` and `frame_age_s`.
4. `analyze_frame` is an observation tool. In `_handle_tool_result`, a successful result with a remembered origin text does not produce a spoken answer directly. Instead the coordinator emits a state snapshot and spawns `_continue_after_observation`, which re-plans the user's original request with the observation appended and `analyze_frame` removed from the tool list. This makes it a single step with no loop. Failures are summarised by `summarize_tool_error` ("I couldn't analyze the image right now ...").

Frame bytes are redacted from the trace (`tests/test_vision.py::test_frames_are_redacted_from_the_trace`). The vision backend itself (`agent/multimodal/vision.py`) uses a pool of models with cooldown and failover, covered by the other tests in `tests/test_vision.py`; it is not documented in detail here.

### 6.3 Worked example

User shares a poster and says "find me flights to the city on this poster". Planner call 1: the model calls `analyze_frame(question="Which city is named on the poster?")`. The result is `{"has_frame": true, "answer": "Goa", ...}`. `_continue_after_observation` re-plans the text "find me flights to the city on this poster" with "[Vision result for the image the user is sharing: Goa]". The model now calls `search_flights(destination="Goa", ...)`. If the user interrupts while the vision call is in flight, the call is cancelled and the continuation never runs (`test_an_interrupt_cancels_an_in_flight_vision_call`).

## 7. export_artifact

### 7.1 Purpose

Turns code the agent wrote (or code the model passes in the call) into a real file the user can open, and opens it in VS Code if possible. The flow is "ask for code, then say 'open it in VS Code'".

### 7.2 Where content comes from

The router entry is a stub that returns `{"exported": False}`; the real work is `AgentCoordinator._run_export`. Content is resolved as:

1. If the call has a non-empty `content` string, it is used.
2. Otherwise the coordinator takes the newest artifact in `self._artifacts[session_id]` (title and language become the default `filename` and `language`).
3. If there is no artifact, it returns `{"exported": False, "error": "I don't have any code to export yet. Ask me to write something first, then say 'open it in VS Code'."}` and writes nothing.

`_artifacts` is filled in `emit_action` whenever an `AgentStepAction` carrying an `artifact` is emitted, and it is bounded to the last 10 per session (`test_artifacts_are_bounded_per_session`). It is dropped when a session is evicted.

### 7.3 The file layer: agent/exporter.py

Key types and functions:

| Name | File | Role |
|---|---|---|
| `export_file` | `agent/exporter.py` | Validates, names, writes, optionally opens; returns an `ExportResult`. |
| `safe_filename` | `agent/exporter.py` | Produces a safe base name with an allow-listed extension. |
| `_unique` | `agent/exporter.py` | Never overwrites: appends `-1`, `-2`, ... up to 999, then fails. |
| `export_dir` | `agent/exporter.py` | `EXPORT_DIR`, default `~/kairos-exports`, created if needed. |
| `editor_uri` | `agent/exporter.py` | `vscode://file/<percent-encoded absolute path>`. |
| `try_open_in_editor` | `agent/exporter.py` | Launches the editor if allowed and available. |
| `resolve_export` | `agent/exporter.py` | Safe lookup used by the download endpoint. |
| `ExportError` | `agent/exporter.py` | Refusal with a message safe to show the user. |
| `ExportResult` | `agent/exporter.py` | `filename`, `path`, `bytes`, `language`, `editor_uri`, `download_path`, `opened_with`. |

Safety rules, which exist because the file name and content come from an LLM that may have read untrusted user or image text:

- Only the base name is used. Backslashes are normalised and any directory part is dropped, so traversal like `../../etc/passwd` becomes the base name only. Characters outside `A-Za-z0-9._-` become `_`, leading dots are stripped (no hidden files), and the stem is cut to 80 characters. An empty stem becomes `kairos_export`.
- The extension must be in `ALLOWED_EXTENSIONS`, derived from `LANGUAGE_EXTENSIONS` (source and text types such as py, js, ts, tsx, jsx, html, css, json, md, txt, svg, java, kt, go, rs, c, cpp, sql, yml, swift, rb, php, toml, xml). A file ending in `.sh`, `.bat`, `.exe` and similar is refused with an `ExportError`, because a user might double-click it. With no extension, the extension is taken from the `language`, defaulting to `txt`.
- Size is capped at `MAX_BYTES = 200_000` (UTF-8 bytes) and an empty or whitespace-only content is refused (no empty files).
- The file is written under the export folder only. After choosing the name, `export_file` rechecks that the export directory is a parent of the resolved path ("defence in depth") and refuses otherwise.
- Existing files are never overwritten.
- The editor is launched with an argument list (`[exe, "--reuse-window", path]`), never through a shell, with stdin/stdout/stderr discarded and `start_new_session=True`, so it does not block and a hostile file name cannot inject editor arguments (`test_a_hostile_filename_cannot_inject_editor_arguments`). A failure to launch returns `None` and never fails the export.
- Opening in the editor is only attempted when `open_in` is `vscode` (the default). In `auto` mode (default) it requires the editor command on `PATH` and a display (`DISPLAY` or `WAYLAND_DISPLAY` on Linux; always true on macOS and Windows).
- `resolve_export(name)` is used by `GET /exports/{name}` in `agent/server.py` and is deliberately strict: the name must equal its own base name, must not start with a dot, must be an existing file whose resolved path is inside the export directory. Otherwise the endpoint returns 404. `/exports/` is in `PROTECTED_PREFIXES`, so it needs the bearer token when `AUTH_TOKEN` is set (`test_downloads_need_the_token_when_one_is_configured`).

### 7.4 After the write

`_run_export` runs `exporter.export_file` in a worker thread (`asyncio.to_thread`) so a file write can never stall interrupt handling. Under the virtual clock it runs inline, because a real thread would let virtual time run ahead. It returns the `ExportResult` fields, plus `folder` and a `preview` (the first `EXPORT_PREVIEW_CHARS = 6000` characters).

On the result event, `_handle_tool_result` emits a `FileExportedAction` (in `agent/schemas/actions.py`) with the filename, path, size, language, editor URI, download path, which editor opened it and the preview, so the UI can show an export card. Then the standard spoken reply is produced by `_export` in `agent/tool_summaries.py`: "Saved <name> (<KB>) to <folder>; opened it in VS Code." or "...it is ready to open from the Exports panel." A refused export is reported with the error text rather than swallowed (`test_a_refused_export_is_reported_not_swallowed`).

The vscode URI is only meaningful when the server runs on the user's machine; for a remote user, `download_path` (`/exports/<name>`) is the way to get the file.

### 7.5 Configuration

| Variable | Default | Effect |
|---|---|---|
| `EXPORT_DIR` | `~/kairos-exports` | Where files are written and served from. |
| `EXPORT_OPEN` | `auto` | `0`/`false`/`no`/`off` disables launching the editor; `1` forces an attempt without the display check; `auto` requires the editor and a display. |
| `EXPORT_EDITOR_CMD` | `code` | The editor executable (looked up with `shutil.which`). |

The cap, the extension allow-list, `FRAMES_PER_SESSION` and similar values are constants in code, not environment variables.

### 7.6 Worked example

User: "write a debounce function in TypeScript and open it in VS Code". Turn 1: the model calls `spawn_agent` with an `expected_artifact` of `debounce.ts`. A worker (section 9) streams steps and its last step carries the artifact, which `emit_action` stores. Turn 2: the model calls `export_artifact` with no arguments (or only `open_in`). `_run_export` takes the newest artifact, `safe_filename("debounce.ts", "typescript")` returns `debounce.ts`, the file lands in `~/kairos-exports/debounce.ts`, `code --reuse-window <path>` is launched if the machine has a display, and the UI gets a `file_exported` card. Asking again for the same export in the same state hits the idempotency key and exports once (`test_the_same_export_asked_twice_runs_once`: only `a.py` exists, no `a-1.py`).

Exporting is skipped from slots on purpose (`SKIP_TOOLS`), so a whole source file never becomes part of the session state, snapshot or idempotency hash. The idempotency key therefore depends on tool name, intent, slots, epoch and the canonicalised arguments, which include `content` when it is passed.

## 8. spawn_agent and the worker swarm

### 8.1 Purpose

`spawn_agent` is the "agent that builds agents" tool. When the user asks for something specialised (a database schema, an SVG, a security audit, a component) the planner's system prompt tells the model it can "synthesize custom, bespoke agents on the fly": it chooses a name, a role, a `system_prompt`, a step plan and an `expected_artifact`. The coordinator creates a worker object that runs a multi-step loop, streams each step to the UI as an `AgentStepAction`, and finishes with an artifact. The streaming gives live "thinking" visible to the user, and because the worker is just an `asyncio` task, it is cancelled instantly by an interrupt.

### 8.2 How it works (coordinator side)

In `_execute_tool_task`, for `tool_name == "spawn_agent"`:

1. Pops `name` (default "bob"), `role` (default "Worker") and `goal` from a copy of the arguments.
2. Calls `create_agent_worker(name, role, goal, session_id, epoch, call_id, llm_backend=self.llm_backend, **remaining_args)` from `agent/workers/registry.py`. Every remaining argument (such as `steps`, `system_prompt`, `expected_artifact`, `component`, `language`) is forwarded.
3. Defines `_step_callback`, which calls `emit_action` for each `AgentStepAction`.
4. Awaits `worker.execute(step_callback)`. On `asyncio.CancelledError` it logs and returns without posting a result; on another exception it posts an error result.
5. The returned dict becomes the `ToolResultEvent`. In `_handle_tool_result`, a result containing `"artifact"` produces the announcement "Agent '<name>' has successfully finished building '<title>'. The artifact is ready in your workspace." and the artifact is passed to the memory commit (an `ArtifactNode`).

The `spawn_agent` router handler (`_spawn_agent`) is a stub that returns a completed dict; the comment in the code says streaming is handled by the coordinator. `spawn_agent` is flagged non-modifying, so it has no idempotency key; cancellation by epoch is its protection.

### 8.3 Key types

| Name | File | Role |
|---|---|---|
| `BaseAgentWorker` | `agent/workers/base.py` | Abstract base: identity, epoch, call id, step counter, `emit_step`, `sleep_cancellable`, abstract `execute`. |
| `create_agent_worker` | `agent/workers/registry.py` | Factory that picks the worker class. |
| `DynamicAgentWorker` | `agent/workers/dynamic_agent.py` | LLM-designed worker with custom persona, steps and artifact; the general case. |
| `CoderAgent` | `agent/workers/coder_agent.py` | The "bob" TypeScript generator; fixed four steps. |
| `ResearcherAgent` | `agent/workers/researcher_agent.py` | The "scout" travel researcher; fixed three steps. |
| `ValidatorAgent` | `agent/workers/validator_agent.py` | The "sentinel" validator; fixed two steps, no artifact. |
| `AgentStepAction` | `agent/schemas/actions.py` | Wire message for one step: `call_id`, `name`, `role`, `step`, `total_steps`, `thought`, `status`, `artifact`. |

### 8.4 BaseAgentWorker

The constructor stores `name`, `role`, `goal`, `session_id`, `epoch`, `call_id`, `total_steps` (default 4), `step_delay_s` (default 0.8) and extra keyword arguments. `emit_step(thought, step_number, status, artifact, step_callback)` builds an `AgentStepAction` tagged with the worker's epoch and calls the callback. `sleep_cancellable` is a plain `asyncio.sleep`: cancellation is achieved by the coordinator cancelling the whole task, which raises `CancelledError` at the next `await`. The class has an `is_cancelled` field that nothing reads in the code read.

The status values used are `working` and `completed`; the schema comment also lists `cancelled`, but no worker emits it (a cancelled worker simply stops; the UI learns of it through `tool_cancel`).

### 8.5 The registry (worker selection)

`create_agent_worker` decides in this order:

1. If any of `steps`, `system_prompt`, `expected_artifact` or `artifact` are in the arguments, return a `DynamicAgentWorker`. This is the normal path for LLM-designed agents.
2. Otherwise lower-case the name, role and goal and look for keywords:
   - "research", "scout", "flight", "hotel" or "travel" gives `ResearcherAgent` (defaults: name "scout", role "Market & Flight Scout").
   - "valid", "sentinel", "lint", "test" or "audit" gives `ValidatorAgent`.
   - "bob" in the name, or "clock", "analog" or "typescript generator" in the goal, gives `CoderAgent`.
   - Anything else gives a `DynamicAgentWorker` named "specialist" by default.

The order matters: a goal mentioning a "test" in a flight context would pick the researcher first (the researcher check comes first).

### 8.6 DynamicAgentWorker

Constructor arguments: `system_prompt` (default "You are <name>, an expert <role>."), `steps` (list of thoughts; default is three generic steps built from the goal and role), `expected_artifact` (dict with `title` and `language`, or the `artifact` keyword), and `llm_backend`. `total_steps` equals the number of steps.

`execute` flow:

1. For every step except the last it emits a `working` step and sleeps `step_delay_s` (default 0.8).
2. It calls `_generate_artifact()`.
3. It emits the last step with status `completed` and the artifact.
4. It returns `{"status": "completed", "agent_name", "role", "artifact"}`.

`_generate_artifact` works out the title and language. If `expected_artifact.title` is missing it infers from goal keywords: "sql", "database" or "postgres" gives `schema.sql`; "svg", "icon", "gauge" or "tachometer" gives `VectorGraphic.svg`; "python" or ".py" gives `script.py`; "rust" or ".rs" gives `main.rs`; "audit", "security" or "report" gives `SecurityReport.md`; otherwise `<Name>Artifact.tsx` in TypeScript. If an `llm_backend` is set (the coordinator always passes it), it sends a system message (the persona, "Generate the exact, complete, high-quality code or file content for '<title>'. Do not include conversational preamble or markdown backticks.") plus the goal, via `llm_backend.generate(messages)`. A reply longer than 20 characters is used, after stripping a wrapping code fence. If the call fails or the reply is too short, `_synthesize_fallback_content` returns a canned template for sql, svg, python, markdown or a React TSX component. The artifact dict is `{title, language, content, author, description}`.

Observations on the fallbacks: they are offline templates, not generated from the goal (for example the SQL fallback is a ride-sharing schema whatever the goal). The TSX fallback template emits an import line of the form `import React, {"useState", "useEffect"} from "react";` because of f-string brace escaping, which is not valid JavaScript import syntax. This only affects the offline fallback and is the one defect found in the worker code.

### 8.7 CoderAgent, ResearcherAgent and ValidatorAgent

These are fixed-script demo workers, written before the dynamic worker and kept as fast, deterministic specialists. Their output is canned:

- `CoderAgent` (defaults name "bob", component "AnalogClock", language "TypeScript", `step_delay_s` 1.0) has four steps (requirements, state hooks and London timezone logic, dial synthesis, verification). It always returns the same `DEFAULT_ANALOG_CLOCK_TSX` content as the artifact, titled `<component>.tsx` or `.jsx` depending on the language. It does not use the LLM.
- `ResearcherAgent` (defaults "scout", origin "BLR", destination "DEL", `step_delay_s` 1.0) has three steps and returns a JSON comparison matrix of the same three flights as the `search_flights` tool, as artifact `Route_<origin>_<destination>_Comparison.json`. The `spawn_agent` schema has no origin or destination fields, so unless the model passes extra keyword arguments the route is the default BLR to DEL. The step text "Aggregated 14 flight candidates" is a fixed string.
- `ValidatorAgent` (defaults "sentinel", `step_delay_s` 0.8) has two steps and returns `{"verification": "PASSED"}` with no artifact. It does not inspect any code.

Judges should treat these three as scripted demonstrations of the worker streaming protocol, not as real research or validation. The `DynamicAgentWorker` is the one that calls the LLM.

### 8.8 Cancellation and the epoch

A worker is registered as an in-flight call under the epoch it started. On an interrupt, `bump_epoch()` cancels its task. `tests/test_agent_workers.py::test_coordinator_spawn_agent_and_interruption` dispatches a `spawn_agent`, lets step 1 stream, posts an interrupt and checks that there is exactly one `ToolCancelAction` for the call with epoch 2, and that the call's status is `cancelled`. `test_coder_agent_mid_step_cancellation` checks that cancelling the worker task mid-step stops it. Steps already emitted stay visible; the final artifact never arrives and (as in the export path) `emit_action` has stored only artifacts that were actually emitted.

A mid-flight worker step is also tagged with the epoch it started in (`AgentStepAction.epoch`), so clients can ignore stale steps.

### 8.9 Worked example: a spawned worker interrupted and then redirected

1. User: "design a Postgres schema for a ride sharing app". The model calls `spawn_agent(name="db_architect", role="PostgreSQL Scaling Specialist", goal="...", steps=[...4 steps...], expected_artifact={"title": "schema.sql", "language": "sql"})`.
2. `create_agent_worker` sees `steps`, returns a `DynamicAgentWorker` with `total_steps=4`.
3. Steps 1 to 3 are emitted as `working` `AgentStepAction`s, 0.8 s apart; the UI shows them as live thoughts.
4. The user interrupts after step 2: "actually make it MySQL". The epoch bumps, the worker task is cancelled, a `tool_cancel` is sent, and the planner (aware of the cancelled call's arguments through the interrupted-work note) spawns a new worker for MySQL.
5. The new worker finishes: the last step arrives with `status="completed"` and the artifact; `_handle_tool_result` says the agent finished building `schema.sql`, the artifact is committed into the graph (`PRODUCED` edge) and remembered in `_artifacts`, so "open it in VS Code" exports it.

## 9. Interactions with other components

- Planner: supplies manifests, validates and rejects invalid calls, stages slots, and decides whether to call `analyze_frame` or `export_artifact`.
- Epoch model (`SessionState`): tags every call with its epoch, cancels tasks, reverts slots and discards stale results.
- Idempotency (`agent/coordination/idempotency.py`, `canonical.py`): protects state-modifying tools.
- Memory (`agent/memory/`): turns tool arguments into entities and slots, except for `SKIP_TOOLS`, and stores artifacts as graph nodes.
- Server (`agent/server.py`): serves `/exports/{name}` under the bearer-token policy.
- Clients: both the web UI and the Android app render `agent_step` and `file_exported` actions; changing these action classes requires updating `scripts/dump_android_fixtures.py`, the fixtures, `Protocol.kt` and `ChatReducer.kt`, as CLAUDE.md notes.
- Eval harness: `agent/eval/environment.py` replaces tool handlers with scripted ones (for example its `set_timer` handler), so scenarios are deterministic.

## 10. Failure modes and guarantees

- Invalid arguments (schema violations, unknown tool, out-of-range timer, bad `nights`) never execute; the user gets a clarifying reply.
- Duplicate state-modifying calls in the same epoch and state are skipped, including phrasing variants of known cities; different arguments are allowed.
- A cancelled or stale call never produces a visible result, never leaves its slot changes behind, and never announces itself.
- A non-dict tool result is turned into an error, so the agent never claims "Done" for an unreadable response.
- Tool exceptions are caught in `_execute_tool_task`, converted to an error result and spoken as "Sorry, I couldn't complete <tool> (<error>). Want me to try again?".
- Export refusals are explicit messages; nothing is written outside the export folder, nothing is overwritten, and a failure to open the editor does not fail the export.
- Vision with no fresh frame returns an explicit "no frame" note.
- The `export_artifact` default content depends on the in-memory artifact list: a server restart, or session eviction after `SESSION_TTL_S`, forgets it. The exported files themselves persist on disk.
- Limitations and inconsistencies found: simulated travel, weather and timer backends; the three fixed workers return canned content; the offline TSX fallback emits an invalid import line; `BaseAgentWorker.is_cancelled` is unused; `set_timer` and `spawn_agent` are non-modifying so they are not deduplicated (timers are only cancellable); `set_timer` arguments become slots because it is not in `SKIP_TOOLS`.

## 11. Tests that cover it

| Test file | What it covers |
|---|---|
| `tests/test_agent_workers.py` | Coder, researcher and dynamic worker execution, mid-step cancellation, coordinator `spawn_agent` plus interrupt, dynamic custom agent and SVG creation. |
| `tests/test_export.py` | Filename safety, refused extensions, no overwrite, size cap, Unicode round trip, editor URI, editor launch and hostile name, `resolve_export`, export via the coordinator (content in the call, worker artifact, nothing to export, refused export, deduplication), bounded artifacts, the timer, and the download endpoint with and without a token. |
| `tests/test_vision.py` | Frame buffering and limits, stale frames, vision continuation, failure path, interrupt cancels vision, text-only turns, trace redaction, WebSocket frame messages, and the model pool failover (a live test is marked `live`). |
| `tests/test_tool_replies.py` | Tool result summaries, fallbacks, error summaries, silent cancel, reply recorded in the graph. |
| `tests/test_memory_extraction.py` | Tool arguments to entities, `SKIP_TOOLS`, idempotency with different arguments. |
| `tests/test_coordinator_flow.py` and `tests/test_state_machine.py` | Epoch and cancellation behaviour used by every tool. |
| `tests/test_canonical.py` | Canonical forms behind the idempotency key. |
| `tests/test_slot_validation.py` | Rejection of bad slot patches before dispatch. |
| `agent/eval/` (`scenarios.py`, `holdout.py`) | Scored scenarios with stale-completion and duplicate-state-change checks across tools (the CI gate). |
