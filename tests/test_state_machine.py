"""Unit tests for SessionState, Epoch cancellation, Idempotency, and Snapshot rollback."""

import asyncio
import pytest
from agent.coordination.state_machine import SessionState
from agent.schemas.actions import ToolCancelAction


@pytest.mark.asyncio
async def test_session_state_initialization():
    session = SessionState(session_id="test_sess_1")
    assert session.session_id == "test_sess_1"
    assert session.epoch == 1
    assert session.slots == {}
    assert session.in_flight_calls == {}

    snapshot = session.get_snapshot()
    assert snapshot.session_id == "test_sess_1"
    assert snapshot.epoch == 1
    assert snapshot.in_flight_calls == []


@pytest.mark.asyncio
async def test_epoch_bump_cancels_stale_calls():
    session = SessionState(session_id="test_sess_2")
    
    # Simulate a slow running background task
    async def slow_work():
        await asyncio.sleep(10.0)

    task = asyncio.create_task(slow_work())
    call_action = session.register_tool_call(
        call_id="call_001",
        tool_name="search_flights",
        arguments={"origin": "SFO", "destination": "JFK"},
        is_state_modifying=False,
    )
    assert call_action is not None
    assert call_action.epoch == 1
    session.attach_task("call_001", task)

    # In flight calls should reflect 1 pending/running call
    assert len(session.get_snapshot().in_flight_calls) == 1

    # Bump epoch (simulating user barge-in / correction)
    cancellations = session.bump_epoch(reason="user_changed_mind")
    
    # Assertions
    assert session.epoch == 2
    assert len(cancellations) == 1
    cancel = cancellations[0]
    assert isinstance(cancel, ToolCancelAction)
    assert cancel.call_id == "call_001"
    assert cancel.tool_name == "search_flights"
    assert cancel.epoch == 2
    assert cancel.reason == "user_changed_mind"

    # Background task must be cancelled. (Task.cancelling() only exists on 3.11+, so let the cancellation land and look.)
    await asyncio.sleep(0)
    assert task.cancelled()
    assert session.in_flight_calls["call_001"].status == "cancelled"
    # No pending calls in active snapshot list
    assert len(session.get_snapshot().in_flight_calls) == 0


@pytest.mark.asyncio
async def test_stale_tool_completion_is_discarded():
    session = SessionState(session_id="test_sess_3")
    session.register_tool_call(
        call_id="call_002",
        tool_name="get_weather",
        arguments={"city": "Seattle"},
    )
    
    # Bump epoch to 2
    session.bump_epoch(reason="interrupt")
    assert session.epoch == 2

    # Attempt to complete call_002 (which belonged to epoch 1)
    completed = session.complete_tool_call("call_002", result={"temp": 65})
    assert completed is False
    assert session.in_flight_calls["call_002"].status == "cancelled"


@pytest.mark.asyncio
async def test_idempotency_prevents_duplicate_state_modifying_calls():
    session = SessionState(session_id="test_sess_4")
    session.intent = "book_hotel"
    session.slots = {"city": "Paris", "nights": 3}

    # Register first state-modifying call
    call1 = session.register_tool_call(
        call_id="call_book_1",
        tool_name="book_hotel",
        arguments={"city": "Paris", "nights": 3},
        is_state_modifying=True,
    )
    assert call1 is not None
    assert call1.is_state_modifying is True
    assert call1.idempotency_key is not None

    # Attempt to register duplicate with identical configuration in same epoch
    call2 = session.register_tool_call(
        call_id="call_book_2",
        tool_name="book_hotel",
        arguments={"city": "Paris", "nights": 3},
        is_state_modifying=True,
    )
    # Must be blocked by idempotency store
    assert call2 is None
    assert "call_book_2" not in session.in_flight_calls


@pytest.mark.asyncio
async def test_slot_diff_patching_and_rollback():
    session = SessionState(session_id="test_sess_5")
    session.patch_slots({"origin": "BLR"}, new_intent="flight_search")
    assert session.slots == {"origin": "BLR"}
    assert session.intent == "flight_search"

    # Multi-turn diff update (e.g. user specifies destination later)
    session.patch_slots({"destination": "DEL"})
    assert session.slots == {"origin": "BLR", "destination": "DEL"}

    # Another turn updates date
    session.patch_slots({"date": "2026-10-12"})
    assert session.slots == {"origin": "BLR", "destination": "DEL", "date": "2026-10-12"}

    # Test rollback (§7.4)
    rolled_back = session.rollback_snapshot()
    assert rolled_back is True
    # Should revert date addition
    assert session.slots == {"origin": "BLR", "destination": "DEL"}
    assert "date" not in session.slots
