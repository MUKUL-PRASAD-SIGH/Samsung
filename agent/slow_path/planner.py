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

        # Prepare messages context
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
                    f"Current session intent: {session.intent or 'unknown'}. "
                    f"Current slots: {session.slots}."
                ),
            },


            {"role": "user", "content": event.text},
        ]

        tools = self.tool_router.get_tool_manifests()

        # Generate LLM response through circuit-breaker-wrapped client
        llm_resp = await self.client.generate(messages, tools=tools)

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
