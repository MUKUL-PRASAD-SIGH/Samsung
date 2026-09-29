"""HTTP 429 handling: retry once on a short provider hint, otherwise fail fast to the breaker."""

import io
import json
import urllib.error

import pytest

from agent.llm_client import (
    CircuitBreakerLLMClient,
    GroqBackend,
    LLMConfig,
    OpenRouterBackend,
    RateLimitError,
    _parse_retry_after,
)

MSGS = [{"role": "user", "content": "hi"}]
OK_BODY = json.dumps({"choices": [{"message": {"content": "hello there"}}]}).encode()


class _Resp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, message, headers=None):
    body = json.dumps({"error": {"message": message}}).encode()
    return urllib.error.HTTPError("http://x", code, "err", headers or {}, io.BytesIO(body))


def _install(monkeypatch, script):
    """script: list of 'ok' or an HTTPError to raise; records call count."""
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return _Resp(OK_BODY)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


def _cfg(**kw):
    return LLMConfig(backend_type="groq", api_key="k", timeout_s=5.0, rate_limit_max_wait_s=1.0, **kw)


@pytest.mark.parametrize("text,expected", [
    ("Please try again in 1.905s.", 1.905),
    ("try again in 250ms", 0.25),
    ("try again in 1m5.2s", 65.2),
    ("no hint here", None),
])
def test_parse_retry_after_from_message(text, expected):
    assert _parse_retry_after(None, text) == pytest.approx(expected) if expected is not None else _parse_retry_after(None, text) is None


def test_parse_retry_after_header_wins():
    assert _parse_retry_after("3", "try again in 9s") == 3.0


@pytest.mark.asyncio
async def test_short_429_is_retried_once_and_succeeds(monkeypatch):
    calls = _install(monkeypatch, [_http_error(429, "Rate limit. Please try again in 0.05s."), "ok"])
    resp = await GroqBackend(_cfg()).generate(MSGS)
    assert resp.content == "hello there" and calls["n"] == 2


@pytest.mark.asyncio
async def test_retry_after_header_is_honored(monkeypatch):
    calls = _install(monkeypatch, [_http_error(429, "slow down", {"Retry-After": "0.05"}), "ok"])
    resp = await OpenRouterBackend(_cfg()).generate(MSGS)
    assert resp.content == "hello there" and calls["n"] == 2


@pytest.mark.asyncio
async def test_long_wait_is_not_retried(monkeypatch):
    calls = _install(monkeypatch, [_http_error(429, "Please try again in 30s.")])
    with pytest.raises(RateLimitError) as exc:
        await GroqBackend(_cfg()).generate(MSGS)
    assert exc.value.retry_after_s == 30.0 and calls["n"] == 1


@pytest.mark.asyncio
async def test_429_without_hint_is_not_retried(monkeypatch):
    calls = _install(monkeypatch, [_http_error(429, "too many requests")])
    with pytest.raises(RateLimitError):
        await GroqBackend(_cfg()).generate(MSGS)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_only_one_retry(monkeypatch):
    calls = _install(monkeypatch, [_http_error(429, "try again in 0.05s"), _http_error(429, "try again in 0.05s"), "ok"])
    with pytest.raises(RateLimitError):
        await GroqBackend(_cfg()).generate(MSGS)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_non_429_errors_are_not_retried(monkeypatch):
    calls = _install(monkeypatch, [_http_error(500, "boom"), "ok"])
    with pytest.raises(RuntimeError) as exc:
        await GroqBackend(_cfg()).generate(MSGS)
    assert not isinstance(exc.value, RateLimitError) and calls["n"] == 1


@pytest.mark.asyncio
async def test_retried_429_is_invisible_to_the_circuit_breaker(monkeypatch):
    _install(monkeypatch, [_http_error(429, "try again in 0.05s"), "ok"])
    client = CircuitBreakerLLMClient(GroqBackend(_cfg()), _cfg())
    resp = await client.generate(MSGS)
    assert resp.content == "hello there"
    assert client.consecutive_timeouts == 0 and not client.is_circuit_open


@pytest.mark.asyncio
async def test_unrecoverable_429_degrades_gracefully_through_the_breaker(monkeypatch):
    _install(monkeypatch, [_http_error(429, "try again in 30s")])
    client = CircuitBreakerLLMClient(GroqBackend(_cfg()), _cfg())
    resp = await client.generate(MSGS)
    assert resp.response_type == "clarification" and client.consecutive_timeouts == 1


def test_groq_default_timeout_leaves_room_for_long_replies(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.delenv("LLM_TIMEOUT_S", raising=False)
    assert LLMConfig().timeout_s == 8.0
