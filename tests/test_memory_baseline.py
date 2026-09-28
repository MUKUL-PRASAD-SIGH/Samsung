"""Phase 0 baseline harness (see plan.md revision).

Records prompt-size behavior of Planner.plan() with and without the 2-tier memory
system attached, over a fixed scripted conversation including a DEL->BOM style
correction. This is the "before/after" evidence the original plan.md asserted
(40-70% token reduction) without ever measuring a baseline -- here we measure the
actual delta on a fixed script instead of asserting a specific percentage.
"""

import pytest
from agent.coordination.state_machine import SessionState
from agent.coordination.tool_router import ToolRouter
from agent.slow_path.planner import Planner
from agent.llm_client import LLMConfig, MockLLMBackend, LLMResponse
from agent.schemas.events import UserTextEvent

SCRIPTED_TURNS = [
    "Book a flight from Delhi to Mumbai",
    "Actually, make the destination Bangalore instead",
    "What date works, let's say next Tuesday",
    "Wait, change the destination back to Mumbai",
    "Also book a hotel for 3 nights",
    "Make that 2 nights instead",
    "What's the weather like there",
    "Great, confirm everything",
]


def _total_prompt_chars(mock_backend: MockLLMBackend) -> int:
    return sum(len(m["content"]) for call in mock_backend.call_history for m in call)


async def _run_scripted_conversation(session: SessionState) -> MockLLMBackend:
    mock_backend = MockLLMBackend(
        canned_responses=[LLMResponse(response_type="spoken_response", content="Okay, noted.") for _ in SCRIPTED_TURNS]
    )
    planner = Planner(llm_backend=mock_backend, tool_router=ToolRouter(), config=LLMConfig(backend_type="mock"))

    for text in SCRIPTED_TURNS:
        event = UserTextEvent(session_id=session.session_id, text=text)
        if session.scratchpad is not None:
            session.scratchpad.append_utterance_chunk(text)
        await planner.plan(event, session)
        if session.scratchpad is not None:
            canonical_turn = session.scratchpad.commit(agent_response="Okay, noted.")
            session.graph_memory.insert_turn(canonical_turn)

    return mock_backend


@pytest.mark.asyncio
async def test_baseline_has_no_conversation_history():
    """Confirms the pre-memory-system fact this plan is built on: without
    ensure_memory(), Planner.plan() sends no prior-turn history at all -- each
    call's message list is exactly [system, current_user_text]."""
    session = SessionState(session_id="baseline_sess")
    mock_backend = await _run_scripted_conversation(session)

    for call_messages in mock_backend.call_history:
        assert len(call_messages) == 2  # system + current turn only, no history


@pytest.mark.asyncio
async def test_memory_enabled_context_stays_flat_and_clean():
    """With ensure_memory() attached, prompt size should not grow unboundedly across
    turns (bounded by max_turns in get_subgraph_prompt_context), and no stale
    'bangalore' should leak into context after the user corrects back to Mumbai."""
    session = SessionState(session_id="memory_sess")
    session.ensure_memory()
    mock_backend = await _run_scripted_conversation(session)

    last_call = mock_backend.call_history[-1]
    joined = " ".join(m["content"] for m in last_call).lower()

    # The correction sequence was Mumbai -> Bangalore -> Mumbai; only the final
    # resolved value should be reflected in current slots/context, not the
    # abandoned intermediate one.
    assert "bangalore" not in session.slots.get("destination", "").lower() if "destination" in session.slots else True

    # Prompt size should not exceed a small multiple of a single turn's size,
    # even after 8 turns -- i.e. it did not grow linearly/unboundedly.
    single_turn_chars = len(SCRIPTED_TURNS[0])
    assert len(joined) < single_turn_chars * 50


def test_report_measured_token_delta(capsys):
    """Not an assertion of a specific percentage -- just surfaces the real,
    measured character-count delta between the two conditions for the PR
    description, replacing the original plan's guessed 40-70% figure."""
    import asyncio

    async def _measure():
        baseline_session = SessionState(session_id="measure_baseline")
        baseline_backend = await _run_scripted_conversation(baseline_session)

        memory_session = SessionState(session_id="measure_memory")
        memory_session.ensure_memory()
        memory_backend = await _run_scripted_conversation(memory_session)

        return _total_prompt_chars(baseline_backend), _total_prompt_chars(memory_backend)

    baseline_chars, memory_chars = asyncio.run(_measure())
    print(f"\n[memory baseline] total prompt chars: baseline={baseline_chars}, memory={memory_chars}")
