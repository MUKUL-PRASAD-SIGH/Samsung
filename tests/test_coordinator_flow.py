"""Integration tests for AgentCoordinator: Event Queue, Action Queue, and Interruption Handling."""

import asyncio
import pytest
from agent.coordinator import AgentCoordinator
from agent.coordination.tool_router import ToolRouter
from agent.schemas.events import UserTextEvent, InterruptSignalEvent
from agent.schemas.actions import (
    ActionType,
    ToolCallAction,
    ToolCancelAction,
    StateSnapshotAction,
)


@pytest.mark.asyncio
async def test_coordinator_mid_call_interruption():
    """Test full-duplex mid-sentence interruption:
    1. Tool call is initiated and takes 300ms.
    2. At 50ms, user issues an interruption event.
    3. Coordinator must emit ToolCancelAction with matching call_id and epoch immediately.
    4. Tool background task is cancelled cleanly.
    5. Action queue receives cancellation before tool can complete.
    """
    router = ToolRouter()
    tool_executed = False

    async def mock_slow_tool(query: str):
        nonlocal tool_executed
        await asyncio.sleep(0.3)
        tool_executed = True
        return {"result": f"Found {query}"}

    router.register_tool(
        name="search_database",
        description="Slow database lookup",
        parameters_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        is_state_modifying=False,
        handler=mock_slow_tool,
    )

    coordinator = AgentCoordinator(tool_router=router)
    await coordinator.start()

    session_id = "test_duplex_session"

    try:
        # Step 1: Dispatch tool call
        await coordinator.dispatch_tool_call(
            session_id=session_id,
            tool_name="search_database",
            arguments={"query": "flight BLR to DEL"},
            call_id="call_async_1",
        )

        # Allow event loop a few ms to start background task
        await asyncio.sleep(0.05)

        # Step 2: Barge-in interruption from user
        await coordinator.post_event(
            InterruptSignalEvent(
                session_id=session_id,
                reason="user_barge_in",
            )
        )

        # Step 3: Wait briefly to process interruption
        await asyncio.sleep(0.05)

        # Drain actions from queue
        received_actions = []
        while not coordinator.action_queue.empty():
            action = await coordinator.get_next_action()
            received_actions.append(action)

        action_types = [a.action_type for a in received_actions]
        assert ActionType.TOOL_CALL in action_types
        assert ActionType.TOOL_CANCEL in action_types
        assert ActionType.STATE_SNAPSHOT in action_types

        # Verify cancellation action details
        cancels = [a for a in received_actions if isinstance(a, ToolCancelAction)]
        assert len(cancels) == 1
        cancel = cancels[0]
        assert cancel.call_id == "call_async_1"
        assert cancel.tool_name == "search_database"
        assert cancel.epoch == 2  # Bumper from epoch 1 to 2

        # Step 4: Wait out the remaining 300ms to ensure the cancelled tool didn't run to completion
        await asyncio.sleep(0.35)
        assert tool_executed is False, "Tool was cancelled and should not have set tool_executed to True"

    finally:
        await coordinator.stop()
