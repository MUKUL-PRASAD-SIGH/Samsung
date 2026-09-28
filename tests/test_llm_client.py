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
