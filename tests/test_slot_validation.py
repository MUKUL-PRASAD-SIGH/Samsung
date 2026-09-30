"""Spec §7.4: bad slot patches are refused or rolled back, never propagated; invalid tool calls are not dispatched."""

import pytest

from agent.coordination.slot_validation import SlotPatchError, validate_patch
from agent.coordination.state_machine import SessionState
from agent.coordination.tool_router import ToolRouter, UnknownToolError
from agent.llm_client import LLMResponse, MockLLMBackend
from agent.schemas.actions import ActionType
from agent.schemas.events import UserTextEvent
from agent.slow_path.planner import Planner


@pytest.mark.parametrize("diff", [
    {"nights": 0}, {"nights": 400}, {"nights": "3"}, {"nights": 2.5},
    {"date": "2026-02-30"}, {"date": "1850-01-01"},
    {"city": "x" * 500}, {"city": "Goa\x00"}, {"city": ["a"]}, {"city": {"a": 1}}, {"city": True},
])
def test_bad_patches_are_rejected(diff):
    with pytest.raises(SlotPatchError):
        validate_patch(diff)


@pytest.mark.parametrize("diff", [
    {"nights": 3}, {"date": "next Friday"}, {"date": "2026-12-25"}, {"city": "São Paulo"}, {"city": None}, {}])
def test_good_patches_pass(diff):
    validate_patch(diff)


def test_rejected_patch_leaves_state_untouched():
    s = SessionState("v1")
    s.patch_slots({"origin": "Delhi", "destination": "Mumbai"})
    before, history = dict(s.slots), len(s._history)
    with pytest.raises(SlotPatchError):
        s.patch_slots({"nights": 0, "destination": "Goa"})
    assert s.slots == before and len(s._history) == history


def test_bad_resulting_state_is_rolled_back_through_the_snapshot_ring():
    s = SessionState("v2")
    s.patch_slots({"origin": "Delhi", "destination": "Mumbai"}, new_intent="search_flights")
    with pytest.raises(SlotPatchError) as e:
        s.patch_slots({"destination": "DEL"}, new_intent="book_flight")   # Delhi -> Delhi once canonicalised
    assert s.slots == {"origin": "Delhi", "destination": "Mumbai"} and s.intent == "search_flights"
    assert "Delhi" in e.value.user_message


def test_staging_a_bad_call_leaves_no_undo_record():
    s = SessionState("v3")
    with pytest.raises(SlotPatchError):
        s.stage_call_slots("c1", {"origin": "Goa", "destination": "goa"})
    assert s.slots == {} and "c1" not in s._call_slot_undo


def test_unknown_tool_is_reported():
    with pytest.raises(UnknownToolError):
        ToolRouter().validate_call("launch_missiles", {})


def _planner(resp):
    return Planner(MockLLMBackend([resp]), ToolRouter())


@pytest.mark.parametrize("resp", [
    LLMResponse(response_type="tool_call", tool_name="launch_missiles", arguments={}),
    LLMResponse(response_type="tool_call", tool_name="search_flights", arguments={"origin": "Delhi"}),          # missing required
    LLMResponse(response_type="tool_call", tool_name="search_hotels", arguments={"city": "Goa", "nights": "x"}),  # wrong type
    LLMResponse(response_type="tool_call", tool_name="search_hotels", arguments={"city": "Goa", "nights": 0}),    # bad value
    LLMResponse(response_type="tool_call", tool_name="search_flights", arguments={"origin": "Delhi", "destination": "DEL"}),
])
async def test_invalid_tool_calls_become_clarifications_not_dispatches(resp):
    s = SessionState("p1")
    actions = await _planner(resp).plan(UserTextEvent(session_id="p1", text="do it"), s)
    kinds = [a.action_type for a in actions]
    assert ActionType.TOOL_CALL not in kinds and ActionType.CLARIFICATION in kinds
    assert not s.in_flight_calls and s.slots == {}


async def test_valid_call_still_dispatches():
    s = SessionState("p2")
    resp = LLMResponse(response_type="tool_call", tool_name="search_flights", arguments={"origin": "Delhi", "destination": "Mumbai"})
    actions = await _planner(resp).plan(UserTextEvent(session_id="p2", text="flights"), s)
    assert ActionType.TOOL_CALL in [a.action_type for a in actions]
