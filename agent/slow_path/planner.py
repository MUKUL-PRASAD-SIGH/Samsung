"""Tier 3: Slow Path Planner (§3, §4).

Performs LLM reasoning, argument extraction, tool call dispatching,
and response generation.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from agent.schemas.events import UserTextEvent
from agent.schemas.actions import (
    BaseAction,
    SpokenResponseAction,
    ToolCallAction,
    ClarificationAction,
    StateSnapshotAction,
)
from agent.coordination.state_machine import SessionState
from agent.coordination.tool_router import ToolRouter
from agent.llm_client import LLMBackend, CircuitBreakerLLMClient, LLMConfig, get_fallback_backend
from agent.memory.context_builder import build_context_block, build_history_messages
from agent.memory.tool_slots import MISSING, entities_from_tool_call

logger = logging.getLogger("agent.planner")


class Planner:
    def __init__(
        self,
        llm_backend: LLMBackend,
        tool_router: ToolRouter,
        config: Optional[LLMConfig] = None,
    ):
        self.config = config or LLMConfig()
        self.client = CircuitBreakerLLMClient(llm_backend, self.config, fallback_backend=get_fallback_backend())
        self.tool_router = tool_router

    async def plan(
        self,
        event: UserTextEvent,
        session: SessionState,
        plan_epoch: Optional[int] = None,
        observation: Optional[str] = None,
    ) -> List[BaseAction]:
        """Generate reasoned actions (tool calls, responses, or clarifications) for user input.

        `plan_epoch` is the epoch the plan started under. If the session's epoch has moved on by the
        time the LLM answers, the user interrupted mid-thought: the plan is stale, so return no actions
        rather than registering and dispatching work the user has already abandoned (§2.1).

        `observation` is the result of an observation tool (analyze_frame). It is attached to the request and
        analyze_frame is withheld, so the continuation acts on what was seen instead of looking again.
        """
        actions: List[BaseAction] = []

        # Prepare messages context. build_context_block/build_history_messages are the
        # single extension points for the 2-tier cognitive memory system (agent/memory/) --
        # when a session has no graph_memory attached yet, build_history_messages returns
        # [] and this degrades to exactly the original single-turn behavior.
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a helpful, precise real-time assistant and autonomous agent architect. "
                    "When the user requests an action, call the appropriate tool. "
                    "If they explicitly ask to book, reserve or cancel something and have given the details, "
                    "call that booking tool directly -- do not search first. "
                    "If the user refers to something visible (\"this\", \"on my screen\", \"in the picture\", \"the sign\"), "
                    "call analyze_frame with a specific question about the image, then use its answer to continue the request. "
                    "You have the superpower to synthesize custom, bespoke agents on the fly! "
                    "When the user asks to build, design, audit, analyze, or execute any specialized task (e.g. database schema, SVG graphics, security audit, code, travel), "
                    "call 'spawn_agent' and create a tailored agent with a unique name (e.g. 'db_architect', 'vector_craft', 'sec_auditor', 'bob'), "
                    "an exact specialization role, custom system_prompt, tailored step-by-step thinking plan, and expected_artifact. "
                    "Extract slot arguments accurately. "
                    "If you detect entities worth remembering (locations, dates, components, etc.) or a shift in "
                    "the user's overall intent, end your reply with one final line of the exact form "
                    'MEMORY_UPDATE: {"entities": [{"type": "LOCATION", "key": "destination", "value": "BOM"}], "intent_shift": null} '
                    "(plain text: no code fence, no separator line, nothing after it). Omit it entirely if there is nothing new to record. "
                    f"{build_context_block(session)}"
                ),
            },
            *build_history_messages(session),
            {"role": "user", "content": event.text if observation is None
             else f"{event.text}\n\n[Vision result for the image the user is sharing: {observation}]"},
        ]

        tools = self.tool_router.get_tool_manifests()
        if observation is not None:
            tools = [t for t in tools if t.get("function", {}).get("name") != "analyze_frame"]

        # Generate LLM response through circuit-breaker-wrapped client
        llm_resp = await self.client.generate(messages, tools=tools)

        if plan_epoch is not None and session.epoch != plan_epoch:
            logger.info("Discarding stale plan for %r: epoch %d -> %d while the LLM was thinking",
                        event.text, plan_epoch, session.epoch)
            return []

        if llm_resp.memory_update and session.scratchpad is not None:
            for ent in llm_resp.memory_update.get("entities", []) or []:
                try:
                    session.scratchpad.record_entity_candidate(ent["type"], ent["key"], ent["value"])
                except (KeyError, TypeError):
                    logger.warning("Malformed entity in MEMORY_UPDATE: %r", ent)
            intent_shift = llm_resp.memory_update.get("intent_shift")
            if intent_shift:
                session.scratchpad.record_intent_shift(intent_shift)

        if llm_resp.response_type == "tool_call" and llm_resp.tool_name:
            call_id = f"call_{uuid.uuid4().hex[:8]}"
            tool_name = llm_resp.tool_name
            arguments = llm_resp.arguments

            # Validate tool arguments against manifest schema
            try:
                self.tool_router.validate_call(tool_name, arguments)
            except Exception as e:
                logger.warning("Tool call validation error for '%s': %s", tool_name, e)

            is_modifying = self.tool_router.is_state_modifying(tool_name)

            # Apply the call's arguments to the slots first (§2.3: corrections patch the snapshot, which
            # shows slots while the call is in flight). This must precede registration: the idempotency
            # key hashes the slot values (§2.2), so two identical requests then get the same key while
            # two different ones don't.
            entities = entities_from_tool_call(tool_name, arguments)
            previous = session.stage_call_slots(call_id, {k: v for _, k, v in entities}) if entities else {}

            tool_action = session.register_tool_call(
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                is_state_modifying=is_modifying,
            )

            if tool_action is None:
                session.revert_call_slots(call_id)  # duplicate request: nothing was dispatched
            else:
                actions.append(tool_action)
                if session.scratchpad is not None:
                    for entity_type, key, value in entities:
                        session.scratchpad.record_entity_candidate(
                            entity_type, key, value, call_id=call_id, previous_value=previous.get(key, MISSING)
                        )

        elif llm_resp.response_type == "clarification":
            actions.append(
                ClarificationAction(
                    session_id=session.session_id,
                    epoch=session.epoch,
                    question=llm_resp.content or "Could you please clarify that detail?",
                )
            )

        else:
            actions.append(
                SpokenResponseAction(
                    session_id=session.session_id,
                    epoch=session.epoch,
                    text=llm_resp.content or "I have processed your request.",
                )
            )

        # Always attach current state snapshot
        actions.append(session.get_snapshot())
        return actions
