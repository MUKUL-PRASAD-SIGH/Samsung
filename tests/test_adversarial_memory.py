"""Phase 5 adversarial tests: rapid slot overwrites, rollback, DAG acyclicity, and a
commit+insert_turn latency microbenchmark (<2.0ms per plan.md's acceptance criteria)."""

import time
import pytest
from agent.coordination.state_machine import SessionState


@pytest.mark.asyncio
async def test_rapid_multi_turn_slot_overwrites():
    session = SessionState(session_id="adv_sess_1")
    session.ensure_memory()

    for destination in ("DEL", "BLR", "BOM", "GOA"):
        session.scratchpad.record_slot_diff("destination", destination)
        canonical_turn = session.scratchpad.commit(agent_response=f"Updated destination to {destination}")
        session.graph_memory.insert_turn(canonical_turn)

    from agent.memory.context_builder import build_context_block

    # Clean Context Invariant: the resolved slot state and its derived context block
    # must reflect only the final correction, not the abandoned intermediate values.
    assert session.slots["destination"] == "GOA"
    assert "del" not in build_context_block(session).lower()
    assert "blr" not in build_context_block(session).lower()
    assert "bom" not in build_context_block(session).lower()


@pytest.mark.asyncio
async def test_conversation_rollback_forget_and_go_back():
    session = SessionState(session_id="adv_sess_2")
    session.ensure_memory()

    session.scratchpad.record_slot_diff("destination", "DEL")
    t1 = session.graph_memory.insert_turn(session.scratchpad.commit(agent_response="Set destination DEL"))

    session.scratchpad.record_slot_diff("destination", "BOM")
    session.graph_memory.insert_turn(session.scratchpad.commit(agent_response="Set destination BOM (Mumbai)"))

    # "forget what I said about Mumbai, go back to step 1"
    session.graph_memory.rollback_to_node(t1.id)

    assert session.slots["destination"] == "DEL"


@pytest.mark.asyncio
async def test_dag_has_no_cycles_after_adversarial_sequence():
    session = SessionState(session_id="adv_sess_3")
    session.ensure_memory()

    turns = []
    for i in range(5):
        session.scratchpad.record_slot_diff("counter", i)
        turns.append(session.graph_memory.insert_turn(session.scratchpad.commit(agent_response=f"step {i}")))

    session.graph_memory.rollback_to_node(turns[1].id)
    session.scratchpad.record_slot_diff("counter", 99)
    session.graph_memory.insert_turn(session.scratchpad.commit(agent_response="after rollback"))

    assert session.graph_memory._would_cycle() is False


@pytest.mark.asyncio
async def test_commit_and_insert_turn_latency_budget():
    session = SessionState(session_id="adv_sess_4")
    session.ensure_memory()

    # Warm up (first call may pay import/lazy-init costs not representative of steady state)
    session.scratchpad.record_slot_diff("warmup", 1)
    session.graph_memory.insert_turn(session.scratchpad.commit(agent_response="warmup"))

    start = time.perf_counter()
    session.scratchpad.record_slot_diff("destination", "BLR")
    canonical_turn = session.scratchpad.commit(agent_response="Booking to BLR")
    session.graph_memory.insert_turn(canonical_turn)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms < 2.0, f"scratchpad.commit()+insert_turn() took {elapsed_ms:.3f}ms, budget is 2.0ms"
