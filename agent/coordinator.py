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
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional

from agent.schemas.events import (
    BaseEvent,
    EventType,
    UserTextEvent,
    AudioChunkEvent,
    InterruptSignalEvent,
    ToolResultEvent,
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

logger = logging.getLogger("agent.coordinator")


class AgentCoordinator:
    def __init__(
        self,
        tool_router: Optional[ToolRouter] = None,
        trace_logger: Optional[TraceLogger] = None,
        llm_backend: Optional[LLMBackend] = None,
        intent_classifier: Optional[IntentClassifier] = None,
        asr_processor: Optional[ASRProcessor] = None,
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

        self._debounce_tasks: Dict[str, asyncio.Task] = {}
        self._pending_events: Dict[str, List[UserTextEvent]] = {}
        self._audio_buffers: Dict[str, bytearray] = {}
        self._running = False
        self._loop_task: Optional[asyncio.Task] = None

    def get_or_create_session(self, session_id: str) -> SessionState:
        """Retrieve existing session or instantiate a new SessionState."""
        if session_id not in self.sessions:
            session = SessionState(session_id=session_id)
            session.ensure_memory()
            self.sessions[session_id] = session
        return self.sessions[session_id]

    async def start(self) -> None:
        """Start the background event processing loop."""
        if self._running:
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._process_events())

    async def stop(self) -> None:
        """Stop processing and cancel remaining tasks."""
        self._running = False
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass

        # Cancel all debounce tasks
        for dtask in self._debounce_tasks.values():
            if not dtask.done():
                dtask.cancel()

        # Cancel all in-flight calls across all sessions
        for session in self.sessions.values():
            session.bump_epoch(reason="system_shutdown")

        self._audio_buffers.clear()

    async def post_event(self, event: BaseEvent) -> None:
        """Inbound interface: Put an event onto the Event Queue."""
        self.trace_logger.log_event(event)
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

        elif event.event_type == EventType.TOOL_RESULT:
            assert isinstance(event, ToolResultEvent)
            await self._handle_tool_result(session, event)

    async def _handle_audio_chunk(self, session: SessionState, event: AudioChunkEvent) -> None:
        """Process raw audio chunk: buffer, transcribe via ASR, and feed into intent loop."""
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
                events = self._pending_events.pop(sid, [])
                if not events:
                    return
                # The latest event in the rapid burst represents the final intent
                final_event = events[-1]
                await self._handle_user_text(session, final_event)
            except asyncio.CancelledError:
                pass

        self._debounce_tasks[sid] = asyncio.create_task(_flush_after_delay())

    async def _handle_interrupt(self, session: SessionState, reason: str = "interrupt") -> None:
        """Handle interrupt signal: bump epoch, emit cancellations and updated snapshot."""
        cancellations = session.bump_epoch(reason=reason)
        for cancel_action in cancellations:
            await self.emit_action(cancel_action)

        # Emit updated snapshot showing cancelled calls
        await self.emit_action(session.get_snapshot())

    async def _handle_user_text(self, session: SessionState, event: UserTextEvent) -> None:
        """Process user text: Tier 1 intent/interrupt classification, Tier 2 fast filler, Tier 3 planner."""
        if session.scratchpad is not None:
            session.scratchpad.append_utterance_chunk(event.text)

        # Tier 1 Interrupt / Intent-Shift Detection (§3, §7.2)
        classification = self.intent_classifier.classify_text(event.text)
        is_interrupt = classification["is_interrupt"]
        needs_clarification = classification["needs_clarification"]

        # If high-confidence interrupt, bump epoch and cancel stale in-flight calls immediately
        if is_interrupt:
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
        actions = await self.planner.plan(event, session)
        turn_has_tool_call = False
        agent_response_text = ""
        for action in actions:
            if isinstance(action, ToolCallAction):
                turn_has_tool_call = True
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

        # A turn that resolved synchronously (spoken_response/clarification, no tool
        # call in flight) is complete right now -- commit it. Turns that spawned a
        # tool call/agent worker commit later, in _handle_tool_result, once the
        # result actually comes back.
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

        if not completed:
            logger.info(
                "Discarding stale or cancelled tool result for call_id '%s' (event epoch=%d, session epoch=%d)",
                event.call_id,
                event.epoch,
                session.epoch,
            )
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

