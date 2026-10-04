# Server, Security, Settings, Launcher and Deployment

This part documents the outer shell of Kairos: the FastAPI process that exposes the agent (`agent/server.py`), the request-level protection layer (`agent/security.py`), the operator-facing configuration object (`agent/settings.py`), the one-step `kairos` command (`agent/launcher.py`), and everything that packages and verifies the system (`Dockerfile`, `docker-compose.yml`, `Caddyfile`, `.github/workflows/*.yml`, `requirements.lock`, `scripts/fetch_models.py`, `scripts/eval_trend.py`). It ends with a full environment-variable reference.

The agent core (coordinator, epoch model, planner) is covered elsewhere. From this layer's point of view the coordinator is a black box with three surfaces: `post_event(event)` to feed it, `get_next_action()` to drain its single outbound queue, and a few helpers (`get_or_create_session`, `set_tts`, `sessions`, `asr_processor`, `trace_logger`, `tts_backend`).

## 1. The server (`agent/server.py`)

### Purpose

The server turns the in-process coordinator into a network service. It owns four jobs: (1) accept WebSocket connections and translate client JSON/binary messages into typed events; (2) fan the coordinator's outbound actions back out to the right session's sockets; (3) apply the abuse-protection rules from `agent/security.py` and `agent/settings.py`; (4) serve the built React UI (or an inline fallback page) and a handful of operational HTTP endpoints.

### How it works: startup and lifecycle

1. At import time `agent/server.py` builds a single module-level `coordinator = AgentCoordinator(trace_logger=TraceLogger(log_file=TRACE_LOG_PATH, strict=TRACE_STRICT == "1"), tts_backend=get_tts_backend())`. Live sessions default to non-strict tracing: a malformed trace record is dropped and counted (`trace_dropped_records`) rather than killing the session. `get_tts_backend()` returns `None` when Piper or the voice file is missing; the UI then hides the speaker toggle.
2. `app = FastAPI(title="Kairos · Καιρός", version="1.0.0", lifespan=lifespan)`. If `ALLOWED_ORIGINS` is non-empty, `CORSMiddleware` is added with that list (or `*`), methods `GET`/`POST`, headers `Authorization` and `Content-Type`. With an empty list no CORS middleware is installed at all.
3. The `lifespan` context manager runs on boot: it configures logging (`setup_logging(cfg.log_format, cfg.log_level)`), logs `Settings.describe()` (auth token masked) and every string from `Settings.warnings()`, clears the subscriber table, starts the dispatcher task `_dispatch_actions_loop`, calls `coordinator.start()`, and launches `run_full_warmup(coordinator, include_llm=WARMUP_LLM == "1")` as a background task so startup is not delayed. On shutdown it cancels the warm-up and dispatcher tasks and awaits `coordinator.stop()`.
4. At the very end of the module, if `frontend/dist` exists it is mounted at `/` with `StaticFiles(html=True)`; otherwise a `GET /` route returns the inline `DEMO_HTML` page (a three-panel demo: conversation, state snapshot, trace timeline, using the Tailwind CDN). Because the static mount is added after all API routes, the API routes win.

### How it works: the action fan-out

The coordinator has ONE outbound action queue shared by every session. A single dispatcher task (`_dispatch_actions_loop`) pulls each action and looks up `_subscribers[action.session_id]`, a set of per-connection `asyncio.Queue` inboxes (`INBOX_MAX = 2000`). Each connection runs a `forward_actions` task that reads its own inbox and sends `action.model_dump_json()` over the socket. If an inbox is full, the action is dropped and a "Slow client" warning is logged; the coordinator is never blocked by a slow client. The code comments record why this design exists: an earlier version had every connection read the shared queue and discard other sessions' actions, so a second tab or stale socket randomly stole actions such as the acknowledgement filler. Two tabs on the same session now each receive every action (`tests/test_server_routing.py`).

### HTTP routes

| Route | Method | Auth | Behaviour |
|---|---|---|---|
| `/health` | GET | public, but minimal without a token | Always returns `{"status": "ok", "auth_required": bool}`. If `AUTH_TOKEN` is set and the caller has no valid token, that is all it returns (load balancers learn only that the process is up). With a valid token (or auth disabled) it adds `sessions`, `asr` (from `asr_processor.info()`), `warmup` (the last `warmup_report`), `tts` (`available`, `backend`) and `trace_dropped_records`. |
| `/warmup` | POST | bearer token if configured | Runs `run_full_warmup(coordinator, include_llm=True)` and returns per-stage timings. It spends LLM tokens, so it is rate limited: if called again within `WARMUP_MIN_INTERVAL_S` (default 30 s) it returns 429 with `{"error": "warmup was run recently", "retry_after_s": ...}` and a `Retry-After` header. |
| `/metrics` | GET | bearer token if configured | Prometheus text exposition (`text/plain; version=0.0.4`). Returns 404 `metrics disabled` when `METRICS_ENABLED` is false. Refreshes the `agent_sessions` and `agent_trace_dropped_records` gauges at scrape time. |
| `/auth/check` | GET | bearer token | Returns `{"ok": true}`. The middleware answers 401 if the token is wrong, so the sign-in screen can verify a key before storing it. |
| `/exports/{name}` | GET | bearer token | Downloads a file written by the `export_artifact` tool. `exporter.resolve_export(name)` rejects any name that differs from its basename, starts with a dot, is not a regular file, or resolves outside the export directory; a miss returns 404 `{"error": "not found"}`. Served as `application/octet-stream` with an attachment `Content-Disposition`. |
| `/` and static files | GET | public | The compiled React UI from `frontend/dist`, else the inline demo page. |
| `/ws/{session_id}` | WebSocket | see below | The agent protocol. |

The protected set is defined by two constants: `PROTECTED_HTTP = ("/warmup", "/metrics", "/auth/check")` and `PROTECTED_PREFIXES = ("/exports/",)`.

### The HTTP middleware

`protect_and_tag` wraps every HTTP request:

1. It takes `X-Request-ID` if it is alphanumeric and at most 64 characters, otherwise generates a 16-hex-character id, and stores it in `request_id_var` (a context variable that the JSON logger includes in every line).
2. For protected paths it calls `security.token_ok(security.bearer_token(headers, query_params), cfg)`. On failure it increments `agent_rejected_total{reason="auth"}` and returns 401 `{"error": "unauthorized"}` with `WWW-Authenticate: Bearer`.
3. On every response it sets `X-Request-ID`, and (via `setdefault`) `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`.

Note that FastAPI HTTP middleware does not run for WebSocket handshakes, so the WebSocket route repeats its own checks (below).

### The WebSocket handshake

`websocket_endpoint(websocket, session_id)` runs these checks BEFORE `accept()`, cheapest and most clearly hostile first. The first one that fails decides the refusal reason:

1. `session_id` must match `^[A-Za-z0-9_-]{1,64}$` (`security.valid_session_id`), reason `session_id`.
2. Origin check (`security.origin_allowed`), reason `origin`.
3. Token check (`security.token_ok` on the `Authorization: Bearer` header or `?token=` query parameter), reason `auth`.
4. If the session id is new and `len(coordinator.sessions) >= MAX_SESSIONS`, reason `session_limit`. A reconnect to an existing session is still allowed at the cap.
5. Per-IP concurrent connection cap (`ConnectionLimiter.acquire(ip)`), reason `ip_limit`.

A refusal increments `agent_rejected_total{reason=...}`, logs a warning and closes with code 1013 ("try again later") for `session_limit` and `ip_limit`, or 1008 (policy violation) for the rest. Because the close happens before accept, a browser sees a failed handshake and no session object is created. The client IP comes from `security.client_ip`, which only honours `X-Forwarded-For` when `TRUST_PROXY=1`.

After accept: `agent_ws_connections` is incremented, the logging context variables (`session_id_var`, `epoch_var`) are set, the session is created or fetched, two token buckets are created (`msg_bucket` for text, `audio_bucket` for binary), the connection's inbox is registered in `_subscribers`, and the forwarder task starts.

### Client to server messages

Every text frame is JSON; binary frames are audio. Per frame, in order: binary frames first pass the audio token bucket; text frames pass the size limit (`MAX_TEXT_MESSAGE_BYTES`), the message token bucket, then JSON parsing. A frame that is valid JSON but not an object is silently ignored. The `type` field defaults to `user_text` when missing.

| Message | Fields | What the server does |
|---|---|---|
| `user_text` (default; also any unknown `type`) | `text` (string, at most `MAX_USER_TEXT_CHARS`) | Posts `UserTextEvent(session_id, text)`. A non-string or over-long `text` is rejected with `too_large`. |
| `interrupt` | `reason` (optional, default `ui_barge_in`) | Posts `InterruptSignalEvent(session_id, reason)`. |
| `tts` | `enabled` (bool) | Calls `coordinator.set_tts(session_id, enabled)` and replies directly with `{"type": "tts_status", "enabled": <bool>, "available": <tts backend present>}`. |
| `voice_stream` | `action`: `start` or `stop` | Sets the connection's `voice_streaming` flag and posts `AudioChunkEvent(streaming=True, stream_control=action, format="pcm_16khz")`. Other `action` values are ignored. |
| `video_frame` | `data` (base64, at most 3,000,000 chars), `mime` (default `image/jpeg`), `source` (default `camera`; also `screen`), `width`, `height` | Base64 is decoded with `validate=True`; if it decodes to non-empty bytes a `VideoFrameEvent` is posted. The coordinator re-validates size and mime. Oversized or invalid base64 is silently dropped. |
| `audio_chunk` | `audio_base64`, `format` (default `webm`), `is_final` (default true) | Legacy JSON audio path: decodes and posts an `AudioChunkEvent`. |
| binary frame, not streaming | raw bytes | Treated as one complete push-to-talk recording: `AudioChunkEvent(format="webm", is_final=True)`. |
| binary frame, after `voice_stream` start | raw 16 kHz mono PCM16 | `AudioChunkEvent(format="pcm_16khz", streaming=True)`; feeds the server-side VAD and ASR. |

Binary frames larger than `MAX_BINARY_FRAME_BYTES` (10 MiB, about five minutes of streaming PCM) are logged and dropped; the code comment is honest that Starlette has already buffered the frame by then, so this protects the ASR buffers rather than the socket.

### Server to client messages

Two kinds exist: control replies sent directly by the endpoint, and actions forwarded from the coordinator.

Control replies:

| Message | Fields | When |
|---|---|---|
| `error` | `type: "error"`, `code` | A client message was dropped. `code` is `rate_limit`, `too_large` or `bad_json`. |
| `tts_status` | `enabled`, `available` | In reply to a `tts` message. |

Actions (JSON dump of the pydantic classes in `agent/schemas/actions.py`; every one carries `action_id`, `session_id`, `action_type`, `epoch`, `timestamp`, `payload`):

| `action_type` | Extra fields | Meaning |
|---|---|---|
| `filler` | `text` | Tier 2 acknowledgement ("on it"). |
| `spoken_response` | `text`, `is_final` | The agent's reply. |
| `clarification` | `question`, `target_slot` | The agent needs more information (also used for LLM failure replies). |
| `tool_call` | `call_id`, `tool_name`, `arguments`, `is_state_modifying`, `idempotency_key` | A tool call was dispatched. |
| `tool_cancel` | `call_id`, `tool_name`, `reason` (default `epoch_stale`) | An in-flight call was cancelled. |
| `state_snapshot` | `intent`, `slots`, `in_flight_calls` (list of `call_id`, `tool`, `epoch`, `status`), `last_updated` | Current session state. |
| `agent_step` | `call_id`, `name`, `role`, `step`, `total_steps`, `thought`, `status`, `artifact` | Progress of a multi-step worker agent. |
| `graph_update` | `nodes` (`id`, `node_type`, `label`, `data`), `edges` (`source`, `target`, `edge_type`), `op` (`append` or `full`) | Memory graph delta or full resync. |
| `transcript` | `text`, `asr_model`, `latency_ms`, `is_partial`, `utterance_id` | What the ASR heard (partials refine one utterance until a final one). |
| `voice_activity` | `state` (`listening`, `speech_start`, `speech_end`, `barge_in`, `idle`), `utterance_id`, `detail` | Server-side VAD state for the UI. |
| `audio_out` | `utterance_id`, `seq`, `text`, `sample_rate`, `duration_ms`, `audio_b64`, `is_last` | One sentence of TTS audio (PCM16 mono, base64) to play in `seq` order. |
| `speech_state` | `state` (`started`, `ducked`, `resumed`, `finished`, `stopped`), `utterance_id`, `reason`, `text`, `spoken_text`, `spoken_ms` | Lifecycle of a spoken reply; `stopped` carries what was actually heard. |
| `file_exported` | `call_id`, `filename`, `path`, `bytes`, `language`, `editor_uri`, `download_path`, `opened_with`, `preview` | `export_artifact` wrote a file. |

The Android client parses fixtures generated from these same pydantic classes (`scripts/dump_android_fixtures.py`, checked by `tests/test_android_fixtures.py`), so changing this table means changing the Kotlin side too.

### Rejection and disconnect policy

`reject(code)` counts a violation, increments `agent_rejected_total{reason=code}`, sends the `error` message and returns True once the connection has accumulated `VIOLATIONS_BEFORE_DISCONNECT = 50` violations. At that point the server closes with 1008 (rate limit, bad JSON) or 1009 (`too_large`). Below the threshold the message is simply dropped and the connection continues, so a single oversized message does not kill the session. In the `finally` block a connection that vanished mid-voice-stream posts a synthetic `voice_stream stop` event so the voice runtime is freed, the forwarder is cancelled, the gauge is decremented, the IP slot is released and the inbox is removed (the `_subscribers` entry is deleted when empty).

### Key types and functions

| Name | File | Role |
|---|---|---|
| `coordinator` | `agent/server.py` | Module-level `AgentCoordinator` shared by all sessions. |
| `lifespan` | `agent/server.py` | Logging setup, dispatcher, coordinator start/stop, background warm-up. |
| `_dispatch_actions_loop` | `agent/server.py` | Fans the single outbound queue into per-connection inboxes. |
| `_subscribers`, `INBOX_MAX` | `agent/server.py` | `session_id -> set of asyncio.Queue`; inbox capped at 2000. |
| `protect_and_tag` | `agent/server.py` | HTTP middleware: request id, bearer gate, security headers. |
| `websocket_endpoint` | `agent/server.py` | Handshake checks and the receive loop. |
| `_conn_limiter`, `reset_runtime_state` | `agent/server.py` | Lazily builds the `ConnectionLimiter`; test/reload reset. |
| `DEMO_HTML` | `agent/server.py` | Fallback single-page UI when `frontend/dist` is absent. |

### Worked example: a refused cross-site socket, then a good one

A page at `https://evil.example` opens `wss://agent.example.com/ws/demo_1`. The browser sends `Origin: https://evil.example`. With `ALLOWED_ORIGINS` empty, `origin_allowed` parses the origin, finds the hostname is not local and not equal to the `Host` header, and returns False. The server increments `agent_rejected_total{reason="origin"}`, logs "Refused WebSocket ... origin" and closes with 1008 before accepting. Now the real UI at `https://agent.example.com` connects with `?token=...`: origin host equals request host, `token_ok` passes via `hmac.compare_digest`, the session cap and IP cap pass, the socket is accepted. The user types "book a flight to Delhi": the client sends `{"type":"user_text","text":"book a flight to Delhi"}`; the server posts a `UserTextEvent`; the coordinator emits a `filler`, `tool_call` and `state_snapshot` actions; the dispatcher routes each to this session's inbox and the forwarder sends them as JSON. The user then clicks Interrupt: `{"type":"interrupt"}` becomes an `InterruptSignalEvent` with reason `ui_barge_in`.

### Failure modes and guarantees

- Slow clients lose actions (logged) but never stall other sessions or the coordinator.
- A malformed trace record in live mode is dropped, counted and visible in `/health` and `/metrics`.
- Exceptions in the forwarder end only that connection's forwarder.
- Malformed client payloads: a bad base64 `audio_chunk` is answered with `{"type":"error","code":"bad_payload"}` (counted as a violation like any other rejected message), and non-numeric `width`/`height` on a `video_frame` are coerced to 0 by `_int_or_zero`. Neither ends the connection. Covered by `tests/test_hardening.py`.
- Rate-limit tokens are consumed before the oversized-binary check, so an oversized frame also costs budget.
- `server.py` has a duplicated `import os` and the inline fallback page loads Tailwind from a CDN (it needs internet; the built React UI does not).

### Tests that cover it

- `tests/test_server.py`: `/health` and the demo UI.
- `tests/test_hardening.py`: hostile session ids, cross-site and allow-listed origins, auth on WebSocket and HTTP, per-IP cap and release, global session cap, message flood throttling then disconnect, oversized and malformed messages, audio flood, warm-up rate limit, request id and security headers, `/metrics` format and disable switch.
- `tests/test_server_routing.py`: idle second connections do not steal actions; two tabs on one session both receive everything; closing one connection does not disturb others.
- `tests/test_voice_websocket.py`: streaming PCM over the socket yields activity, partials and a final; binary without `voice_stream start` is legacy WebM; a mid-stream disconnect frees the voice runtime.
- `tests/test_session_eviction.py`: idle-session eviction (the memory-side companion to the session cap).

## 2. Request protection (`agent/security.py`)

### Purpose

A small module of pure functions and two stateful helpers, deliberately free of any server dependency so every rule can be unit tested directly.

### How it works

- `valid_session_id(session_id, settings)`: matches the compiled `session_id_pattern`. This keeps path-like or enormous ids out of the session table and logs.
- `origin_allowed(origin, host, settings)`: browsers always send `Origin` on WebSocket handshakes; scripts usually do not. Decision order: no Origin means allowed (AUTH_TOKEN is the control for non-browser clients); `*` in the allow-list means allowed; exact match in the list means allowed; a non-empty list that did not match means refused (an explicit list is exhaustive); otherwise (default policy) allowed if the Origin hostname is in `LOCAL_HOSTS` (`localhost`, `127.0.0.1`, `::1`, `[::1]`) or equals the request `Host` hostname (port stripped), which supports a same-origin deployment where this server also serves the UI.
- `bearer_token(headers, query)`: takes `Authorization: Bearer <t>` if present, else the `token` query parameter. The query form exists because browsers cannot set headers on a WebSocket.
- `token_ok(provided, settings)`: True when no `AUTH_TOKEN` is configured, else a constant-time `hmac.compare_digest` on the encoded bytes.
- `client_ip(peer, headers, settings)`: the peer address, or the left-most `X-Forwarded-For` entry only when `TRUST_PROXY=1`. The docstring explains why: trusting that header on a directly exposed server would let any client choose its own IP and dodge per-IP limits.
- `TokenBucket(rate, burst)`: classic bucket initialised full; `allow(cost)` refills by elapsed time (using `clock.monotonic()`, so it works under the virtual clock) and deducts `cost`. Used with cost 1 per text message and cost `len(bytes)` per audio frame.
- `ConnectionLimiter(limit)`: counts open connections per IP; `acquire`, `release` (never goes negative), `count`.

### Key types/functions

| Name | File | Role |
|---|---|---|
| `valid_session_id` | `agent/security.py` | Session id format gate. |
| `origin_allowed` | `agent/security.py` | WebSocket origin policy. |
| `bearer_token`, `token_ok` | `agent/security.py` | Token extraction and constant-time comparison. |
| `client_ip` | `agent/security.py` | Real client address, proxy-aware only on request. |
| `TokenBucket` | `agent/security.py` | Per-connection message and audio-byte rate limit. |
| `ConnectionLimiter` | `agent/security.py` | Per-IP concurrent connection cap. |

### Configuration

All limits come from `Settings` (next section): `AUTH_TOKEN`, `ALLOWED_ORIGINS`, `TRUST_PROXY`, `MAX_CONNECTIONS_PER_IP`, `MSG_RATE_PER_S`, `MSG_BURST`, `AUDIO_BYTES_PER_S`, `AUDIO_BURST_BYTES`.

### Interactions

`agent/server.py` is the only caller. `agent/clock.py` supplies the time source for the bucket. `agent/metrics.py` records rejections (`agent_rejected_total` with reasons `origin`, `auth`, `session_id`, `ip_limit`, `session_limit`, `rate_limit`, `too_large`; the `bad_json` code is also incremented by `reject`).

### Failure modes and guarantees

- With no `AUTH_TOKEN` everything is open; `Settings.warnings()` says so at startup.
- The default origin rule trusts the `Host` header. Behind a proxy that rewrites Host, set `ALLOWED_ORIGINS` explicitly.
- `ALLOWED_ORIGINS=*` disables the check entirely and logs a warning about cross-site WebSocket hijacking.
- The bucket is per connection, so a client can multiply its allowance by opening connections; the per-IP cap (default 20) bounds that. With `TRUST_PROXY=0` behind a proxy all users share the proxy's IP, so the cap becomes global; that is why compose passes `TRUST_PROXY`.

### Worked example

Token bucket for audio with defaults (`AUDIO_BYTES_PER_S=256000`, `AUDIO_BURST_BYTES=1000000`): a client streaming 16 kHz PCM16 sends 32,000 bytes per second, well under the sustained rate. A flooding client sending 1 MB/s drains the 1,000,000-byte burst in roughly 1.3 seconds (burst plus refill), after which each frame fails `allow(len(bytes))`, gets an `error` message with `code: "rate_limit"`, and after 50 such violations the socket is closed with 1008.

### Tests

`tests/test_hardening.py` covers session id validation, the origin policy table, token extraction and comparison, proxy header trust, bucket refill, limiter counting, and the end-to-end refusal paths.

## 3. Settings (`agent/settings.py`)

### Purpose

One documented dataclass, `Settings`, for the security, limits and observability knobs an operator needs to expose the server safely. Model and runtime knobs that predate it (`LLM_*`, `WHISPER_*`, `VOICE_*`, `VISION_*`, `TTS_*`, `INTENT_*`) are still read where they are used.

### How it works

`Settings.from_env()` reads the environment through four small parsers: `_bool` (true for `1`, `true`, `yes`, `on`), `_int`, `_float` (both fall back to the default on garbage input rather than crashing), and `_list` (comma-separated, trimmed). `get_settings()` caches a singleton; `reload_settings()` re-reads the environment (used by tests). `describe()` returns every field for the startup log with `auth_token` replaced by `<set>` or `<unset>`. `warnings()` returns operator warnings for a missing `AUTH_TOKEN` and for `ALLOWED_ORIGINS=*`. `session_id_pattern` is a field (`^[A-Za-z0-9_-]{1,64}$`) with no environment override.

Note that `server.py` calls `get_settings()` at import for the CORS decision, and per request for everything else; `reload_settings()` therefore takes effect for limits and auth but not for the CORS middleware, which is installed once.

### Key types/functions

| Name | File | Role |
|---|---|---|
| `Settings` | `agent/settings.py` | Dataclass of all deployment knobs with defaults. |
| `Settings.from_env` | `agent/settings.py` | Builds settings from the environment, tolerant of bad values. |
| `Settings.describe`, `Settings.warnings` | `agent/settings.py` | Masked startup log; operator warnings. |
| `get_settings`, `reload_settings` | `agent/settings.py` | Cached accessor; forced reload. |

### Configuration

See the reference table in section 8 for every variable and default.

### Interactions

Read by `agent/server.py` and `agent/security.py`. `tests/test_deploy_files.py::test_every_setting_is_documented_in_env_example` enforces that each Settings variable appears in `.env.example`.

### Failure modes and guarantees

Garbage numeric values silently revert to defaults (tested: "ignore garbage"). An empty `AUTH_TOKEN` counts as unset. The auth token is never printed.

### Tests

`tests/test_hardening.py` (defaults conservative, parse env and ignore garbage, describe never leaks the token) and `tests/test_deploy_files.py`.

## 4. The `kairos` launcher (`agent/launcher.py`)

### Purpose

A one-step entry point so a first run needs no configuration. `pyproject.toml` registers it as `kairos = "agent.launcher:main"`; the desktop shortcut runs `python -m agent.launcher`.

### How it works

1. `parse_args` defines `--host` (default `127.0.0.1`), `--port` (8000), `--no-open`, `--browser-tab`, `--login`, `--no-auth`, `--install-shortcut`.
2. `--install-shortcut` calls `install_shortcut()`: copies `frontend/public/kairos.svg` into `~/.local/share/icons/hicolor/scalable/apps/` and writes `~/.local/share/applications/kairos.desktop` (from `desktop_entry`, with `Exec=<python> -m agent.launcher`, `Terminal=false`), then exits. It raises `SystemExit` on non-Linux platforms.
3. Otherwise `.env` is loaded from the repository root and the current directory if `python-dotenv` is importable. A note is printed to stderr if `frontend/dist` is missing (the API still works).
4. The URL is computed (`0.0.0.0` and `::` are shown as `127.0.0.1`). `health(url)` GETs `/health`; if something already answers, the launcher prints the banner with "Using the instance already running at", opens the UI unless `--no-open`, and exits 0. Otherwise `port_free(host, port)` is checked by binding a socket; if the port is taken by something else it prints `Try: kairos --port N+1` and returns 2.
5. `decide_auth(host, force_login, no_auth, env_token)` chooses the access key:
   1. an explicit `AUTH_TOKEN` always wins;
   2. `--no-auth` on a loopback host means no key;
   3. `--login` or any non-loopback host means a key from `load_or_create_key()`;
   4. otherwise no key (loopback, open, it is your machine).
6. `load_or_create_key()` stores a `secrets.token_urlsafe(18)` key in `$XDG_CONFIG_HOME/kairos/key` (default `~/.config/kairos/key`) with mode 0600 and reuses it, so restarts and the desktop shortcut keep working.
7. If a key was chosen it is exported as `AUTH_TOKEN`; `EXPORT_DIR` defaults to `~/kairos-exports`. The banner shows the URL, the key and its file, the brain (`llm_description()`: Groq plus model name, OpenRouter, a local server, or a warning that canned demo replies will be used), and the export directory.
8. A daemon thread `opener` waits up to 90 s for `/health` (`wait_until_ready`) and then calls `open_app`. `open_app` prefers a Chromium-family browser (`google-chrome-stable`, `google-chrome`, `chromium`, `chromium-browser`, `microsoft-edge`, `brave-browser`) in app mode (`--app=URL`, its own profile directory `~/.config/kairos/browser-profile`, window 1280x860), falling back to `webbrowser.open`. Setting `KAIROS_FAKE_MEDIA` adds `--use-fake-ui-for-media-stream` (used by demo recording).
9. `uvicorn.Server(uvicorn.Config("agent.server:app", host, port, log_level="warning")).run()` runs in the main thread until Ctrl+C.

### Key types/functions

| Name | File | Role |
|---|---|---|
| `main`, `parse_args` | `agent/launcher.py` | Entry point and CLI. |
| `decide_auth`, `load_or_create_key`, `config_dir` | `agent/launcher.py` | Auth policy by host; persistent 0600 key. |
| `health`, `wait_until_ready`, `port_free` | `agent/launcher.py` | Reuse an existing instance; detect port conflicts; wait for boot. |
| `find_browser`, `open_app` | `agent/launcher.py` | Standalone window or default browser. |
| `install_shortcut`, `desktop_entry` | `agent/launcher.py` | Linux launcher entry. |
| `banner`, `llm_description` | `agent/launcher.py` | Startup summary. |

### Configuration

Flags above; environment: `AUTH_TOKEN`, `EXPORT_DIR`, `XDG_CONFIG_HOME`, `KAIROS_FAKE_MEDIA`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `USE_LOCAL_LLM`, `LLM_MODEL_NAME` (the last four only for the banner text).

### Interactions

Starts `agent.server:app` in-process. When it reuses a running instance, it prints no key (it does not know it) and cannot tell whether that instance requires one.

### Failure modes and guarantees

- Never exposes an open server beyond loopback: `--no-auth` is honoured only on loopback hosts.
- `--host 0.0.0.0` generates and requires a key automatically.
- Port in use by a non-Kairos process exits with code 2 and a hint.
- Missing browser falls back to `webbrowser.open`; an `OSError` launching Chromium also falls back.
- The reuse check treats any process answering `/health` as Kairos.

### Worked example

`kairos --host 0.0.0.0` on a LAN machine: no `AUTH_TOKEN` in the environment, host is not loopback, so `decide_auth` returns the persisted key (created on first run). The key is exported as `AUTH_TOKEN`, so `Settings` sees it, `/ws`, `/warmup`, `/metrics`, `/auth/check` and `/exports/` demand it, and `/health` becomes minimal for unauthenticated callers. The banner prints the key; the UI shows its sign-in screen, which calls `/auth/check` to validate the key before storing it.

### Tests

`tests/test_launcher.py`: the key is created once, private and reused; the auth policy table; explicit token wins; port detection; browser discovery; app-window flags and profile; fallback; readiness and reuse detection; the desktop entry; the banner; and an end-to-end test that starts the real server and checks a second launch reuses it.

## 5. Container and proxy deployment

### Dockerfile

A three-stage build, documented in its header comments:

1. `frontend` (`node:20-slim`): `npm ci` then `npm run build`, producing `frontend/dist`.
2. `python-deps` (`python:3.11-slim`): copies `requirements.lock`, drops lines starting with `nvidia`, `triton` or `cuda` (`grep -viE '^(nvidia|triton|cuda)'`), builds `/opt/venv` and installs with `--extra-index-url https://download.pytorch.org/whl/cpu`. The lock was compiled on a machine whose torch pulls the CUDA stack, and this image is CPU only (the MiniLM model is about 22 million parameters).
3. `runtime` (`python:3.11-slim`): installs `ffmpeg` (decodes push-to-talk WebM) and `libgomp1`, creates non-root user `app` (uid 10001) with `/models` and `/data`, copies the venv, `agent/`, `scripts/` and the built UI.

Runtime environment baked into the image: `HF_HOME=/models/hf`, `TTS_VOICE_PATH=/models/piper/en_US-lessac-medium.onnx`, `LOG_FORMAT=json`, `TRACE_LOG_PATH=/data/trace.jsonl`, `PYTHONUNBUFFERED=1`, `PYTHONPATH=/app`. Build argument `BAKE_MODELS` (default 1) runs `python scripts/fetch_models.py` at build time as the `app` user, and sets `HF_HUB_OFFLINE=${BAKE_MODELS}` so a baked image never contacts the Hub at runtime (air-gap friendly). `--build-arg BAKE_MODELS=0` makes a slim image that downloads at first run. Volumes `/models` and `/data`; `EXPOSE 8000`; a `HEALTHCHECK` every 30 s (5 s timeout, 60 s start period, 3 retries) fetching `/health` with Python's urllib. The command is `uvicorn agent.server:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips *`.

Note on `--forwarded-allow-ips "*"`: this makes uvicorn trust forwarding headers from any peer for scheme and host; it is separate from the application-level `TRUST_PROXY` flag that governs `X-Forwarded-For` for the per-IP limits.

### docker-compose.yml

- Service `app`: `build: .`; `env_file: .env` (optional); environment `LOG_FORMAT: json` and `TRUST_PROXY: ${TRUST_PROXY:-0}`; port `127.0.0.1:8000:8000` (loopback only by default); volumes `models` and `data`; `restart: unless-stopped`, `init: true`, `read_only: true` root filesystem with `/tmp` as tmpfs, `no-new-privileges`.
- Service `caddy` (profile `https`): `caddy:2`, ports 80 and 443, `DOMAIN` (default `localhost`), mounts `./Caddyfile` read-only and a `caddy_data` volume (certificates).
- Usage in the header comments: `docker compose up --build` (app only) and `DOMAIN=agent.example.com docker compose --profile https up --build -d` (adds automatic HTTPS; microphone and camera need a secure origin off localhost).

One consequence of `read_only: true`: anything the app writes must go to `/data`, `/models` or `/tmp`. The export tool defaults to `~/kairos-exports` unless `EXPORT_DIR` is set, so in the container `EXPORT_DIR` should point at a writable volume; the compose file does not set it. I did not verify this path in a running container.

### Caddyfile

Six lines: site address `{$DOMAIN}`, `encode gzip`, `reverse_proxy app:8000`. Caddy obtains certificates automatically, and proxies WebSockets transparently. Its comment reminds the operator to run the app with `TRUST_PROXY=1` so the per-IP limits see real client addresses.

### requirements.lock

Generated by `pip-compile` with Python 3.11 from `pyproject.toml` with the `embeddings` and `tts` extras, fully pinned (85 pinned lines, including `fastapi==0.142.2`, `uvicorn==0.54.0`, `pydantic==2.13.5`, `faster-whisper==1.2.1`, `sentence-transformers==6.1.0`, `torch==2.14.1`, `piper-tts==1.8.0`, `onnxruntime==1.30.0`, `numpy==2.4.6`). It is used by the Docker build only; CI and local installs use `pyproject.toml` (`pip install -e ".[dev,embeddings,tts]"`). `tests/test_deploy_files.py` checks that direct dependencies are pinned and that the Dockerfile's filter removes GPU wheels.

### scripts/fetch_models.py

Downloads everything the server needs so the first request is not the one paying for it, and so an image can be baked for offline use. Steps (a dict `STEPS`): `whisper` (`faster_whisper.utils.download_model`, name from `WHISPER_MODEL`, default `base.en`), `minilm` (`SentenceTransformer("all-MiniLM-L6-v2", device="cpu")`), `tts` (downloads `en_US-lessac-medium.onnx` and `.onnx.json` from the `rhasspy/piper-voices` Hugging Face repo to `TTS_VOICE_PATH`, default `models/piper/` in the repo; files over 1000 bytes that already exist are skipped). `--skip whisper minilm tts` omits steps. Each step is isolated: failures are printed to stderr and the script continues, exiting 1 at the end if any failed. Silero VAD ships inside the faster-whisper wheel and needs no download.

### scripts/eval_trend.py

Prints one line per recorded eval report (default glob `eval_results/*.json`): overall score, the four category scores (task, interrupt, latency, safety), median cancel latency, median first-ack and first-reply times, and the scenarios flagged flaky in variance runs. Reads `summary.overall`, `summary.categories`, `summary.raw.median_cancel_latency_s` / `median_first_ack_s` / `median_first_reply_s` and `variance.flaky`. Unreadable files print an "unreadable" line instead of aborting; exit 1 only when no reports exist. It is the viewer for the reports the nightly workflow publishes.

### Tests

`tests/test_deploy_files.py` validates the CI matrix against `requires-python`, that workflows are valid YAML and gate what they claim, that every Settings variable is in `.env.example`, lock pinning and GPU-wheel filtering, the Dockerfile (multi-stage, non-root, healthcheck), compose (valid, app kept off the public interface by default), ruff config and packaging. This file is skipped if PyYAML is missing.

## 6. CI and nightly workflows (`.github/workflows/`)

### `ci.yml` (on push to `main` and every pull request)

Concurrency group per ref cancels superseded runs. Jobs:

| Job | What it does |
|---|---|
| `lint` | Python 3.11, `pip install ruff`, `ruff check agent tests scripts` (only E9 and F: syntax and pyflakes, per `pyproject.toml`). |
| `secrets` | Full-history checkout and `gitleaks/gitleaks-action@v2`. |
| `test` | Matrix Python 3.10, 3.11, 3.12 (matches `requires-python >=3.10,<3.13`, enforced by a test). Installs `ffmpeg`, caches `~/.cache/huggingface` (key `hf-models-whisper-base-en-v1`), installs `".[dev,embeddings]"` with CPU torch, runs `pytest -q -p no:cacheprovider` (live tests excluded via `addopts = "-m 'not live'"`), then the mock-mode eval gate: `python -m agent.eval --llm mock --virtual --set all --quiet --fail-under 97 --min-scenario 95`. |
| `frontend` | Node 20, `npm ci`, `npm run build` in `frontend/`. |
| `android` | Java 17 (temurin), Android SDK, `./gradlew --no-daemon testDebugUnitTest assembleDebug`, uploads the debug APK as an artifact. |
| `docker` | Needs `lint`, `test`, `frontend`. Builds the image with `BAKE_MODELS=0` (no push) using the GitHub Actions layer cache. |

The eval gate is the project's quality bar: overall at least 97, every scenario at least 95, and (per the workflow comment) zero stale completions, duplicate state changes, invalid payloads, trace violations and false completion claims. Voice scenarios are skipped in virtual time.

### `nightly-live-eval.yml`

Scheduled daily at 03:17 UTC and manually triggerable (`workflow_dispatch` with a `runs` input, default 3). One job, 150-minute timeout: installs the same dependencies, then `python -m agent.eval --llm live --set all --runs N --out eval_results/live_<date>.json`, teeing a text report, and uploads `eval_results/` as the `live-eval-report` artifact (even on failure). It needs repository secrets `GROQ_API_KEY` (and optionally `OPENROUTER_API_KEY`). The comment states a full 3-run pass costs about Groq's 200k tokens/day free-tier allowance, which is why it runs once a day. `scripts/eval_trend.py` reads these artifacts.

### Interactions and failure modes

CI never contacts a real LLM: all gating runs use the mock backend and the virtual clock, so it is deterministic and spends no tokens. The nightly job is informational (nothing gates on it). If the Groq daily cap is exhausted the live run will degrade into clarification replies ("I hit a snag reaching my reasoning engine"), which shows up as low scores in the trend rather than as a build failure.

## 7. Operating notes

- Local development: `uvicorn agent.server:app --host 127.0.0.1 --port 8000`, with `npm run dev` in `frontend/` proxying `/ws` and `/health`.
- Exposure checklist (from README and `Settings.warnings()`): set `AUTH_TOKEN`, set `ALLOWED_ORIGINS` if the UI lives on another origin, set `TRUST_PROXY=1` behind Caddy or nginx, and use HTTPS (browsers require a secure context for microphone and camera).
- Observability: `LOG_FORMAT=json` for shippers, `/metrics` for Prometheus. Series (all `agent_*`): `ws_connections`, `sessions`, `events_total{type}`, `actions_total{type}`, `first_ack_seconds`, `interrupt_cancel_seconds`, `speech_stop_seconds`, `llm_requests_total{outcome}`, `llm_latency_seconds`, `llm_circuit_open`, `llm_soft_deadline_total`, `rejected_total{reason}`, `trace_dropped_records`.
- Session memory: sessions are evicted after `SESSION_TTL_S` of inactivity (default 3600, checked every `SESSION_EVICT_INTERVAL_S`, default 300); sessions with in-flight work, planning or active voice are spared (`tests/test_session_eviction.py`).

## 8. Environment variable reference

Defaults are those in the code. "Module" is the file that reads the variable. Values marked (derived) depend on which API key is set.

### Security, limits and observability

| Variable | Default | Module | Meaning |
|---|---|---|---|
| `AUTH_TOKEN` | unset (open) | `agent/settings.py` | Bearer token for `/ws`, `/warmup`, `/metrics`, `/auth/check`, `/exports/` and the full `/health`. |
| `ALLOWED_ORIGINS` | empty (same host plus localhost) | `agent/settings.py` | Comma-separated WebSocket/CORS origins, or `*`. |
| `TRUST_PROXY` | `0` | `agent/settings.py` | Take client IP from `X-Forwarded-For`. |
| `MAX_CONNECTIONS_PER_IP` | `20` | `agent/settings.py` | Concurrent sockets per IP. |
| `MAX_SESSIONS` | `500` | `agent/settings.py` | New sessions beyond this are refused. |
| `MSG_RATE_PER_S` | `30` | `agent/settings.py` | Sustained text messages per second per connection. |
| `MSG_BURST` | `60` | `agent/settings.py` | Text message burst. |
| `AUDIO_BYTES_PER_S` | `256000` | `agent/settings.py` | Sustained binary bytes per second per connection. |
| `AUDIO_BURST_BYTES` | `1000000` | `agent/settings.py` | Audio burst bytes. |
| `MAX_TEXT_MESSAGE_BYTES` | `4000000` | `agent/settings.py` | Largest accepted text frame (a base64 camera frame). |
| `MAX_USER_TEXT_CHARS` | `8000` | `agent/settings.py` | Largest `user_text`. |
| `WARMUP_MIN_INTERVAL_S` | `30` | `agent/settings.py` | Minimum gap between `POST /warmup` calls. |
| `LOG_FORMAT` | `text` (`json` in Docker) | `agent/settings.py` | `json` or `text`. |
| `LOG_LEVEL` | `INFO` | `agent/settings.py` | Logging level. |
| `METRICS_ENABLED` | `1` | `agent/settings.py` | Enables `/metrics`. |
| `TRACE_LOG_PATH` | unset (no file) | `agent/server.py` | JSONL trace file (`/data/trace.jsonl` in Docker). |
| `TRACE_STRICT` | `0` | `agent/server.py` | `1` raises on invalid trace records instead of dropping. |
| `WARMUP_LLM` | unset | `agent/server.py` | `1` makes the boot warm-up also ping the LLM. |
| `SESSION_TTL_S` | `3600` | `agent/coordinator.py` | Idle session eviction age (0 or less disables). |
| `SESSION_EVICT_INTERVAL_S` | `300` | `agent/coordinator.py` | Eviction sweep period. |

### LLM

| Variable | Default | Module | Meaning |
|---|---|---|---|
| `GROQ_API_KEY` | unset | `agent/llm_client.py`, `agent/launcher.py` | Selects the Groq backend (highest priority). |
| `OPENROUTER_API_KEY` | unset | `agent/llm_client.py`, `agent/multimodal/vision.py`, `agent/launcher.py` | OpenRouter backend; also vision. |
| `OPENAI_API_KEY` | unset | `agent/llm_client.py` | Last-resort API key value. |
| `USE_LOCAL_LLM` | unset | `agent/llm_client.py`, `agent/launcher.py` | Local OpenAI-compatible server. Backend priority is Groq, then OpenRouter, then local, else mock. |
| `LLM_MODEL_NAME` | `openai/gpt-oss-120b` with Groq, else `qwen/qwen-2.5-7b-instruct` (derived) | `agent/llm_client.py` | Model id. |
| `LLM_BASE_URL` | Groq URL, OpenRouter URL, or `http://localhost:8000/v1` (derived) | `agent/llm_client.py` | API base URL. |
| `LLM_TIMEOUT_S` | `8.0` Groq, `5.0` OpenRouter, `2.0` otherwise (derived) | `agent/llm_client.py` | Hard request deadline. |
| `LLM_SOFT_DEADLINE_S` | `2.0` | `agent/llm_client.py` | Progress message threshold; 0 disables. |
| `LLM_RATE_LIMIT_MAX_WAIT_S` | `3.0` | `agent/llm_client.py` | Longest 429 wait that is retried once. |
| `LLM_CIRCUIT_COOLDOWN_S` | `15.0` | `agent/llm_client.py` | Circuit breaker cool-down before a probe. |
| `LLM_FALLBACK_MODEL_NAME` | unset (fallback disabled) | `agent/llm_client.py` | Enables the fallback backend. |
| `LLM_FALLBACK_BACKEND_TYPE` | the default backend type | `agent/llm_client.py` | Fallback backend kind. |
| `LLM_FALLBACK_API_KEY` | falls back to Groq/OpenRouter/OpenAI key | `agent/llm_client.py` | Fallback credentials. |
| `LLM_FALLBACK_BASE_URL` | primary base URL | `agent/llm_client.py` | Fallback endpoint. |
| `LLM_FALLBACK_TIMEOUT_S` | `4.0` | `agent/llm_client.py` | Fallback deadline. |

### Voice, speech and vision

| Variable | Default | Module | Meaning |
|---|---|---|---|
| `WHISPER_MODEL` | `base.en` | `agent/multimodal/asr.py`, `scripts/fetch_models.py` | faster-whisper model. |
| `WHISPER_DEVICE` | `cpu` | `agent/multimodal/asr.py` | Device. |
| `WHISPER_COMPUTE_TYPE` | `int8` | `agent/multimodal/asr.py` | Quantisation. |
| `WHISPER_INITIAL_PROMPT` | built-in `DEFAULT_INITIAL_PROMPT` | `agent/multimodal/asr.py` | Vocabulary bias. |
| `VOICE_VAD_THRESHOLD` | `0.5` | `agent/multimodal/streaming.py` | Speech probability threshold. |
| `VOICE_VAD_SILENCE_THRESHOLD` | `0.35` | `agent/multimodal/streaming.py` | Hysteresis silence threshold. |
| `VOICE_MIN_SPEECH_MS` | `128` | `agent/multimodal/streaming.py` | Speech needed to start an utterance. |
| `VOICE_ENDPOINT_MS` | `500` | `agent/multimodal/streaming.py` | Base silence that ends an utterance. |
| `VOICE_PREROLL_MS` | `320` | `agent/multimodal/streaming.py` | Audio kept before onset. |
| `VOICE_MAX_UTTERANCE_S` | `20` | `agent/multimodal/streaming.py` | Forced endpoint. |
| `VOICE_PARTIAL_INTERVAL_MS` | `500` | `agent/multimodal/streaming.py` | Partial transcript cadence. |
| `VOICE_MIN_PARTIAL_MS` | `600` | `agent/multimodal/streaming.py` | Minimum audio for a partial. |
| `VOICE_TAIL_PARTIAL_MS` | `200` | `agent/multimodal/streaming.py` | Tail partial after silence (0 off). |
| `VOICE_ADAPTIVE_ENDPOINT` | `1` | `agent/coordinator.py` | Adaptive endpointing. |
| `VOICE_FINAL_REUSE_SLACK_MS` | `100` | `agent/coordinator.py` | Slack for reusing the tail partial as final. |
| `VOICE_STOP_ON_SPEECH_START` | `0` | `agent/coordinator.py` | Stop TTS the instant VAD hears speech. |
| `VOICE_SPEAKING_VAD_THRESHOLD` | `0.75` | `agent/coordinator.py` | VAD bar while the agent speaks. |
| `TTS_BACKEND` | `auto` | `agent/multimodal/tts.py` | `auto`, `piper`, `mock` or `off`. |
| `TTS_VOICE_PATH` | `models/piper/en_US-lessac-medium.onnx` (module default) | `agent/multimodal/tts.py`, `scripts/fetch_models.py` | Piper voice file. |
| `VISION_BACKEND` | `openrouter` if a key exists, else `mock` | `agent/multimodal/vision.py` | Vision backend. |
| `VISION_MODELS` | built-in free-model failover list | `agent/multimodal/vision.py` | Comma-separated failover order. |
| `VISION_TIMEOUT_S` | `30` | `agent/multimodal/vision.py` | Vision request deadline. |
| `VISION_MAX_TOKENS` | `1500` | `agent/multimodal/vision.py` | Vision token budget. |
| `INTENT_EMBEDDINGS` | `auto` | `agent/fast_path/intent_classifier.py` | `auto`, `1` or `0`. |
| `INTENT_THREADS` | `2` | `agent/fast_path/intent_classifier.py` | torch threads for MiniLM. |
| `INTENT_DEVICE` | `cpu` | `agent/fast_path/intent_classifier.py` | MiniLM device. |

### Export tool, launcher, deployment

| Variable | Default | Module | Meaning |
|---|---|---|---|
| `EXPORT_DIR` | `~/kairos-exports` | `agent/exporter.py`, `agent/launcher.py` | Where exports are written and served from. The eval runner sets a temp dir. |
| `EXPORT_OPEN` | `auto` | `agent/exporter.py` | Whether to launch the editor (the eval runner sets `0`). |
| `EXPORT_EDITOR_CMD` | `code` | `agent/exporter.py` | Editor command (launched via an argument list). |
| `XDG_CONFIG_HOME` | `~/.config` | `agent/launcher.py` | Location of the `kairos/` config dir (key, browser profile). |
| `KAIROS_FAKE_MEDIA` | unset | `agent/launcher.py` | Adds the fake-media browser flag (demo recording). |
| `DOMAIN` | `localhost` in compose | `docker-compose.yml`, `Caddyfile` | Public hostname for Caddy. |
| `BAKE_MODELS` | `1` (build arg) | `Dockerfile` | Download models at image build and set `HF_HUB_OFFLINE`. |
| `HF_HOME`, `HF_HUB_OFFLINE`, `PYTHONPATH`, `PYTHONUNBUFFERED` | set by the Dockerfile | `Dockerfile` | Hugging Face cache location and offline mode; interpreter settings. |

### Inconsistencies noticed while building this table

- `.env.example` now documents the real voice defaults (`VOICE_ENDPOINT_MS=500`, `VOICE_PARTIAL_INTERVAL_MS=500`), matching `VoiceConfig` in `agent/multimodal/streaming.py`.
- `.env.example` has `LLM_MODEL_NAME=qwen/qwen-2.5-7b-instruct` uncommented. If a user copies it and also sets `GROQ_API_KEY`, this overrides the Groq default `openai/gpt-oss-120b`, which the project notes say is the only Groq model enabled for the org.
- `.env.example` has `OPENROUTER_API_KEY=sk-or-v1-your-key-here` uncommented, so a placeholder key counts as "set" and selects the OpenRouter backend if no Groq key is present.
- `VOICE_VAD_SILENCE_THRESHOLD`, `VOICE_PREROLL_MS`, `VOICE_MIN_PARTIAL_MS`, `SESSION_TTL_S`, `SESSION_EVICT_INTERVAL_S`, `TRACE_LOG_PATH`, `TRACE_STRICT`, `WARMUP_LLM`, `INTENT_THREADS` and the `EXPORT_*` variables are read by code but are not described in `.env.example` (`test_every_setting_is_documented_in_env_example` only covers the `Settings` fields).
