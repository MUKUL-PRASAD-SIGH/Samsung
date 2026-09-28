"""Unit tests for GraphMemory (L2 cognitive memory tier)."""

import pytest
from agent.coordination.state_machine import SessionState


def _commit_turn(session, slot_diffs, agent_response, artifacts=None):
    for slot, value in slot_diffs.items():
        session.scratchpad.record_slot_diff(slot, value)
    canonical_turn = session.scratchpad.commit(agent_response=agent_response, artifacts=artifacts)
    return session.graph_memory.insert_turn(canonical_turn)


@pytest.mark.asyncio
async def test_insert_turn_builds_next_turn_chain():
    session = SessionState(session_id="graph_sess_1")
    session.ensure_memory()

    t1 = _commit_turn(session, {"origin": "DEL"}, "Got it, flying from DEL.")
    t2 = _commit_turn(session, {"destination": "BOM"}, "Booking DEL to BOM.")

    thread = session.graph_memory.get_active_thread()
    assert [n.id for n in thread] == [t1.id, t2.id]

    next_turn_edges = [e for e in session.graph_memory.edges if e.edge_type == "NEXT_TURN"]
    assert any(e.source == t1.id and e.target == t2.id for e in next_turn_edges)


@pytest.mark.asyncio
async def test_entity_and_artifact_linking():
    session = SessionState(session_id="graph_sess_2")
    session.ensure_memory()

    session.scratchpad.record_entity_candidate("LOCATION", "destination", "BOM")
    turn = _commit_turn(
        session,
        {},
        "Here is your component.",
        artifacts=[{"title": "AnalogClock.tsx", "language": "typescript", "content": "export const Clock = () => null;"}],
    )

    entities = session.graph_memory.get_entities_for_turn(turn.id)
    assert len(entities) == 1
    assert entities[0].key == "destination" and entities[0].value == "BOM"

    artifact_nodes = [a for a in session.graph_memory.artifacts.values() if a.source_turn_id == turn.id]
    assert len(artifact_nodes) == 1
    assert artifact_nodes[0].title == "AnalogClock.tsx"

    produced_edges = [e for e in session.graph_memory.edges if e.edge_type == "PRODUCED"]
    assert any(e.source == turn.id and e.target == artifact_nodes[0].id for e in produced_edges)


@pytest.mark.asyncio
async def test_rollback_to_node_restores_slots_via_patch_slots():
    session = SessionState(session_id="graph_sess_3")
    session.ensure_memory()

    t1 = _commit_turn(session, {"destination": "BLR"}, "Booking to BLR.")
    _commit_turn(session, {"destination": "BOM"}, "Actually, booking to BOM.")
    assert session.slots["destination"] == "BOM"

    branch = session.graph_memory.rollback_to_node(t1.id)

    assert session.slots == t1.slots_snapshot
    assert session.slots["destination"] == "BLR"
    assert session.graph_memory.turns[branch.id].pruned is False
    # The superseded turn should now be marked pruned in the active thread view
    active_ids = {n.id for n in session.graph_memory.get_active_thread()}
    assert t1.id not in active_ids or branch.id in active_ids


@pytest.mark.asyncio
async def test_clean_context_invariant_after_correction():
    """Clean Context Invariant (plan.md §9): after a correction, the CURRENT resolved
    slot state (what future tool calls/prompts key off) must not contain the
    abandoned value. Past turns' transcript text legitimately still says "BLR" --
    that's accurate history, not pollution; the invariant is about session.slots
    and the system-prompt context block built from it, not scrubbing history."""
    from agent.memory.context_builder import build_context_block

    session = SessionState(session_id="graph_sess_4")
    session.ensure_memory()

    _commit_turn(session, {"destination": "BLR"}, "Booking flight to BLR.")
    _commit_turn(session, {"destination": "BOM"}, "Actually booking to BOM instead.")

    assert session.slots["destination"] == "BOM"
    assert "blr" not in build_context_block(session).lower()


@pytest.mark.asyncio
async def test_no_cycles_in_conversation_dag():
    session = SessionState(session_id="graph_sess_5")
    session.ensure_memory()

    t1 = _commit_turn(session, {"a": 1}, "ok1")
    _commit_turn(session, {"b": 2}, "ok2")
    _commit_turn(session, {"c": 3}, "ok3")
    session.graph_memory.rollback_to_node(t1.id)
    _commit_turn(session, {"d": 4}, "ok4")

    assert session.graph_memory._would_cycle() is False
