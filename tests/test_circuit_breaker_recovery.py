"""Circuit breaker half-open recovery and backend-error handling."""

import asyncio

import pytest

from agent.llm_client import CircuitBreakerLLMClient, LLMBackend, LLMConfig, LLMResponse

MSGS = [{"role": "user", "content": "hi"}]


class ScriptedBackend(LLMBackend):
    """Each call pops the next behavior: 'ok', 'slow', or an Exception instance."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def generate(self, messages, tools=None):
        self.calls += 1
        step = self.script.pop(0) if self.script else "ok"
        if step == "slow":
            await asyncio.sleep(1.0)
        if isinstance(step, Exception):
            raise step
        return LLMResponse(response_type="spoken_response", content="real answer")


def _client(script, cooldown=0.15, max_failures=2):
    cfg = LLMConfig(timeout_s=0.05, max_consecutive_timeouts=max_failures, circuit_cooldown_s=cooldown)
    backend = ScriptedBackend(script)
    return CircuitBreakerLLMClient(backend, cfg), backend


@pytest.mark.asyncio
async def test_circuit_recovers_after_cooldown_when_probe_succeeds():
    client, backend = _client(["slow", "slow", "ok"])
    await client.generate(MSGS)
    await client.generate(MSGS)
    assert client.is_circuit_open

    # During the cooldown the backend is not touched at all.
    calls_before = backend.calls
    resp = await client.generate(MSGS)
    assert resp.response_type == "clarification" and backend.calls == calls_before

    await asyncio.sleep(0.2)
    resp = await client.generate(MSGS)  # half-open probe
    assert resp.content == "real answer"
    assert client.is_circuit_open is False and client.consecutive_timeouts == 0


@pytest.mark.asyncio
async def test_failed_probe_reopens_circuit_for_another_cooldown():
    client, backend = _client(["slow", "slow", "slow", "ok"])
    await client.generate(MSGS)
    await client.generate(MSGS)
    await asyncio.sleep(0.2)

    resp = await client.generate(MSGS)  # probe times out
    assert resp.response_type == "clarification"
    assert client.is_circuit_open

    calls = backend.calls
    await client.generate(MSGS)  # cooldown restarted -> no backend call
    assert backend.calls == calls


@pytest.mark.asyncio
async def test_only_one_concurrent_probe():
    client, backend = _client(["slow", "slow", "ok"], cooldown=0.1)
    await client.generate(MSGS)
    await client.generate(MSGS)
    await asyncio.sleep(0.15)

    calls = backend.calls
    results = await asyncio.gather(*[client.generate(MSGS) for _ in range(5)])
    assert backend.calls == calls + 1
    assert sum(r.content == "real answer" for r in results) == 1


@pytest.mark.asyncio
async def test_backend_errors_degrade_gracefully_and_trip_the_breaker():
    err = RuntimeError("Groq HTTP 429: rate limited")
    client, backend = _client([err, err])
    r1 = await client.generate(MSGS)  # used to raise straight through to the caller
    assert r1.response_type == "clarification" and "snag" in r1.content
    assert not client.is_circuit_open
    await client.generate(MSGS)
    assert client.is_circuit_open


@pytest.mark.asyncio
async def test_success_resets_failure_count():
    client, _ = _client(["slow", "ok", "slow"], max_failures=2)
    await client.generate(MSGS)
    await client.generate(MSGS)
    await client.generate(MSGS)
    assert not client.is_circuit_open  # failures were never consecutive
