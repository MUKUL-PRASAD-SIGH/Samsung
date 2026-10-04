"""Registry and Factory for Spawning Autonomous Worker Agents."""

from __future__ import annotations

import logging
from typing import Any

from agent.workers.base import BaseAgentWorker
from agent.workers.coder_agent import CoderAgent
from agent.workers.researcher_agent import ResearcherAgent
from agent.workers.validator_agent import ValidatorAgent
from agent.workers.dynamic_agent import DynamicAgentWorker

logger = logging.getLogger("agent.workers.registry")


def create_agent_worker(
    name: str,
    role: str,
    goal: str,
    session_id: str,
    epoch: int,
    call_id: str,
    **kwargs: Any,
) -> BaseAgentWorker:
    """Factory creating specialized or dynamic JIT agent worker instances."""
    # If custom steps, system prompt, or bespoke artifact is requested -> DynamicAgentWorker
    if "steps" in kwargs or "system_prompt" in kwargs or "expected_artifact" in kwargs or "artifact" in kwargs:
        return DynamicAgentWorker(
            name=name,
            role=role,
            goal=goal,
            session_id=session_id,
            epoch=epoch,
            call_id=call_id,
            **kwargs,
        )

    name_str = (name or "").lower()
    role_str = (role or "").lower()
    goal_str = (goal or "").lower()

    if any(k in name_str or k in role_str or k in goal_str for k in ("research", "scout", "flight", "hotel", "travel")):
        return ResearcherAgent(
            name=name or "scout",
            role=role or "Market & Flight Scout",
            goal=goal,
            session_id=session_id,
            epoch=epoch,
            call_id=call_id,
            **kwargs,
        )
    elif any(k in name_str or k in role_str or k in goal_str for k in ("valid", "sentinel", "lint", "test", "audit")):
        return ValidatorAgent(
            name=name or "sentinel",
            role=role or "Quality & Type Sentinel",
            goal=goal,
            session_id=session_id,
            epoch=epoch,
            call_id=call_id,
            **kwargs,
        )
    elif "bob" in name_str or any(k in goal_str for k in ("clock", "analog", "typescript generator")):
        return CoderAgent(
            name=name or "bob",
            role=role or "TypeScript Generator",
            goal=goal,
            session_id=session_id,
            epoch=epoch,
            call_id=call_id,
            **kwargs,
        )
    else:
        # Dynamic JIT Agent for all emergent custom requests
        return DynamicAgentWorker(
            name=name or "specialist",
            role=role or "Custom AI Agent",
            goal=goal,
            session_id=session_id,
            epoch=epoch,
            call_id=call_id,
            **kwargs,
        )
