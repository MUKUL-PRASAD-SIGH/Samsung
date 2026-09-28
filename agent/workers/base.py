"""Base Worker Agent Class for Asynchronous Multi-Step Autonomous Execution."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Coroutine, Dict, Optional

from agent.schemas.actions import AgentStepAction

logger = logging.getLogger("agent.workers.base")


class BaseAgentWorker(ABC):
    def __init__(
        self,
        name: str,
        role: str,
        goal: str,
        session_id: str,
        epoch: int,
        call_id: str,
        total_steps: int = 4,
        step_delay_s: float = 0.8,
        **kwargs,
    ):
        self.name = name
        self.role = role
        self.goal = goal
        self.session_id = session_id
        self.epoch = epoch
        self.call_id = call_id
        self.total_steps = total_steps
        self.step_delay_s = step_delay_s
        self.extra_args = kwargs
        self.current_step = 0
        self.is_cancelled = False

    async def emit_step(
        self,
        thought: str,
        step_number: Optional[int] = None,
        status: str = "working",
        artifact: Optional[Dict[str, Any]] = None,
        step_callback: Optional[Callable[[AgentStepAction], Coroutine[Any, Any, None]]] = None,
    ) -> None:
        """Emit intermediate thinking/execution step to coordinator."""
        if step_number is not None:
            self.current_step = step_number
        else:
            self.current_step += 1

        action = AgentStepAction(
            session_id=self.session_id,
            epoch=self.epoch,
            call_id=self.call_id,
            name=self.name,
            role=self.role,
            step=self.current_step,
            total_steps=self.total_steps,
            thought=thought,
            status=status,
            artifact=artifact,
        )

        if step_callback:
            await step_callback(action)

    async def sleep_cancellable(self, duration_s: float) -> None:
        """Sleep with instant cancellation responsiveness."""
        await asyncio.sleep(duration_s)

    @abstractmethod
    async def execute(
        self,
        step_callback: Callable[[AgentStepAction], Coroutine[Any, Any, None]],
    ) -> Dict[str, Any]:
        """Execute the multi-step agentic loop. Must support asyncio cancellation."""
        pass
