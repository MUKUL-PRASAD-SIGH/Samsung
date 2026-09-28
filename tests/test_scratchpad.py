"""Unit tests for TurnScratchpad (L1 cognitive memory tier)."""

import pytest
from agent.coordination.state_machine import SessionState


@pytest.mark.asyncio
async def test_scratchpad_commit_equals_direct_patch_slots():
    """Regression guard for plan.md's requirement: scratchpad.commit() must produce
    identical session.slots/session._history state as calling session.patch_slots()
    directly -- it must not diverge from the existing, already-tested primitive."""
    session_a = SessionState(session_id="sess_a")
    session_a.patch_slots({"origin": "BLR", "destination": "DEL"}, new_intent="flight_search")

    session_b = SessionState(session_id="sess_b")
    session_b.ensure_memory()
    session_b.scratchpad.record_slot_diff("origin", "BLR")
    session_b.scratchpad.record_slot_diff("destination", "DEL")
    session_b.scratchpad.record_intent_shift("flight_search")
    session_b.scratchpad.commit(agent_response="ok")

    assert session_a.slots == session_b.slots
    assert session_a.intent == session_b.intent
    assert len(session_a._history) == len(session_b._history)


@pytest.mark.asyncio
async def test_scratchpad_last_write_wins_within_a_turn():
    session = SessionState(session_id="sess_c")
    session.ensure_memory()
    session.scratchpad.record_slot_diff("destination", "BLR")
    session.scratchpad.record_slot_diff("destination", "BOM")  # user corrects mid-turn
    canonical_turn = session.scratchpad.commit(agent_response="Booking to BOM")

    assert session.slots == {"destination": "BOM"}
    assert canonical_turn.final_slots == {"destination": "BOM"}
    assert "blr" not in canonical_turn.final_slots.get("destination", "").lower()


@pytest.mark.asyncio
async def test_scratchpad_purges_after_commit():
    session = SessionState(session_id="sess_d")
    session.ensure_memory()
    session.scratchpad.append_utterance_chunk("book a flight")
    session.scratchpad.record_slot_diff("origin", "DEL")
    session.scratchpad.commit(agent_response="done")

    assert session.scratchpad.raw_chunks == []
    assert session.scratchpad.slot_deltas == []
    assert session.scratchpad.entity_candidates == []


@pytest.mark.asyncio
async def test_scratchpad_tracks_aborted_calls_via_bump_epoch():
    session = SessionState(session_id="sess_e")
    session.ensure_memory()
    session.register_tool_call(call_id="call_x", tool_name="search_flights", arguments={})

    session.bump_epoch(reason="user_correction")

    canonical_turn = session.scratchpad.commit(agent_response="Updated")
    assert canonical_turn.aborted_calls_count == 1


@pytest.mark.asyncio
async def test_bare_session_state_without_ensure_memory_is_unaffected():
    """A SessionState that never calls ensure_memory() (e.g. existing tests) must
    behave exactly as before -- bump_epoch must not raise even with scratchpad=None."""
    session = SessionState(session_id="sess_f")
    session.register_tool_call(call_id="call_y", tool_name="get_weather", arguments={})
    cancellations = session.bump_epoch(reason="interrupt")
    assert len(cancellations) == 1
    assert session.scratchpad is None
