"""Entrypoint for the interruptible real-time agent system.

Wires the asynchronous event queue, coordination layer, and action queue,
and exposes the agent interface for evaluation harnesses and interactive runs.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Optional

from agent.coordinator import AgentCoordinator
from agent.coordination.tool_router import ToolRouter
from agent.trace_logger import TraceLogger
from agent.schemas.events import UserTextEvent, InterruptSignalEvent


def create_agent(log_file: Optional[str] = "trace.jsonl") -> AgentCoordinator:
    """Factory to instantiate the agent coordinator with default routing and tracing."""
    router = ToolRouter()
    tracer = TraceLogger(log_file=log_file)
    return AgentCoordinator(tool_router=router, trace_logger=tracer)


async def main() -> None:
    """Simple interactive CLI runner for manual testing."""
    coordinator = create_agent()
    await coordinator.start()

    session_id = "interactive_session"
    print("Interruptible Agent initialized. Type commands, 'interrupt' to barge in, or 'quit' to exit.")

    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            text = line.strip()
            if not text:
                continue
            if text.lower() == "quit":
                break

            if text.lower() == "interrupt":
                await coordinator.post_event(
                    InterruptSignalEvent(
                        session_id=session_id,
                        reason="manual_interrupt",
                    )
                )
            else:
                await coordinator.post_event(
                    UserTextEvent(
                        session_id=session_id,
                        text=text,
                    )
                )

            # Consume actions emitted until queue is empty
            await asyncio.sleep(0.05)
            while not coordinator.action_queue.empty():
                action = await coordinator.get_next_action()
                print(f"[ACTION] {action.action_type.value.upper()}: {action.model_dump_json()}")

    finally:
        await coordinator.stop()


if __name__ == "__main__":
    asyncio.run(main())
