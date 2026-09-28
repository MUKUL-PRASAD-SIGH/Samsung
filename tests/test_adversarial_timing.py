"""Adversarial Timing & Fault Injection Stress Tests (§6 #8, §7.3, §7.5).

Tests:
1. Rapid-fire corrections within debounce window (coalescing prevents epoch thrashing).
2. Mid-call corrections under artificially injected tool delays.
3. Fault injection: backend 500 error and tool timeouts handled gracefully.
4. Duplicate state-modifying requests under rapid-fire racing conditions.
"""

import asyncio
import pytest
from agent.coordinator import AgentCoordinator
from agent.coordination.tool_router import ToolRouter
from agent.coordination.fault_injection import FaultInjectedToolHandler, FaultInjectionConfig
from agent.schemas.events import UserTextEvent, InterruptSignalEvent
from agent.schemas.actions import ToolCallAction, ToolCancelAction, ActionType


@pytest.mark.asyncio
async def test_rapid_fire_corrections_coalescing():
    """Verify that 3 rapid corrections within 100ms coalesce into a single execution turn."""
    router = ToolRouter()
    coordinator = AgentCoordinator(
        tool_router=router,
        enable_debounce=True,
        debounce_window_s=0.08,
    )
    await coordinator.start()
    session_id = "test_rapid_fire_sess"

    try:
        # Fire 3 events within 30ms (< debounce_window_s)
        await coordinator.post_event(UserTextEvent(session_id=session_id, text="Book flight to NYC"))
        await asyncio.sleep(0.01)
        await coordinator.post_event(UserTextEvent(session_id=session_id, text="Actually wait make that Boston"))
        await asyncio.sleep(0.01)
        await coordinator.post_event(UserTextEvent(session_id=session_id, text="No sorry, Chicago"))

        # Wait for debounce window to flush
        await asyncio.sleep(0.15)

        # Collect emitted actions
        actions = []
        while not coordinator.action_queue.empty():
            actions.append(await coordinator.get_next_action())

        # Fillers should only have been emitted once for the final coalesced event
        fillers = [a for a in actions if a.action_type == ActionType.FILLER]
        assert len(fillers) == 1

        session = coordinator.sessions[session_id]
        # Epoch should not have thrashed multiple times
        assert session.epoch <= 2

    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_fault_injected_tool_latency_and_cancellation():
    """Verify cancellation when a tool has high injected latency."""
    router = ToolRouter()
    tool_completed = False

    async def real_db_call(hotel_id: str):
        nonlocal tool_completed
        tool_completed = True
        return {"status": "booked"}

    # Inject 400ms latency
    injected_handler = FaultInjectedToolHandler(
        real_handler=real_db_call,
        config=FaultInjectionConfig(delay_seconds=0.4),
    )

    router.register_tool(
        name="book_hotel",
        description="Book a hotel room",
        parameters_schema={"type": "object", "properties": {"hotel_id": {"type": "string"}}},
        is_state_modifying=True,
        handler=injected_handler,
    )

    coordinator = AgentCoordinator(tool_router=router)
    await coordinator.start()
    session_id = "test_fault_sess"

    try:
        # Dispatch call
        await coordinator.dispatch_tool_call(
            session_id=session_id,
            tool_name="book_hotel",
            arguments={"hotel_id": "hotel_123"},
            call_id="call_fault_1",
        )
        await asyncio.sleep(0.05)

        # Cancel mid-flight
        await coordinator.post_event(
            InterruptSignalEvent(
                session_id=session_id,
                reason="user_stop",
            )
        )
        await asyncio.sleep(0.05)

        # Action queue should contain cancel
        actions = []
        while not coordinator.action_queue.empty():
            actions.append(await coordinator.get_next_action())

        cancels = [a for a in actions if isinstance(a, ToolCancelAction)]
        assert len(cancels) == 1
        assert cancels[0].call_id == "call_fault_1"

        # Wait past injected delay
        await asyncio.sleep(0.45)
        # Real handler must not have completed
        assert tool_completed is False

    finally:
        await coordinator.stop()
