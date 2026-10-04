"""Integration and Unit Tests for Autonomous Sub-Agent Workers and Live Thought Streaming."""

import asyncio
import pytest
from agent.workers.coder_agent import CoderAgent
from agent.workers.researcher_agent import ResearcherAgent
from agent.schemas.actions import ActionType, AgentStepAction, ToolCancelAction
from agent.coordinator import AgentCoordinator
from agent.schemas.events import InterruptSignalEvent


@pytest.mark.asyncio
async def test_coder_agent_full_execution():
    """Verify CoderAgent runs multi-step lifecycle, streams thoughts, and yields artifact."""
    emitted_steps = []

    async def step_cb(step_action: AgentStepAction):
        emitted_steps.append(step_action)

    agent = CoderAgent(
        name="bob",
        role="TypeScript Generator",
        goal="Build AnalogClock in TypeScript",
        session_id="test_sess",
        epoch=1,
        call_id="call_bob_1",
        component="AnalogClock",
        language="TypeScript",
        step_delay_s=0.01,  # Fast execution for test suite
    )

    result = await agent.execute(step_callback=step_cb)

    assert result["status"] == "completed"
    assert result["agent_name"] == "bob"
    assert "artifact" in result
    assert result["artifact"]["title"] == "AnalogClock.tsx"
    assert "AnalogClock" in result["artifact"]["content"]

    # Verify all 4 steps were emitted
    assert len(emitted_steps) == 4
    step_nums = [s.step for s in emitted_steps]
    assert step_nums == [1, 2, 3, 4]
    assert emitted_steps[-1].status == "completed"
    assert emitted_steps[-1].artifact is not None


@pytest.mark.asyncio
async def test_researcher_agent_execution():
    """Verify ResearcherAgent executes multi-step search and compiles comparison matrix."""
    emitted = []

    async def step_cb(action):
        emitted.append(action)

    agent = ResearcherAgent(
        name="scout",
        role="Market & Flight Scout",
        goal="Compare flights",
        session_id="test_sess",
        epoch=1,
        call_id="call_scout_1",
        origin="BLR",
        destination="DEL",
        step_delay_s=0.01,
    )

    result = await agent.execute(step_callback=step_cb)

    assert result["status"] == "completed"
    assert "matrix" in result
    assert result["matrix"]["route"] == "BLR -> DEL"
    assert len(result["matrix"]["recommendations"]) >= 2
    assert len(emitted) == 3


@pytest.mark.asyncio
async def test_coder_agent_mid_step_cancellation():
    """Verify agent worker cancellation cleanly aborts execution on interruption."""
    agent = CoderAgent(
        name="bob",
        role="TypeScript Generator",
        goal="Build complex component",
        session_id="test_sess",
        epoch=1,
        call_id="call_bob_cancel",
        step_delay_s=0.5,  # Long delay
    )

    async def step_cb(action):
        pass

    task = asyncio.create_task(agent.execute(step_callback=step_cb))

    # Wait for first step to initiate
    await asyncio.sleep(0.05)

    # Cancel task (simulating epoch bump on user barge-in)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_coordinator_spawn_agent_and_interruption():
    """End-to-End Test:
    1. Coordinator dispatches spawn_agent.
    2. Agent steps stream to action queue.
    3. User barges in with interrupt signal.
    4. Coordinator bumps epoch and cancels agent worker task immediately.
    """
    coordinator = AgentCoordinator()
    await coordinator.start()

    session_id = "test_agent_e2e"

    try:
        # Step 1: Dispatch spawn_agent tool call
        await coordinator.dispatch_tool_call(
            session_id=session_id,
            tool_name="spawn_agent",
            arguments={
                "name": "bob",
                "role": "TypeScript Generator",
                "goal": "Build AnalogClock in TypeScript",
                "component": "AnalogClock",
                "language": "TypeScript",
                "step_delay_s": 0.2,  # 200ms per step
            },
            call_id="call_bob_e2e",
        )

        # Allow worker to start and emit step 1
        await asyncio.sleep(0.08)

        # Step 2: Barge in with user interruption
        await coordinator.post_event(
            InterruptSignalEvent(
                session_id=session_id,
                reason="user_changed_mind",
            )
        )

        # Allow coordinator to process interruption
        await asyncio.sleep(0.08)

        # Step 3: Drain action queue and inspect actions
        actions = []
        while not coordinator.action_queue.empty():
            actions.append(await coordinator.get_next_action())

        action_types = [a.action_type for a in actions]
        assert ActionType.TOOL_CALL in action_types
        assert ActionType.AGENT_STEP in action_types
        assert ActionType.TOOL_CANCEL in action_types

        # Verify cancellation action
        cancels = [a for a in actions if isinstance(a, ToolCancelAction)]
        assert len(cancels) == 1
        assert cancels[0].call_id == "call_bob_e2e"
        assert cancels[0].epoch == 2

        # Verify session state reflects cancellation
        session = coordinator.get_or_create_session(session_id)
        assert session.epoch == 2
        call_info = session.in_flight_calls["call_bob_e2e"]
        assert call_info.status == "cancelled"

    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_dynamic_custom_agent_synthesis():
    """Verify DynamicAgentWorker executes custom steps and outputs bespoke artifact."""
    from agent.workers.dynamic_agent import DynamicAgentWorker

    steps = [
        "Analyzing geospatial query patterns and trip lifecycle...",
        "Structuring PostGIS spatial indexes and partitioning scheme...",
        "Compiling production PostgreSQL schema...",
    ]

    emitted_steps = []

    async def step_cb(action):
        emitted_steps.append(action)

    agent = DynamicAgentWorker(
        name="db_architect",
        role="PostgreSQL Scaling Specialist",
        goal="Design ride-sharing schema with PostGIS spatial indexing",
        session_id="test_custom_sess",
        epoch=1,
        call_id="call_db_01",
        steps=steps,
        expected_artifact={"title": "rideshare_schema.sql", "language": "sql"},
        step_delay_s=0.01,
    )

    result = await agent.execute(step_callback=step_cb)

    assert result["status"] == "completed"
    assert result["agent_name"] == "db_architect"
    assert result["role"] == "PostgreSQL Scaling Specialist"
    assert "artifact" in result
    art = result["artifact"]
    assert art["title"] == "rideshare_schema.sql"
    assert art["language"] == "sql"
    assert "postgis" in art["content"].lower()
    assert "riders" in art["content"].lower()

    # Verify custom steps were streamed
    assert len(emitted_steps) == 3
    assert emitted_steps[0].thought == steps[0]
    assert emitted_steps[1].thought == steps[1]
    assert emitted_steps[2].status == "completed"


@pytest.mark.asyncio
async def test_dynamic_agent_svg_creation():
    """Verify DynamicAgentWorker synthesizes visual SVG artifacts."""
    from agent.workers.dynamic_agent import DynamicAgentWorker

    agent = DynamicAgentWorker(
        name="vector_craft",
        role="SVG Motion & DataViz Engineer",
        goal="Create an interactive telemetry tachometer with neon cyan glow",
        session_id="test_svg_sess",
        epoch=1,
        call_id="call_svg_01",
        expected_artifact={"title": "TachometerGauge.svg", "language": "svg"},
        step_delay_s=0.01,
    )

    emitted = []
    result = await agent.execute(step_callback=lambda a: emitted.append(a) or asyncio.sleep(0))

    assert result["status"] == "completed"
    art = result["artifact"]
    assert art["language"] == "svg"
    assert "<svg" in art["content"]
    assert "RPM" in art["content"]

