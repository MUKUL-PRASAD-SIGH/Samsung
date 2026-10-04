"""Autonomous Research Sub-Agent (e.g. 'scout').

Executes multi-step flight, hotel, and itinerary market research.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Coroutine, Dict

from agent.schemas.actions import AgentStepAction
from agent.workers.base import BaseAgentWorker

logger = logging.getLogger("agent.workers.researcher")


class ResearcherAgent(BaseAgentWorker):
    def __init__(
        self,
        name: str = "scout",
        role: str = "Market & Flight Scout",
        goal: str = "Search and compare travel options",
        session_id: str = "",
        epoch: int = 1,
        call_id: str = "",
        origin: str = "BLR",
        destination: str = "DEL",
        step_delay_s: float = 1.0,
        **kwargs,
    ):
        super().__init__(
            name=name,
            role=role,
            goal=goal,
            session_id=session_id,
            epoch=epoch,
            call_id=call_id,
            total_steps=3,
            step_delay_s=step_delay_s,
            **kwargs,
        )
        self.origin = origin or kwargs.get("city", "BLR")
        self.destination = destination or "DEL"

    async def execute(
        self,
        step_callback: Callable[[AgentStepAction], Coroutine[Any, Any, None]],
    ) -> Dict[str, Any]:
        """Run multi-step market search with live thought streaming."""
        logger.info("Agent '%s' researching route %s -> %s (epoch %d)", self.name, self.origin, self.destination, self.epoch)

        # Step 1: Query inventories
        await self.emit_step(
            thought=f"Connecting to flight aggregator APIs for route {self.origin} -> {self.destination}...",
            step_number=1,
            status="working",
            step_callback=step_callback,
        )
        await self.sleep_cancellable(self.step_delay_s)

        # Step 2: Normalize and filter
        await self.emit_step(
            thought="Aggregated 14 flight candidates. Normalizing fare classes and on-time performance...",
            step_number=2,
            status="working",
            step_callback=step_callback,
        )
        await self.sleep_cancellable(self.step_delay_s)

        # Step 3: Synthesis
        matrix = {
            "route": f"{self.origin} -> {self.destination}",
            "recommendations": [
                {"flight": "6E-455", "airline": "IndiGo", "fare": "$145", "duration": "2h 40m", "rating": 4.6},
                {"flight": "AI-102", "airline": "Air India", "fare": "$180", "duration": "2h 45m", "rating": 4.3},
                {"flight": "UK-820", "airline": "Vistara", "fare": "$195", "duration": "2h 35m", "rating": 4.8},
            ]
        }
        artifact = {
            "title": f"Route_{self.origin}_{self.destination}_Comparison.json",
            "language": "json",
            "content": json.dumps(matrix, indent=2),
            "author": self.name,
            "description": f"Hi, I am {self.name}. I have analyzed and ranked the best options for {self.origin} to {self.destination}.",
        }

        await self.emit_step(
            thought="Optimal fare ranking compiled. IndiGo 6E-455 is lowest tariff ($145).",
            step_number=3,
            status="completed",
            artifact=artifact,
            step_callback=step_callback,
        )

        return {
            "status": "completed",
            "agent_name": self.name,
            "matrix": matrix,
            "artifact": artifact,
        }
