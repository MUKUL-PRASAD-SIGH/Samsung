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
from agent.llm_client import LLMBackend, CircuitBreakerLLMClient, LLMConfig
from agent.memory.context_builder import build_context_block, build_history_messages
from agent.memory.tool_slots import entities_from_tool_call

logger = logging.getLogger("agent.planner")


class Planner:
    def __init__(
        self,
        llm_backend: LLMBackend,
        tool_router: ToolRouter,
        config: Optional[LLMConfig] = None,
    ):
        self.config = config or LLMConfig()
        self.client = CircuitBreakerLLMClient(llm_backend, self.config)
        self.tool_router = tool_router

    async def plan(
        self,
        event: UserTextEvent,
        session: SessionState,
    ) -> List[BaseAction]:
        """Generate reasoned actions (tool calls, responses, or clarifications) for user input."""
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
            {"role": "user", "content": event.text},
        ]

        tools = self.tool_router.get_tool_manifests()

        # Generate LLM response through circuit-breaker-wrapped client
        llm_resp = await self.client.generate(messages, tools=tools)

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

            # Register call in session state
            tool_action = session.register_tool_call(
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                is_state_modifying=is_modifying,
            )

            if tool_action is not None:
                actions.append(tool_action)
                # The arguments the model chose are the resolved parameters for this turn; record
                # them as call-scoped entities (dropped if this call is later aborted by a
                # correction) so slots/graph reflect tool turns, which never carry MEMORY_UPDATE.
                if session.scratchpad is not None:
                    for entity_type, key, value in entities_from_tool_call(tool_name, arguments):
                        session.scratchpad.record_entity_candidate(entity_type, key, value, call_id=call_id)

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
