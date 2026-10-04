"""Slot/entity extraction for tool-call turns, MEMORY_UPDATE parsing, and correction semantics."""

import asyncio

import pytest

from agent.coordination.state_machine import SessionState
from agent.coordination.tool_router import ToolRouter
from agent.coordinator import AgentCoordinator
from agent.llm_client import LLMConfig, LLMResponse, MockLLMBackend, _extract_memory_update
from agent.memory.tool_slots import entities_from_tool_call
from agent.schemas.actions import ActionType
from agent.schemas.events import UserTextEvent
from agent.slow_path.planner import Planner


# ---------------------------------------------------------------- tool-argument extraction
def test_entities_from_tool_call_types_and_filtering():
    ents = entities_from_tool_call("book_flight", {"origin": "Delhi", "destination": "Goa", "date": "2026-12-12", "nights": 3, "empty": "  ", "obj": {"a": 1}, "flag": True})
    assert ("LOCATION", "origin", "Delhi") in ents
    assert ("LOCATION", "destination", "Goa") in ents
    assert ("DATE", "date", "2026-12-12") in ents
    assert ("QUANTITY", "nights", 3) in ents
    assert {k for _, k, _ in ents} == {"origin", "destination", "date", "nights"}  # empty/obj/bool skipped


def test_spawn_agent_arguments_are_not_user_entities():
    assert entities_from_tool_call("spawn_agent", {"name": "bob", "role": "x", "goal": "y"}) == []


# ------------------------------------------------------------------- MEMORY_UPDATE parsing
@pytest.mark.parametrize("raw,expected_text,has_update", [
    ('Hi.\n\nMEMORY_UPDATE: {"entities": [], "intent_shift": null}', "Hi.", True),
    ('Hi there.\n\n---\n\nMEMORY_UPDATE: {"entities": []}', "Hi there.", True),          # stray rule removed
    ('Hi.\n```\nMEMORY_UPDATE: {"entities": []}\n```', "Hi.", True),                       # fenced
    ('Hi.\n**MEMORY_UPDATE:** {"entities": []}', "Hi.", True),                             # bold marker
    ('Hi.\nMEMORY_UPDATE: {"entities": [oops', "Hi.", False),                              # malformed: never leaks
    ("Just text.", "Just text.", False),
])
def test_memory_update_parsing_variants(raw, expected_text, has_update):
    text, update = _extract_memory_update(raw)
    assert text == expected_text
    assert "MEMORY_UPDATE" not in text
    assert (update is not None) == has_update


def test_memory_update_text_after_json_is_kept():
    text, update = _extract_memory_update('Hi.\nMEMORY_UPDATE: {"entities": []} Hope that helps!')
    assert update == {"entities": []} and "Hope that helps!" in text


# -------------------------------------------------------------- planner -> slots (tool turns)
def _planner(*responses):
    backend = MockLLMBackend(canned_responses=list(responses))
    return Planner(llm_backend=backend, tool_router=ToolRouter(), config=LLMConfig(backend_type="mock"))


def _tool(name, **args):
    return LLMResponse(response_type="tool_call", tool_name=name, arguments=args)


async def _turn(planner, session, text, agent_response="ok"):
    session.scratchpad.append_utterance_chunk(text)
    await planner.plan(UserTextEvent(session_id=session.session_id, text=text), session)
    turn = session.scratchpad.commit(agent_response=agent_response)
    return session.graph_memory.insert_turn(turn)


@pytest.mark.asyncio
async def test_tool_turn_populates_slots_and_graph_entities():
    session = SessionState("ts1"); session.ensure_memory()
    planner = _planner(_tool("search_flights", origin="Delhi", destination="Mumbai"))
    turn = await _turn(planner, session, "flights Delhi to Mumbai")
    assert session.slots == {"origin": "Delhi", "destination": "Mumbai"}
    assert {e.key for e in session.graph_memory.get_entities_for_turn(turn.id)} == {"origin", "destination"}


@pytest.mark.asyncio
async def test_correction_supersedes_the_turn_that_set_the_old_value():
    session = SessionState("ts2"); session.ensure_memory()
    planner = _planner(
        _tool("search_flights", origin="Delhi", destination="Mumbai"),
        _tool("check_weather", city="Mumbai"),                       # unrelated turn, inherits nothing
        _tool("search_flights", origin="Delhi", destination="Goa"),   # real correction
    )
    t1 = await _turn(planner, session, "flights Delhi to Mumbai")
    t2 = await _turn(planner, session, "weather in Mumbai")
    t3 = await _turn(planner, session, "actually Goa")

    assert session.slots["destination"] == "Goa"
    supersedes = [(e.source, e.target) for e in session.graph_memory.edges if e.edge_type == "SUPERSEDES"]
    assert (t3.id, t1.id) in supersedes                # points at the turn that INTRODUCED Mumbai
    assert all(src != t2.id for src, _ in supersedes)  # the weather turn superseded nothing
    assert all(src != t1.id for src, _ in supersedes)  # first turn superseded nothing


@pytest.mark.asyncio
async def test_aborted_call_entities_never_leak_into_slots():
    session = SessionState("ts3"); session.ensure_memory()
    planner = _planner(
        _tool("search_flights", origin="Delhi", destination="Mumbai"),
        LLMResponse(response_type="spoken_response", content="Okay, cancelled."),
    )
    session.scratchpad.append_utterance_chunk("flights Delhi to Mumbai")
    await planner.plan(UserTextEvent(session_id="ts3", text="flights Delhi to Mumbai"), session)
    assert len(session.scratchpad.entity_candidates) == 2

    session.bump_epoch(reason="user_correction")          # user abandons the pending search
    assert session.scratchpad.entity_candidates == []      # Mumbai discarded with the call

    await _turn(planner, session, "never mind, cancel that", agent_response="Okay, cancelled.")
    assert session.slots == {}
    assert "Mumbai" not in str(session.slots)


@pytest.mark.asyncio
async def test_memory_update_entities_on_spoken_turn_still_work():
    session = SessionState("ts4"); session.ensure_memory()
    planner = _planner(LLMResponse(
        response_type="spoken_response", content="Great choice!",
        memory_update={"entities": [{"type": "LOCATION", "key": "destination", "value": "Jaipur"}], "intent_shift": None},
    ))
    await _turn(planner, session, "thinking of Jaipur")
    assert session.slots == {"destination": "Jaipur"}


# ---------------------------------------------------------------------------- idempotency
def test_different_arguments_are_not_duplicates_but_identical_calls_are():
    s = SessionState("idem1")
    a = s.register_tool_call("c1", "book_flight", {"origin": "DEL", "destination": "BOM"}, is_state_modifying=True)
    b = s.register_tool_call("c2", "book_flight", {"origin": "DEL", "destination": "GOA"}, is_state_modifying=True)
    dup = s.register_tool_call("c3", "book_flight", {"origin": "DEL", "destination": "BOM"}, is_state_modifying=True)
    assert a is not None and b is not None   # previously b was silently dropped as a "duplicate"
    assert dup is None                        # true duplicate (same args, same state) still blocked


# ------------------------------------------------------------- end-to-end via the coordinator
@pytest.mark.asyncio
async def test_coordinator_correction_flow_ends_with_only_the_corrected_slot():
    async def fast(**kw):
        return {"flights": [{"flight": "6E-1", "airline": "IndiGo", "origin": kw.get("origin"), "destination": kw.get("destination"), "price": "$100", "departure": "9 AM"}]}

    router = ToolRouter(register_defaults=False)
    router.register_tool(
        name="search_flights", description="t",
        parameters_schema={"type": "object", "properties": {"origin": {"type": "string"}, "destination": {"type": "string"}}},
        is_state_modifying=False, handler=fast,
    )
    backend = MockLLMBackend(canned_responses=[
        _tool("search_flights", origin="Delhi", destination="Mumbai"),
        _tool("search_flights", origin="Delhi", destination="Goa"),
    ])
    coordinator = AgentCoordinator(tool_router=router, llm_backend=backend, enable_debounce=False)
    await coordinator.start()
    try:
        await coordinator.post_event(UserTextEvent(session_id="e2e", text="flights from Delhi to Mumbai"))
        await asyncio.sleep(0.4)
        await coordinator.post_event(UserTextEvent(session_id="e2e", text="actually change the destination to Goa instead"))
        await asyncio.sleep(0.6)

        session = coordinator.sessions["e2e"]
        assert session.slots == {"origin": "Delhi", "destination": "Goa"}
        graph = session.graph_memory
        assert any(e.edge_type == "SUPERSEDES" for e in graph.edges)
        types = []
        while not coordinator.action_queue.empty():
            types.append(coordinator.action_queue.get_nowait().action_type)
        assert ActionType.GRAPH_SNAPSHOT in types
    finally:
        await coordinator.stop()
