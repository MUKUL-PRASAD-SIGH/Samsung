"""Every completed tool call must produce a visible reply (success, failure, or artifact)."""

import asyncio

import pytest

from agent.coordination.tool_router import ToolRouter
from agent.coordinator import AgentCoordinator
from agent.llm_client import MockLLMBackend
from agent.schemas.actions import ActionType, SpokenResponseAction
from agent.schemas.events import InterruptSignalEvent
from agent.tool_summaries import summarize_tool_error, summarize_tool_result


def test_flight_summary_mentions_cheapest():
    text = summarize_tool_result("search_flights", {"flights": [
        {"flight": "AI-102", "airline": "Air India", "origin": "DEL", "destination": "BOM", "price": "$180", "departure": "08:30 AM"},
        {"flight": "6E-455", "airline": "IndiGo", "origin": "DEL", "destination": "BOM", "price": "$145", "departure": "11:15 AM"},
    ]})
    assert "2 flights" in text and "DEL to BOM" in text
    assert "cheapest is IndiGo 6E-455 at $145" in text


def test_other_tool_summaries():
    assert "confirmed" in summarize_tool_result("book_flight", {"booking_id": "FL-1", "status": "confirmed", "origin": "DEL", "destination": "BOM", "flight": "6E-455"})
    assert "HT-9" in summarize_tool_result("book_hotel", {"reservation_id": "HT-9", "status": "confirmed", "hotel": "X", "city": "Goa", "nights": 2})
    assert "sunny in Goa" in summarize_tool_result("check_weather", {"city": "Goa", "condition": "Sunny", "temp": "28°C", "humidity": "45%"})
    assert "refund" in summarize_tool_result("cancel_booking", {"booking_id": "B1", "status": "cancelled", "refund": "processed"})
    assert "2 hotels" in summarize_tool_result("search_hotels", {"hotels": [{"name": "A"}, {"name": "B"}]})


def test_empty_and_malformed_results_fall_back_gracefully():
    assert "couldn't find any flights" in summarize_tool_result("search_flights", {"flights": []})
    assert summarize_tool_result("search_flights", {"flights": [{"weird": 1}]})  # no exception
    assert summarize_tool_result("mystery_tool", None) == "Done — mystery tool completed."


def test_error_summary_includes_detail():
    assert "search flights" in summarize_tool_error("search_flights", "timeout")
    assert "timeout" in summarize_tool_error("search_flights", "timeout")


async def _collect(coordinator, settle_s):
    await asyncio.sleep(settle_s)
    out = []
    while not coordinator.action_queue.empty():
        out.append(coordinator.action_queue.get_nowait())
    return out


def _fast_router(handler, name="check_weather"):
    router = ToolRouter(register_defaults=False)
    router.register_tool(
        name=name, description="t",
        parameters_schema={"type": "object", "properties": {"city": {"type": "string"}}},
        is_state_modifying=False, handler=handler,
    )
    return router


@pytest.mark.asyncio
async def test_plain_tool_call_gets_a_spoken_reply():
    async def weather(city: str, **kw):
        return {"city": city, "condition": "Sunny", "temp": "28°C", "humidity": "45%"}

    coordinator = AgentCoordinator(tool_router=_fast_router(weather), llm_backend=MockLLMBackend(), enable_debounce=False)
    await coordinator.start()
    try:
        await coordinator.dispatch_tool_call("s1", "check_weather", {"city": "Goa"}, "call_w1")
        actions = await _collect(coordinator, 0.3)
        replies = [a for a in actions if isinstance(a, SpokenResponseAction)]
        assert len(replies) == 1
        assert "Goa" in replies[0].text and "sunny" in replies[0].text.lower()
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_failed_tool_call_reports_the_failure():
    async def boom(city: str, **kw):
        raise RuntimeError("upstream down")

    coordinator = AgentCoordinator(tool_router=_fast_router(boom), llm_backend=MockLLMBackend(), enable_debounce=False)
    await coordinator.start()
    try:
        await coordinator.dispatch_tool_call("s2", "check_weather", {"city": "Goa"}, "call_w2")
        actions = await _collect(coordinator, 0.3)
        replies = [a for a in actions if isinstance(a, SpokenResponseAction)]
        assert len(replies) == 1
        assert "couldn't complete" in replies[0].text and "upstream down" in replies[0].text
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_cancelled_tool_call_stays_silent():
    """A call interrupted by a newer epoch must NOT produce a stale reply."""
    async def slow(city: str, **kw):
        await asyncio.sleep(0.5)
        return {"city": city, "condition": "Sunny", "temp": "1", "humidity": "1"}

    coordinator = AgentCoordinator(tool_router=_fast_router(slow), llm_backend=MockLLMBackend(), enable_debounce=False)
    await coordinator.start()
    try:
        await coordinator.dispatch_tool_call("s3", "check_weather", {"city": "Goa"}, "call_w3")
        await asyncio.sleep(0.05)
        await coordinator.post_event(InterruptSignalEvent(session_id="s3", reason="user_barge_in"))
        actions = await _collect(coordinator, 0.8)
        assert ActionType.TOOL_CANCEL in [a.action_type for a in actions]
        assert not [a for a in actions if isinstance(a, SpokenResponseAction)]
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_reply_is_recorded_in_conversation_graph():
    async def weather(city: str, **kw):
        return {"city": city, "condition": "Sunny", "temp": "28°C", "humidity": "45%"}

    coordinator = AgentCoordinator(tool_router=_fast_router(weather), llm_backend=MockLLMBackend(), enable_debounce=False)
    await coordinator.start()
    try:
        await coordinator.dispatch_tool_call("s4", "check_weather", {"city": "Goa"}, "call_w4")
        await _collect(coordinator, 0.3)
        thread = coordinator.sessions["s4"].graph_memory.get_active_thread()
        assert thread and "Goa" in thread[-1].agent_response
    finally:
        await coordinator.stop()
