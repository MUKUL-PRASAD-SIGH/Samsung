"""Idle session eviction (gap #8): sessions, their idempotency store, and memory graph are
in-memory only and previously never freed, so a long-running server accumulated one SessionState
per page load forever."""

import asyncio
import time

import pytest

from agent.coordination.tool_router import ToolRouter
from agent.coordinator import AgentCoordinator


def test_evict_idle_sessions_drops_only_sessions_past_the_ttl():
    coordinator = AgentCoordinator()
    idle = coordinator.get_or_create_session("idle_session")
    fresh = coordinator.get_or_create_session("fresh_session")

    idle.last_activity = time.time() - 10_000  # long past any reasonable TTL
    fresh.last_activity = time.time()

    evicted = coordinator.evict_idle_sessions(now=time.time())

    assert evicted == ["idle_session"]
    assert "idle_session" not in coordinator.sessions
    assert "fresh_session" in coordinator.sessions


def test_evict_idle_sessions_spares_a_session_with_in_flight_work():
    """A session shouldn't vanish out from under a still-running tool call just because no new
    user event has arrived recently -- a booking search can legitimately take a while."""
    coordinator = AgentCoordinator()
    session = coordinator.get_or_create_session("busy_session")
    session.last_activity = time.time() - 10_000
    session.register_tool_call(call_id="c1", tool_name="search_flights", arguments={})

    evicted = coordinator.evict_idle_sessions(now=time.time())

    assert evicted == []
    assert "busy_session" in coordinator.sessions


def test_evict_idle_sessions_disabled_when_ttl_not_positive(monkeypatch):
    import agent.coordinator as coordinator_module

    monkeypatch.setattr(coordinator_module, "SESSION_TTL_S", 0)
    coordinator = AgentCoordinator()
    session = coordinator.get_or_create_session("never_evicted")
    session.last_activity = time.time() - 10_000

    evicted = coordinator.evict_idle_sessions(now=time.time())

    assert evicted == []
    assert "never_evicted" in coordinator.sessions


@pytest.mark.asyncio
async def test_post_event_touches_the_session_activity_clock():
    from agent.schemas.events import UserTextEvent

    coordinator = AgentCoordinator()
    session = coordinator.get_or_create_session("touch_session")
    session.last_activity = 0.0

    await coordinator.post_event(UserTextEvent(session_id="touch_session", text="hi"))

    assert session.last_activity > 0.0


def test_evict_idle_sessions_spares_planning_and_voice_sessions_and_cleans_leftovers():
    coordinator = AgentCoordinator()
    for sid in ("planning", "voicing", "gone"):
        coordinator.get_or_create_session(sid).last_activity = time.time() - 10_000
    coordinator._active_plans["planning"] = 1
    coordinator._voice["voicing"] = object()
    coordinator.sessions["gone"].register_tool_call(call_id="c9", tool_name="search_flights", arguments={})
    coordinator.sessions["gone"].in_flight_calls["c9"].status = "cancelled"
    coordinator._call_origin["c9"] = "find flights"

    evicted = coordinator.evict_idle_sessions(now=time.time())

    assert evicted == ["gone"]
    assert "c9" not in coordinator._call_origin
