"""Tests for LLM client, Circuit Breaker, and Timeout degradation (§7.1)."""

import asyncio
import pytest
from agent.llm_client import (
    LLMBackend,
    LLMResponse,
    LLMConfig,
    CircuitBreakerLLMClient,
    MockLLMBackend,
)


class SlowMockBackend(LLMBackend):
    async def generate(self, messages, tools=None):
        await asyncio.sleep(0.5)
        return LLMResponse(response_type="spoken_response", content="Finally finished")


@pytest.mark.asyncio
async def test_circuit_breaker_timeout_fallback():
    # Set hard deadline to 0.1s while backend takes 0.5s
    config = LLMConfig(timeout_s=0.1, max_consecutive_timeouts=2)
    client = CircuitBreakerLLMClient(SlowMockBackend(), config)

    # First attempt: should timeout gracefully without throwing uncaught exception
    resp1 = await client.generate([{"role": "user", "content": "hi"}])
    assert resp1.response_type == "clarification"
    assert "one second" in resp1.content.lower()
    assert client.consecutive_timeouts == 1
    assert client.is_circuit_open is False

    # Second attempt: trips consecutive timeouts -> opens circuit breaker
    resp2 = await client.generate([{"role": "user", "content": "hi again"}])
    assert client.consecutive_timeouts == 2
    assert client.is_circuit_open is True

    # Third attempt: circuit is open, returns immediate fallback without awaiting
    resp3 = await client.generate([{"role": "user", "content": "hi third time"}])
    assert resp3.response_type == "clarification"


class FastMockBackend(LLMBackend):
    async def generate(self, messages, tools=None):
        return LLMResponse(response_type="spoken_response", content="Backup answered")


@pytest.mark.asyncio
async def test_circuit_breaker_routes_to_fallback_backend_once_open():
    """§7.1: after N consecutive primary failures, requests go to the fallback backend instead
    of a static clarification message, and the response is tagged via_fallback so the caller
    can add a spoken notice."""
    config = LLMConfig(timeout_s=0.1, max_consecutive_timeouts=1, circuit_cooldown_s=999)
    client = CircuitBreakerLLMClient(SlowMockBackend(), config, fallback_backend=FastMockBackend())

    # First call times out and immediately opens the circuit (max_consecutive_timeouts=1).
    resp1 = await client.generate([{"role": "user", "content": "hi"}])
    assert client.is_circuit_open is True
    # Fallback available -> routed there in the same call rather than a canned message.
    assert resp1.response_type == "spoken_response"
    assert resp1.content == "Backup answered"
    assert resp1.via_fallback is True
    assert client.fallback_calls == 1

    # Cooldown is long, so subsequent calls keep using the fallback (no half-open probe yet).
    resp2 = await client.generate([{"role": "user", "content": "again"}])
    assert resp2.via_fallback is True
    assert client.fallback_calls == 2


@pytest.mark.asyncio
async def test_circuit_breaker_fallback_failure_still_returns_a_response():
    """If the fallback backend also fails, the caller still gets a usable LLMResponse, not an
    uncaught exception."""
    class BrokenBackend(LLMBackend):
        async def generate(self, messages, tools=None):
            raise RuntimeError("backup is down too")

    config = LLMConfig(timeout_s=0.1, max_consecutive_timeouts=1, circuit_cooldown_s=999)
    client = CircuitBreakerLLMClient(SlowMockBackend(), config, fallback_backend=BrokenBackend())

    resp = await client.generate([{"role": "user", "content": "hi"}])
    assert resp.response_type == "clarification"
    assert resp.via_fallback is True


@pytest.mark.live
@pytest.mark.asyncio
async def test_openrouter_live_tool_calling():
    import os
    if not os.getenv("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not configured in environment")

    from agent.llm_client import OpenRouterBackend

    config = LLMConfig()
    backend = OpenRouterBackend(config)
    client = CircuitBreakerLLMClient(backend, config)

    resp = await client.generate(
        messages=[
            {"role": "system", "content": "You are a test assistant. If the user asks for flights, call search_flights."},
            {"role": "user", "content": "Find me flights from Delhi to Mumbai"},
        ],
        tools=[{
            "type": "function",
            "function": {
                "name": "search_flights",
                "description": "Search for flights",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "origin": {"type": "string"},
                        "destination": {"type": "string"},
                    },
                    "required": ["origin", "destination"],
                },
            },
        }],
    )

    assert resp.response_type == "tool_call"
    assert resp.tool_name == "search_flights"
    assert "Delhi" in resp.arguments.get("origin", "")
    assert "Mumbai" in resp.arguments.get("destination", "")


def test_backend_selection_prefers_groq_over_openrouter(monkeypatch):
    from agent.llm_client import get_backend, GroqBackend, OpenRouterBackend, LocalQwenBackend

    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("LLM_MODEL_NAME", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    config = LLMConfig()
    assert config.backend_type == "groq"
    assert config.base_url == "https://api.groq.com/openai/v1"
    assert config.model_name == "openai/gpt-oss-120b"
    assert isinstance(get_backend(config), GroqBackend)


def test_backend_selection_falls_back_to_openrouter_without_groq(monkeypatch):
    from agent.llm_client import get_backend, OpenRouterBackend

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    config = LLMConfig()
    assert config.backend_type == "openrouter"
    assert isinstance(get_backend(config), OpenRouterBackend)


@pytest.mark.live
@pytest.mark.asyncio
async def test_groq_live_tool_calling():
    import os
    if not os.getenv("GROQ_API_KEY"):
        pytest.skip("GROQ_API_KEY not configured in environment")

    from agent.llm_client import GroqBackend

    config = LLMConfig()
    backend = GroqBackend(config)
    client = CircuitBreakerLLMClient(backend, config)

    resp = await client.generate(
        messages=[
            {"role": "system", "content": "You are a test assistant. If the user asks for flights, call search_flights."},
            {"role": "user", "content": "Find me flights from Delhi to Mumbai"},
        ],
        tools=[{
            "type": "function",
            "function": {
                "name": "search_flights",
                "description": "Search for flights",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "origin": {"type": "string"},
                        "destination": {"type": "string"},
                    },
                    "required": ["origin", "destination"],
                },
            },
        }],
    )

    assert resp.response_type == "tool_call"
    assert resp.tool_name == "search_flights"
    assert "Delhi" in resp.arguments.get("origin", "")
    assert "Mumbai" in resp.arguments.get("destination", "")
