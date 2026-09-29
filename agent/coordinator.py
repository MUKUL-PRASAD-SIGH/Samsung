"""Core Agent Coordinator: Orchestrates Event Queue, Coordination Layer, and Action Queue.

Manages:
- Dual asynchronous queues (inbound Event Queue, outbound Action Queue)
- Monotonic session epochs and immediate cancellation of stale calls (§2.1)
- State snapshots (§2.3) and trace logging (§7.6)
- Background task execution and lifecycle management
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Deque, Dict, List, Optional, Set, Tuple

from agent.schemas.events import (
    BaseEvent,
    EventType,
    UserTextEvent,
    AudioChunkEvent,
    InterruptSignalEvent,
    ToolResultEvent,
    VideoFrameEvent,
)
from agent.schemas.actions import (
    BaseAction,
    FillerAction,
    SpokenResponseAction,
    ToolCallAction,
    ToolCancelAction,
    StateSnapshotAction,
    GraphUpdateAction,
    TranscriptAction,
    VoiceActivityAction,
    GraphNodePayload,
    GraphEdgePayload,
)
from agent.coordination.state_machine import SessionState
from agent.coordination.tool_router import ToolRouter
from agent.trace_logger import TraceLogger
from agent.tool_summaries import summarize_tool_result, summarize_tool_error
from agent.fast_path.templates import generate_filler
from agent.fast_path.intent_classifier import IntentClassifier
from agent.slow_path.planner import Planner
from agent.llm_client import MockLLMBackend, LLMBackend, get_backend, LLMConfig
from agent.multimodal.asr import ASRProcessor
from agent.multimodal.vision import ALLOWED_MIME, MAX_FRAME_BYTES, VisionBackend, get_vision_backend
from agent.multimodal.streaming import (
    PartialDue,
    SpeechStart,
    UtteranceEnd,
    VoiceStream,
    make_vad,
)

logger = logging.getLogger("agent.coordinator")


FRAMES_PER_SESSION = 3      # ring buffer: only the newest frames matter, and nothing is analyzed until asked
MAX_FRAME_AGE_S = 15.0      # a frame older than this means sharing stopped: don't answer from a stale image

# Resource bounds (gap #8): sessions are in-memory only and were never evicted, so a long-running
# server accumulated one SessionState (plus its idempotency store and memory graph) per page load
# forever. <= 0 disables eviction, e.g. for tests that assert on session count across time.
SESSION_TTL_S = float(os.getenv("SESSION_TTL_S", "3600"))
SESSION_EVICT_INTERVAL_S = float(os.getenv("SESSION_EVICT_INTERVAL_S", "300"))


@dataclass
class _Frame:
    frame_id: str
    mime: str
    data: bytes
    source: str
    ts: float


@dataclass
class _VoiceRuntime:
    """Per-session state for continuous voice streaming."""

    stream: VoiceStream
    partial_task: Optional[asyncio.Task] = None
    final_tail: Optional[asyncio.Task] = None  # serializes finals so utterances reach the planner in order
    barge_in_fired: Set[str] = field(default_factory=set)
    finalized: Set[str] = field(default_factory=set)
    tasks: Set[asyncio.Task] = field(default_factory=set)


class AgentCoordinator:
    def __init__(
        self,
        tool_router: Optional[ToolRouter] = None,
        trace_logger: Optional[TraceLogger] = None,
        llm_backend: Optional[LLMBackend] = None,
        intent_classifier: Optional[IntentClassifier] = None,
        asr_processor: Optional[ASRProcessor] = None,
        vision_backend: Optional[VisionBackend] = None,
        enable_debounce: bool = True,
        debounce_window_s: float = 0.10,
    ):
        self.event_queue: asyncio.Queue[BaseEvent] = asyncio.Queue()
        self.action_queue: asyncio.Queue[BaseAction] = asyncio.Queue()
        self.sessions: Dict[str, SessionState] = {}
        self.tool_router = tool_router or ToolRouter()
        self.trace_logger = trace_logger or TraceLogger()
        self.enable_debounce = enable_debounce
        self.debounce_window_s = debounce_window_s

        self.intent_classifier = intent_classifier or IntentClassifier(use_embeddings=False)
        self.llm_backend = llm_backend or get_backend(LLMConfig())
        self.planner = Planner(llm_backend=self.llm_backend, tool_router=self.tool_router)
        self.asr_processor = asr_processor or ASRProcessor()
        self.vision_backend = vision_backend or get_vision_backend()

        self._debounce_tasks: Dict[str, asyncio.Task] = {}
        self._pending_events: Dict[str, List[UserTextEvent]] = {}
        self._audio_buffers: Dict[str, bytearray] = {}
        self._voice: Dict[str, _VoiceRuntime] = {}
        self._frames: Dict[str, Deque[_Frame]] = {}   # session_id -> newest camera/screen frames
        self._call_origin: Dict[str, str] = {}        # call_id -> the user request that spawned it
        self._turn_tasks: Set[asyncio.Task] = set()  # in-progress planning turns (debounced text)
        self._active_plans: Dict[str, int] = {}      # session_id -> planner calls currently awaiting the LLM
        self._running = False
        self._loop_task: Optional[asyncio.Task] = None
        self._evict_task: Optional[asyncio.Task] = None

    def get_or_create_session(self, session_id: str) -> SessionState:
        """Retrieve existing session or instantiate a new SessionState."""
        if session_id not in self.sessions:
            session = SessionState(session_id=session_id)
            session.ensure_memory()
            self.sessions[session_id] = session
        return self.sessions[session_id]

    def evict_idle_sessions(self, now: Optional[float] = None) -> List[str]:
        """Drop sessions idle longer than SESSION_TTL_S, freeing their idempotency store and
        memory graph (gap #8). A session with in-flight work is never evicted, however idle its
        last *event* looked, so a slow tool call can't have its own session vanish underneath it.
        """
        if SESSION_TTL_S <= 0:
            return []
        now = now if now is not None else time.time()
        stale = [
            sid for sid, session in self.sessions.items()
            if (now - session.last_activity) > SESSION_TTL_S
            and not any(c.status in ("pending", "running") for c in session.in_flight_calls.values())
        ]
        for sid in stale:
            del self.sessions[sid]
            self._pending_events.pop(sid, None)
            self._audio_buffers.pop(sid, None)
            self._frames.pop(sid, None)
            self._voice.pop(sid, None)
            self._active_plans.pop(sid, None)
        if stale:
            logger.info("Evicted %d idle session(s): %s", len(stale), stale)
        return stale

    async def _evict_idle_sessions_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(SESSION_EVICT_INTERVAL_S)
                self.evict_idle_sessions()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Session eviction pass failed")

    async def start(self) -> None:
        """Start the background event processing loop."""
        if self._running:
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._process_events())
        self._evict_task = asyncio.create_task(self._evict_idle_sessions_loop())

    async def stop(self) -> None:
        """Stop processing and cancel remaining tasks."""
        self._running = False
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        if self._evict_task and not self._evict_task.done():
            self._evict_task.cancel()
            try:
                await self._evict_task
            except asyncio.CancelledError:
                pass

        # Cancel all debounce tasks
        for dtask in self._debounce_tasks.values():
            if not dtask.done():
                dtask.cancel()

        # Cancel all in-flight calls across all sessions
        for session in self.sessions.values():
            session.bump_epoch(reason="system_shutdown")

        for task in list(self._turn_tasks):
            task.cancel()
        for runtime in self._voice.values():
            for task in list(runtime.tasks):
                task.cancel()
        self._voice.clear()
        self._audio_buffers.clear()

    async def post_event(self, event: BaseEvent) -> None:
        """Inbound interface: Put an event onto the Event Queue."""
        self.trace_logger.log_event(event)
        if event.session_id in self.sessions:
            self.sessions[event.session_id].touch()
        await self.event_queue.put(event)

    async def emit_action(self, action: BaseAction) -> None:
        """Outbound interface: Put an action onto the Action Queue after trace validation."""
        self.trace_logger.log_action(action)
        await self.action_queue.put(action)

    async def _commit_turn_and_emit_graph(
        self,
        session: SessionState,
        agent_response: str,
        artifacts: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Distill the current scratchpad turn into a CanonicalTurn, insert it into
        graph_memory, and stream the resulting nodes/edges to the frontend. No-op if
        this session never opted into the 2-tier memory system (session.ensure_memory()
        not called)."""
        if session.scratchpad is None or session.graph_memory is None:
            return

        canonical_turn = session.scratchpad.commit(agent_response=agent_response, artifacts=artifacts)
        turn_node = session.graph_memory.insert_turn(canonical_turn)

        new_node_ids = {turn_node.id}
        new_node_ids.update(e.id for e in session.graph_memory.get_entities_for_turn(turn_node.id))
        new_node_ids.update(
            a.id for a in session.graph_memory.artifacts.values() if a.source_turn_id == turn_node.id
        )

        payload = session.graph_memory.to_graph_payload()
        nodes = [GraphNodePayload(**n) for n in payload["nodes"] if n["id"] in new_node_ids]
        edges = [
            GraphEdgePayload(**e)
            for e in payload["edges"]
            if e["source"] in new_node_ids or e["target"] in new_node_ids
        ]

        await self.emit_action(
            GraphUpdateAction(
                session_id=session.session_id,
                epoch=session.epoch,
                nodes=nodes,
                edges=edges,
                op="append",
            )
        )

    async def get_next_action(self, timeout: Optional[float] = None) -> BaseAction:
        """Outbound consumer: Read the next action from the Action Queue."""
        if timeout is not None:
            return await asyncio.wait_for(self.action_queue.get(), timeout=timeout)
        return await self.action_queue.get()

    async def _process_events(self) -> None:
        """Main event loop consuming from event_queue."""
        while self._running:
            try:
                event = await self.event_queue.get()
                await self._handle_event(event)
                self.event_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Error processing event: %s", e)

    async def _handle_event(self, event: BaseEvent) -> None:
        """Route event according to type."""
        session = self.get_or_create_session(event.session_id)

        if event.event_type == EventType.INTERRUPT_SIGNAL:
            await self._handle_interrupt(session, reason=getattr(event, "reason", "interrupt_signal"))

        elif event.event_type == EventType.USER_TEXT:
            assert isinstance(event, UserTextEvent)
            if self.enable_debounce and self.debounce_window_s > 0:
                await self._queue_debounced_user_text(session, event)
            else:
                await self._handle_user_text(session, event)

        elif event.event_type == EventType.AUDIO_CHUNK:
            assert isinstance(event, AudioChunkEvent)
            await self._handle_audio_chunk(session, event)

        elif event.event_type == EventType.VIDEO_FRAME:
            assert isinstance(event, VideoFrameEvent)
            self._handle_video_frame(session, event)

        elif event.event_type == EventType.TOOL_RESULT:
            assert isinstance(event, ToolResultEvent)
            await self._handle_tool_result(session, event)

    async def _handle_audio_chunk(self, session: SessionState, event: AudioChunkEvent) -> None:
        """Process raw audio chunk: buffer, transcribe via ASR, and feed into intent loop."""
        if event.streaming or event.stream_control:
            await self._handle_voice_stream(session, event)
            return

        if not event.audio_bytes:
            return

        sid = session.session_id
        if sid not in self._audio_buffers:
            self._audio_buffers[sid] = bytearray()

        self._audio_buffers[sid].extend(event.audio_bytes)

        # Transcribe if marked final or if buffer exceeds 1.5s (48000 bytes at 16kHz 16-bit PCM)
        CHUNK_THRESHOLD_BYTES = 48000
        if event.is_final or len(self._audio_buffers[sid]) >= CHUNK_THRESHOLD_BYTES:
            raw_bytes = bytes(self._audio_buffers[sid])
            self._audio_buffers[sid].clear()

            # Run transcription off the event loop thread to prevent blocking
            asr_started = time.perf_counter()
            transcribed_text = await asyncio.to_thread(
                self.asr_processor.transcribe_audio_bytes,
                raw_bytes,
                event.format,
            )

            # Tell the client what was heard (or that nothing was) so it can show the
            # transcript instead of an opaque "audio sent" placeholder.
            await self.emit_action(
                TranscriptAction(
                    session_id=sid,
                    epoch=session.epoch,
                    text=transcribed_text or "",
                    asr_model=getattr(self.asr_processor, "model_size", None),
                    latency_ms=round((time.perf_counter() - asr_started) * 1000, 1),
                )
            )

            if transcribed_text:
                logger.info("Transcribed audio for session %s: '%s'", sid, transcribed_text)
                text_event = UserTextEvent(
                    session_id=sid,
                    text=transcribed_text,
                )
                self.trace_logger.log_event(text_event)
                if self.enable_debounce and self.debounce_window_s > 0:
                    await self._queue_debounced_user_text(session, text_event)
                else:
                    await self._handle_user_text(session, text_event)

    # ------------------------------------------------------------------------------------- vision
    def _handle_video_frame(self, session: SessionState, event: VideoFrameEvent) -> None:
        """Buffer the latest frame. Deliberately does NO inference: vision runs only when the planner asks."""
        if not event.frame_data or len(event.frame_data) > MAX_FRAME_BYTES or event.mime not in ALLOWED_MIME:
            logger.warning("Dropping invalid video frame (%s bytes, %s)", len(event.frame_data or b""), event.mime)
            return
        frames = self._frames.setdefault(session.session_id, deque(maxlen=FRAMES_PER_SESSION))
        frames.append(_Frame(event.frame_id or uuid.uuid4().hex[:8], event.mime, event.frame_data, event.source, event.timestamp))

    def latest_frame(self, session_id: str) -> Optional[_Frame]:
        frames = self._frames.get(session_id)
        if not frames or time.time() - frames[-1].ts > MAX_FRAME_AGE_S:
            return None
        return frames[-1]

    async def _run_vision(self, session_id: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """The analyze_frame tool: ask the vision backend about the session's newest frame."""
        question = str(arguments.get("question") or "Describe what you see.")
        frame = self.latest_frame(session_id)
        if frame is None:
            return {"has_frame": False, "answer": "", "note": "No camera or screen frame is being shared right now."}
        result = await self.vision_backend.analyze(frame.data, frame.mime, question)
        return {
            "has_frame": True, "answer": result.answer, "model": result.model,
            "frame_id": frame.frame_id, "frame_age_s": round(time.time() - frame.ts, 2),
        }

    # ------------------------------------------------------------------ continuous voice streaming
    def _spawn_voice_task(self, runtime: _VoiceRuntime, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        runtime.tasks.add(task)
        task.add_done_callback(runtime.tasks.discard)
        return task

    async def _handle_voice_stream(self, session: SessionState, event: AudioChunkEvent) -> None:
        """Streaming voice input: VAD -> utterance segmentation -> partial/final transcription.

        Transcription always runs in background tasks; this handler only does the cheap per-frame
        VAD work, so the coordinator's event loop is never stalled by Whisper (a stalled loop would
        delay exactly the interrupts and tool results this feature exists to react to).
        """
        sid = session.session_id
        runtime = self._voice.get(sid)

        if event.stream_control == "start":
            if runtime is None:
                runtime = _VoiceRuntime(stream=VoiceStream(vad=make_vad()))
                self._voice[sid] = runtime
            else:
                runtime.stream.reset()
            await self.emit_action(VoiceActivityAction(session_id=sid, epoch=session.epoch, state="listening"))
            return

        if runtime is None:
            return  # audio before/without a "start" control message is ignored

        if event.stream_control == "stop":
            await self._dispatch_voice_events(session, runtime, runtime.stream.flush())
            self._voice.pop(sid, None)  # in-flight finals keep running via their own task references
            await self.emit_action(VoiceActivityAction(session_id=sid, epoch=session.epoch, state="idle"))
            return

        if event.audio_bytes:
            await self._dispatch_voice_events(session, runtime, runtime.stream.feed(event.audio_bytes))

    async def _dispatch_voice_events(self, session: SessionState, runtime: _VoiceRuntime, events: List[Any]) -> None:
        sid = session.session_id
        for ev in events:
            if isinstance(ev, SpeechStart):
                await self.emit_action(
                    VoiceActivityAction(session_id=sid, epoch=session.epoch, state="speech_start", utterance_id=ev.utterance_id)
                )
            elif isinstance(ev, PartialDue):
                # One partial in flight at a time; if Whisper is still busy, skip this tick.
                if runtime.partial_task is None or runtime.partial_task.done():
                    runtime.partial_task = self._spawn_voice_task(runtime, self._voice_partial(session, runtime, ev))
            elif isinstance(ev, UtteranceEnd):
                runtime.finalized.add(ev.utterance_id)  # any partial still in flight for it is now stale
                await self.emit_action(
                    VoiceActivityAction(
                        session_id=sid, epoch=session.epoch, state="speech_end",
                        utterance_id=ev.utterance_id, detail=ev.reason,
                    )
                )
                runtime.final_tail = self._spawn_voice_task(
                    runtime, self._voice_final(session, runtime, ev, runtime.final_tail)
                )

    async def _voice_partial(self, session: SessionState, runtime: _VoiceRuntime, ev: PartialDue) -> None:
        started = time.perf_counter()
        text = await asyncio.to_thread(self.asr_processor.transcribe_audio_bytes, ev.pcm, "pcm_16khz")
        if not text or ev.utterance_id in runtime.finalized:
            return
        await self.emit_action(
            TranscriptAction(
                session_id=session.session_id,
                epoch=session.epoch,
                text=text,
                asr_model=getattr(self.asr_processor, "model_size", None),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                is_partial=True,
                utterance_id=ev.utterance_id,
            )
        )
        await self._maybe_voice_barge_in(session, runtime, ev.utterance_id, text)

    async def _maybe_voice_barge_in(self, session: SessionState, runtime: _VoiceRuntime, utterance_id: str, text: str) -> None:
        """Interrupt in-flight work as soon as a partial transcript already reads as a correction.

        Only worth doing (and only safe to do) when something is actually running; otherwise the
        final transcript goes through the normal path, which handles epoch bumps itself.
        """
        if utterance_id in runtime.barge_in_fired:
            return
        if not any(c.status in ("pending", "running") for c in session.in_flight_calls.values()):
            return
        if not self.intent_classifier.classify_text(text)["is_interrupt"]:
            return

        runtime.barge_in_fired.add(utterance_id)
        cancellations = session.bump_epoch(reason=f"voice_barge_in: {text}")
        for cancel_action in cancellations:
            await self.emit_action(cancel_action)
        await self.emit_action(
            VoiceActivityAction(
                session_id=session.session_id, epoch=session.epoch, state="barge_in",
                utterance_id=utterance_id, detail=text,
            )
        )
        await self.emit_action(session.get_snapshot())

    async def _voice_final(
        self, session: SessionState, runtime: _VoiceRuntime, ev: UtteranceEnd, previous: Optional[asyncio.Task]
    ) -> None:
        if previous is not None:
            try:
                await previous  # keep utterances in order
            except BaseException:  # noqa: BLE001 - an earlier utterance failing must not block this one
                pass
        started = time.perf_counter()
        text = await asyncio.to_thread(self.asr_processor.transcribe_audio_bytes, ev.pcm, "pcm_16khz")
        await self.emit_action(
            TranscriptAction(
                session_id=session.session_id,
                epoch=session.epoch,
                text=text or "",
                asr_model=getattr(self.asr_processor, "model_size", None),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                is_partial=False,
                utterance_id=ev.utterance_id,
            )
        )
        handled = ev.utterance_id in runtime.barge_in_fired
        runtime.barge_in_fired.discard(ev.utterance_id)
        runtime.finalized.discard(ev.utterance_id)
        if text:
            logger.info("Voice utterance %s (%s): '%s'", ev.utterance_id, ev.reason, text)
            await self.post_event(UserTextEvent(session_id=session.session_id, text=text, barge_in_handled=handled))

    async def _queue_debounced_user_text(self, session: SessionState, event: UserTextEvent) -> None:
        """Buffer and coalesce rapid-fire user corrections (§7.5)."""
        sid = session.session_id
        if sid not in self._pending_events:
            self._pending_events[sid] = []
        self._pending_events[sid].append(event)

        if sid in self._debounce_tasks and not self._debounce_tasks[sid].done():
            self._debounce_tasks[sid].cancel()

        async def _flush_after_delay():
            try:
                await asyncio.sleep(self.debounce_window_s)
            except asyncio.CancelledError:
                return  # a newer event arrived inside the window: it will flush the coalesced burst
            events = self._pending_events.pop(sid, [])
            if not events:
                return
            # The latest event in the rapid burst represents the final intent. Planning (an LLM call)
            # runs in its OWN task: cancelling the debounce timer for a newer message must never kill a
            # plan that is already in progress -- that silently lost the earlier request. Whether that
            # plan is still wanted is decided by the epoch check in the planner, not by task cancellation.
            self._spawn_turn(self._handle_user_text(session, events[-1]))

        self._debounce_tasks[sid] = asyncio.create_task(_flush_after_delay())

    def _spawn_turn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._turn_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._turn_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("Turn handling failed", exc_info=t.exception())

        task.add_done_callback(_done)
        return task

    async def _handle_interrupt(self, session: SessionState, reason: str = "interrupt") -> None:
        """Handle interrupt signal: bump epoch, emit cancellations and updated snapshot."""
        cancellations = session.bump_epoch(reason=reason)
        for cancel_action in cancellations:
            await self.emit_action(cancel_action)

        # Acknowledge out loud when something was actually stopped (a bare interrupt has no follow-up
        # text, so without this the user gets silence after "stop").
        if cancellations or self._active_plans.get(session.session_id, 0) > 0:
            await self.emit_action(
                FillerAction(
                    session_id=session.session_id,
                    epoch=session.epoch,
                    text=generate_filler(intent=session.intent, slots=session.slots, is_interruption=True),
                )
            )

        # Emit updated snapshot showing cancelled calls
        await self.emit_action(session.get_snapshot())

    async def _handle_user_text(self, session: SessionState, event: UserTextEvent) -> None:
        """Process user text: Tier 1 intent/interrupt classification, Tier 2 fast filler, Tier 3 planner."""
        if session.scratchpad is not None:
            session.scratchpad.append_utterance_chunk(event.text)

        # Tier 1 Interrupt / Intent-Shift Detection (§3, §7.2)
        classification = self.intent_classifier.classify_text(event.text)
        is_interrupt = classification["is_interrupt"] or event.barge_in_handled
        needs_clarification = classification["needs_clarification"]

        # If high-confidence interrupt, bump epoch and cancel stale in-flight calls immediately.
        # A voice barge-in may already have done this from a partial transcript -- don't bump twice.
        if is_interrupt and not event.barge_in_handled:
            cancellations = session.bump_epoch(reason=f"user_correction: {event.text}")
            for cancel_action in cancellations:
                await self.emit_action(cancel_action)

        # Emit Tier 2 Fast-Path filler or ack immediately (< 50ms)
        filler_text = generate_filler(
            intent=session.intent,
            slots=session.slots,
            is_interruption=is_interrupt,
        )
        await self.emit_action(
            FillerAction(
                session_id=session.session_id,
                epoch=session.epoch,
                text=filler_text,
            )
        )

        # Tier 3 Slow-Path Planning (§3, §4)
        # Tag this plan with the epoch it starts under (§2.1). If an interrupt bumps the epoch while the
        # LLM is still thinking, the planner returns [] instead of dispatching an already-stale plan.
        sid = session.session_id
        self._active_plans[sid] = self._active_plans.get(sid, 0) + 1
        try:
            actions = await self.planner.plan(event, session, plan_epoch=session.epoch)
        finally:
            self._active_plans[sid] -= 1
        if not actions:
            return  # superseded by a newer interrupt: nothing to emit, and no turn to commit
        turn_has_tool_call, agent_response_text = await self._dispatch_actions(session, actions, origin_text=event.text)

        # A turn that resolved synchronously (spoken_response/clarification, no tool
        # call in flight) is complete right now -- commit it. Turns that spawned a
        # tool call/agent worker commit later, in _handle_tool_result, once the
        # result actually comes back.
        if not turn_has_tool_call:
            await self._commit_turn_and_emit_graph(session, agent_response=agent_response_text)

    async def _dispatch_actions(self, session: SessionState, actions: List[BaseAction], origin_text: str) -> Tuple[bool, str]:
        """Emit a plan's actions and start its tool calls. Returns (started_a_tool_call, spoken_text)."""
        turn_has_tool_call = False
        agent_response_text = ""
        for action in actions:
            if isinstance(action, ToolCallAction):
                turn_has_tool_call = True
                self._call_origin[action.call_id] = origin_text
                await self.emit_action(action)
                # Spawn background execution task
                task = asyncio.create_task(
                    self._execute_tool_task(
                        session_id=session.session_id,
                        call_id=action.call_id,
                        tool_name=action.tool_name,
                        arguments=action.arguments,
                        epoch=action.epoch,
                    )
                )
                session.attach_task(action.call_id, task)
            else:
                if isinstance(action, SpokenResponseAction):
                    agent_response_text = action.text
                await self.emit_action(action)
        return turn_has_tool_call, agent_response_text

    async def _continue_after_observation(self, session: SessionState, request_text: str, result: Dict[str, Any]) -> None:
        """analyze_frame is an OBSERVATION tool: its output is input to further reasoning, not a final answer.

        Re-plan the user's original request with the observation attached ("book a flight to the city on this
        poster" needs the vision result before search_flights can be called). One step only -- the continuation
        plan is not offered analyze_frame again, so it cannot loop.
        """
        observation = result.get("answer") or result.get("note") or "The image could not be read."
        sid = session.session_id
        self._active_plans[sid] = self._active_plans.get(sid, 0) + 1
        try:
            actions = await self.planner.plan(
                UserTextEvent(session_id=sid, text=request_text), session,
                plan_epoch=session.epoch, observation=observation,
            )
        finally:
            self._active_plans[sid] -= 1
        if not actions:
            return  # interrupted while re-planning: the newer turn owns the conversation now
        turn_has_tool_call, agent_response_text = await self._dispatch_actions(session, actions, origin_text=request_text)
        if not turn_has_tool_call:
            await self._commit_turn_and_emit_graph(session, agent_response=agent_response_text)

    async def dispatch_tool_call(
        self,
        session_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        call_id: str,
    ) -> Optional[ToolCallAction]:
        """Dispatch a tool call, enforce idempotency, and run in background."""
        session = self.get_or_create_session(session_id)
        is_modifying = self.tool_router.is_state_modifying(tool_name)

        action = session.register_tool_call(
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            is_state_modifying=is_modifying,
        )

        if action is None:
            # Skipped due to idempotency duplicate check
            logger.info("Skipping duplicate tool call '%s' (idempotency key exists)", tool_name)
            return None

        # Emit tool call action
        await self.emit_action(action)
        # Emit snapshot showing call as in-flight
        await self.emit_action(session.get_snapshot())

        # Spawn execution task in background
        task = asyncio.create_task(
            self._execute_tool_task(
                session_id=session_id,
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                epoch=session.epoch,
            )
        )
        session.attach_task(call_id, task)
        return action

    async def _execute_tool_task(
        self,
        session_id: str,
        call_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        epoch: int,
    ) -> None:
        """Run tool handler or autonomous worker asynchronously and post ToolResultEvent."""
        result = None
        error = None

        if tool_name == "spawn_agent":
            from agent.workers.registry import create_agent_worker
            clean_args = dict(arguments)
            name = clean_args.pop("name", "bob")
            role = clean_args.pop("role", "Worker")
            goal = clean_args.pop("goal", "")
            worker = create_agent_worker(
                name=name,
                role=role,
                goal=goal,
                session_id=session_id,
                epoch=epoch,
                call_id=call_id,
                llm_backend=self.llm_backend,
                **clean_args,
            )


            async def _step_callback(step_action):
                await self.emit_action(step_action)


            try:
                result = await worker.execute(step_callback=_step_callback)
            except asyncio.CancelledError:
                logger.info("Autonomous worker '%s' (call_id=%s) cancelled under epoch %d", worker.name, call_id, epoch)
                return
            except Exception as e:
                logger.exception("Error executing autonomous worker '%s': %s", worker.name, e)
                error = str(e)
        elif tool_name == "analyze_frame":
            try:
                result = await self._run_vision(session_id, arguments)
            except asyncio.CancelledError:
                logger.info("Vision call (call_id=%s) cancelled under epoch %d", call_id, epoch)
                return
            except Exception as e:
                logger.warning("Vision analysis failed: %s", e)
                error = str(e)
        else:
            try:
                result = await self.tool_router.execute_tool(tool_name, arguments)
            except asyncio.CancelledError:
                logger.info("Tool task '%s' (call_id=%s) cancelled", tool_name, call_id)
                return
            except Exception as e:
                logger.exception("Error executing tool '%s': %s", tool_name, e)
                error = str(e)

        # Post result to inbound event queue
        await self.post_event(
            ToolResultEvent(
                session_id=session_id,
                call_id=call_id,
                tool_name=tool_name,
                epoch=epoch,
                result=result,
                error=error,
            )
        )

    async def _handle_tool_result(self, session: SessionState, event: ToolResultEvent) -> None:
        """Process tool completion event and optionally announce completed artifacts."""
        completed = session.complete_tool_call(
            call_id=event.call_id,
            result=event.result,
            error=event.error,
        )

        origin_text = self._call_origin.pop(event.call_id, None)

        if not completed:
            logger.info(
                "Discarding stale or cancelled tool result for call_id '%s' (event epoch=%d, session epoch=%d)",
                event.call_id,
                event.epoch,
                session.epoch,
            )
            return

        if event.tool_name == "analyze_frame" and not event.error and origin_text and isinstance(event.result, dict):
            await self.emit_action(session.get_snapshot())
            self._spawn_turn(self._continue_after_observation(session, origin_text, event.result))
            return

        # Every completed call gets a visible reply: a failure notice, an artifact
        # announcement (agent workers), or a summary of the tool's result. Previously only
        # artifact-producing workers replied, leaving plain tool calls (flights, weather...)
        # stuck on the "Looking that up..." filler with no confirmation.
        artifacts: List[Dict[str, Any]] = []
        if event.error:
            agent_response_text = summarize_tool_error(event.tool_name, event.error)
        elif event.result and isinstance(event.result, dict) and "artifact" in event.result:
            art = event.result["artifact"]
            agent_name = event.result.get("agent_name", "Worker")
            agent_response_text = (
                f"Agent '{agent_name}' has successfully finished building '{art.get('title', 'artifact')}'. "
                "The artifact is ready in your workspace."
            )
            artifacts = [art]
        else:
            agent_response_text = summarize_tool_result(event.tool_name, event.result)

        await self.emit_action(
            SpokenResponseAction(
                session_id=session.session_id,
                epoch=session.epoch,
                text=agent_response_text,
            )
        )

        # Emit snapshot with updated completed state
        await self.emit_action(session.get_snapshot())

        await self._commit_turn_and_emit_graph(session, agent_response=agent_response_text, artifacts=artifacts)

