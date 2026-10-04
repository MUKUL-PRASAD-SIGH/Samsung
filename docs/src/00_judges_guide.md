# Kairos · Judges' Guide

## 1. What Kairos is

Kairos is a full-duplex, interruptible real-time agent built for Samsung Hackathon, Theme 05. The user can talk or type, and can interrupt or correct the agent while it is working. Kairos cancels the stale work instead of finishing the wrong thing, and it never reports a cancelled task as done.

The central idea is the **epoch model**. Every session has an epoch counter that only increases. Each tool call is tagged with the epoch it started under. A correction or interrupt bumps the epoch and cancels the in-flight calls, and any result that arrives from an older epoch is dropped. This is what makes interruption safe rather than cosmetic.

## 2. Fastest way to try it (about 5 minutes, no API key needed)

```
./setup.sh              # Linux / macOS     (Windows: setup.bat)
.venv/bin/kairos        # starts the server and opens the UI
```

Without an LLM key Kairos runs in an offline **mock mode**: canned planner replies, but the real coordination logic (epochs, cancellation, idempotency, memory, trace). This is enough to see interruption handling work. Add `GROQ_API_KEY=...` to `.env` for real model reasoning. See the Setup and Quickstart document for every option, including Docker.

## 3. What to look at, in order

1. **Run the self-check.** `.venv/bin/python -m agent.eval --llm mock --virtual --set all --fail-under 97 --min-scenario 95` runs every evaluation scenario in virtual time (seconds, offline). It prints the scorecard and ends with `GATE PASSED` or fails.
2. **Open the UI and interrupt it.** Type "find me flights to Delhi", and while it works send "no, Mumbai". Watch the state panel: the epoch increments, the old call is cancelled, and only the Mumbai result is kept.
3. **Open the trace and memory graph panels** in the UI to see every action and the supersession of the old request.
4. **Ask it to write code.** The `export_artifact` tool saves the result to `~/kairos-exports` and opens your editor.
5. **Read the component documents** listed below for how each part works.

## 4. Document map

| Document | What it covers |
|---|---|
| 01 Setup and Quickstart | Installation paths, configuration, troubleshooting |
| 02 Coordination Core | Epoch model, session state, idempotency, tool router, virtual clock |
| 03 Coordinator Event Flow | The orchestrator: queues, interrupts, tool execution, actions, trace |
| 04 Fast and Slow Path | Tier 1 interrupt classifier, Tier 2 fillers, Tier 3 planner |
| 05 LLM Layer | Backends, circuit breaker, deadlines, fallback, mock mode |
| 06 Memory | Scratchpad, graph memory, context builder |
| 07 Tools and Workers | Every tool and sub-agent worker |
| 08 Voice, Vision and Speech | VAD, Whisper, Piper, barge-in, echo guard, camera |
| 09 Server, Security and Deployment | HTTP and WebSocket protocol, limits, Docker, CI, environment variables |
| 10 Evaluation and Testing | Scenario harness, scoring, CI gate, pytest suite |
| 11 Web Frontend | React UI |
| 12 Android App | Kotlin / Compose client |
| 13 Demo Tooling | How the demo video was produced |
| 14 Desktop App | The Windows .exe: installer, API-key screen, packaging |

## 5. Architecture at a glance

```
 Web UI / Android ---- WebSocket /ws/{session} ----+
                                                   v
   user text / audio / interrupt / tool result --> EVENT QUEUE (per session)
                                                   |
   Tier 1  interrupt classifier (fast, local) -----+
   Tier 2  fillers and acknowledgements (templates)|
   Tier 3  planner --> LLM (Groq / OpenRouter / local / mock)
                          |
                    tool router --> tools / workers
                          |
        SessionState (epoch, slots, in-flight calls)   Memory (scratchpad + graph)
                          |
                    ACTION QUEUE --> TraceLogger --> WebSocket --> UI
```

## 6. Honest notes for judges

- Travel, weather and timer tools return canned data after a short delay. They exist to exercise the coordination logic, not to talk to real booking systems. See the Tools and Workers document.
- The mock LLM mode is deterministic and offline. The quality gate in CI runs in mock mode. Live-LLM evaluation spends tokens and is limited by the provider's daily cap (the Groq model in use has 200k tokens per day).
- Voice (microphone) requires a secure context in the browser: `localhost` works, other hosts need HTTPS.
- The component documents note, where relevant, places where the code and the older README disagree. Those notes are deliberate.
