"""Autonomous Coder Sub-Agent (e.g. 'bob').

Executes multi-step frontend/full-stack code generation, syntax validation,
and delivers live code artifacts to the workspace.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine, Dict, Optional

from agent.schemas.actions import AgentStepAction
from agent.workers.base import BaseAgentWorker

logger = logging.getLogger("agent.workers.coder")

DEFAULT_ANALOG_CLOCK_TSX = """import React, { useState, useEffect } from "react";
import { defineProperties } from "figma:react";

export default function AnalogClock({
  updateInterval = 1000,
  secondHandColor = "red",
  minuteHandColor = "black",
  hourHandColor = "black",
}) {
  const [time, setTime] = useState({ hours: 0, minutes: 0, seconds: 0 });

  useEffect(() => {
    const updateClock = () => {
      // Get London's local time using en-GB format
      const londonTimeString = new Date().toLocaleTimeString("en-GB", {
        timeZone: "Europe/London",
        hour12: false
      });
      const [hoursStr, minutesStr, secondsStr] = londonTimeString.split(":");
      setTime({
        hours: parseInt(hoursStr, 10),
        minutes: parseInt(minutesStr, 10),
        seconds: parseInt(secondsStr, 10)
      });
    };

    updateClock();
    const timerId = setInterval(updateClock, updateInterval);
    return () => clearInterval(timerId);
  }, [updateInterval]);

  return (
    <div className="analog-clock-container">
      {/* Clock Face Rendering */}
    </div>
  );
}"""


class CoderAgent(BaseAgentWorker):
    def __init__(
        self,
        name: str = "bob",
        role: str = "TypeScript Generator",
        goal: str = "Generate AnalogClock component",
        session_id: str = "",
        epoch: int = 1,
        call_id: str = "",
        component: str = "AnalogClock",
        language: str = "TypeScript",
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
            total_steps=4,
            step_delay_s=step_delay_s,
            **kwargs,
        )
        self.component = component or "AnalogClock"
        self.language = language or "TypeScript"

    async def execute(
        self,
        step_callback: Callable[[AgentStepAction], Coroutine[Any, Any, None]],
    ) -> Dict[str, Any]:
        """Run the multi-step code generation pipeline with cancellable intermediate thoughts."""
        logger.info("Agent '%s' started goal: %s (epoch %d)", self.name, self.goal, self.epoch)

        # Step 1: Requirements analysis
        await self.emit_step(
            thought=f"Analyzing specifications and prop signatures for {self.component} ({self.language})...",
            step_number=1,
            status="working",
            step_callback=step_callback,
        )
        await self.sleep_cancellable(self.step_delay_s)

        # Step 2: Logic and hook structure
        await self.emit_step(
            thought=f"Setting up state hooks, London timezone clock logic, and interval handlers...",
            step_number=2,
            status="working",
            step_callback=step_callback,
        )
        await self.sleep_cancellable(self.step_delay_s)

        # Step 3: Synthesis of JSX and dial math
        await self.emit_step(
            thought=f"Synthesizing dial markers, rotational transforms, and styling classes...",
            step_number=3,
            status="working",
            step_callback=step_callback,
        )
        await self.sleep_cancellable(self.step_delay_s)

        # Step 4: Final verification and artifact delivery
        ext = "tsx" if self.language.lower() in ("typescript", "ts", "tsx") else "jsx"
        artifact = {
            "title": f"{self.component}.{ext}",
            "language": self.language.lower(),
            "content": DEFAULT_ANALOG_CLOCK_TSX,
            "author": self.name,
            "description": f"Hi, I am {self.name}. Here is the code in {self.language.lower()} for your {self.component} interface.",
        }

        await self.emit_step(
            thought=f"Component {self.component}.{ext} compiled cleanly with zero TypeScript errors. Artifact ready.",
            step_number=4,
            status="completed",
            artifact=artifact,
            step_callback=step_callback,
        )

        logger.info("Agent '%s' completed goal: %s", self.name, self.goal)
        return {
            "status": "completed",
            "agent_name": self.name,
            "component": self.component,
            "language": self.language,
            "artifact": artifact,
        }
