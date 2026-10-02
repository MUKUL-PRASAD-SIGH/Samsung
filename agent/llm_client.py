"""Pluggable LLM Backend interface (§3.1) and Circuit Breaker (§7.1).

Supports:
- Local quantized model (e.g. Qwen3-4B / Qwen2.5-3B via vLLM or local endpoint)
- OpenRouter API backend
- Mock backend for deterministic test scenarios
- Circuit breaker with hard timeouts and fallback degradation (§7.1)
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
import json
import os
import logging
import re
import time
from agent import clock
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from agent.slow_path.validator import validate_and_repair

# Load environment variables from .env file if present
load_dotenv()

logger = logging.getLogger("agent.llm_client")


def _default_backend_type() -> str:
    if os.getenv("GROQ_API_KEY"):
        return "groq"
    if os.getenv("OPENROUTER_API_KEY"):
        return "openrouter"
    if os.getenv("USE_LOCAL_LLM"):
        return "local"
    return "mock"


class LLMConfig(BaseModel):
    backend_type: str = Field(default_factory=_default_backend_type)
    model_name: str = Field(
        default_factory=lambda: os.getenv(
            "LLM_MODEL_NAME",
            # Note: Groq's available model set is org/account-specific and changes
            # over time (verify with GET /openai/v1/models against your own key --
            # e.g. llama-3.3-70b-versatile and qwen/qwen3.8-27b were unavailable
            # or org-blocked on the account this was built against).
            "openai/gpt-oss-120b" if os.getenv("GROQ_API_KEY") else "qwen/qwen-2.5-7b-instruct",
        )
    )
    api_key: Optional[str] = Field(
        default_factory=lambda: os.getenv("GROQ_API_KEY") or os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
    )
    base_url: str = Field(
        default_factory=lambda: os.getenv(
            "LLM_BASE_URL",
            "https://api.groq.com/openai/v1"
            if os.getenv("GROQ_API_KEY")
            else ("https://openrouter.ai/api/v1" if os.getenv("OPENROUTER_API_KEY") else "http://localhost:8000/v1"),
        )
    )
    timeout_s: float = Field(
        default_factory=lambda: float(
            os.getenv(
                "LLM_TIMEOUT_S",
                "8.0" if os.getenv("GROQ_API_KEY") else ("5.0" if os.getenv("OPENROUTER_API_KEY") else "2.0"),
            )
        )
    )
    max_consecutive_timeouts: int = 3
    # On HTTP 429, wait and retry once if the provider says to retry within this many seconds.
    rate_limit_max_wait_s: float = Field(
        default_factory=lambda: float(os.getenv("LLM_RATE_LIMIT_MAX_WAIT_S", "3.0"))
    )
    # Soft deadline: if the model hasn't answered by now, say so (progress line) but KEEP WAITING until
    # timeout_s (the hard deadline). Preserves latency perception without abandoning slow-but-good answers.
    # 0 disables. Only meaningful below timeout_s.
    soft_deadline_s: float = Field(
        default_factory=lambda: float(os.getenv("LLM_SOFT_DEADLINE_S", "2.0"))
    )
    circuit_cooldown_s: float = Field(
        default_factory=lambda: float(os.getenv("LLM_CIRCUIT_COOLDOWN_S", "15.0"))
    )
    temperature: float = 0.1


class LLMResponse(BaseModel):
    response_type: str  # "tool_call", "spoken_response", "clarification"
    content: Optional[str] = None
    tool_name: Optional[str] = None
    arguments: Dict[str, Any] = Field(default_factory=dict)
    raw_text: Optional[str] = None
    latency_s: float = 0.0
    memory_update: Optional[Dict[str, Any]] = None
    # Set when this response came from the fallback backend (circuit open on the primary), §7.1.
    via_fallback: bool = False
    # Provider-reported token usage ({"prompt_tokens", "completion_tokens", "total_tokens"}) when available.
    usage: Optional[Dict[str, int]] = None


_MEMORY_MARKER = re.compile(r"[*_`]*MEMORY_UPDATE[*_`]*\s*:?[*_`]*\s*")
_TRAILING_NOISE = re.compile(r"(?:\s*(?:-{3,}|\*{3,}|_{3,}|```[a-zA-Z]*))+\s*$")


def _extract_memory_update(content: Optional[str]) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Strip a 'MEMORY_UPDATE: {...}' block out of LLM content and parse it.

    This is the one place slot/entity extraction from the LLM's own text happens. Tolerates what
    the live model actually does: a preceding '---' rule, a code fence, bold markers, and text
    after the JSON. The marker is ALWAYS removed from the visible reply -- even when the JSON is
    malformed -- so the raw line never leaks into chat or speech; unparseable blocks yield None.
    Returns (cleaned_content, memory_update_dict_or_None).
    """
    if not content:
        return content, None
    match = _MEMORY_MARKER.search(content)
    if not match:
        return content, None

    before = content[: match.start()]
    rest = content[match.end():]
    memory_update: Optional[Dict[str, Any]] = None
    after = ""

    brace = rest.find("{")
    if brace != -1:
        try:
            parsed, end_idx = json.JSONDecoder().raw_decode(rest[brace:])
            if isinstance(parsed, dict):
                memory_update = parsed
            after = rest[brace + end_idx:]
        except (json.JSONDecodeError, ValueError):
            after = ""  # malformed: drop the rest of the block rather than show it
    else:
        after = ""

    after = re.sub(r"^\s*```\s*", "", after)  # closing fence of a fenced block
    cleaned = _TRAILING_NOISE.sub("", before).rstrip()
    if after.strip():
        cleaned = f"{cleaned}\n\n{after.strip()}".strip()
    return cleaned, memory_update


class RateLimitError(RuntimeError):
    """HTTP 429 from the provider, carrying the server's suggested wait (seconds) when given."""

    def __init__(self, message: str, retry_after_s: Optional[float] = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


class ToolCallRejectedError(RuntimeError):
    """HTTP 400 because the MODEL produced a tool call the provider's validator rejected (Groq:
    "Tool call validation failed" / code tool_use_failed). The provider is healthy; the model's output was
    bad, and sampling again usually fixes it -- so this must not count against the circuit breaker."""


def _is_tool_rejection(err_text: str) -> bool:
    return "tool_use_failed" in err_text or "Tool call validation failed" in err_text


_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def _parse_retry_after(header_value: Optional[str], body_text: str) -> Optional[float]:
    """Seconds to wait before retrying, from a Retry-After header or the provider's message
    (Groq: "Please try again in 1.905s" / "in 250ms" / "in 1m5.2s")."""
    if header_value:
        try:
            return max(0.0, float(header_value))
        except ValueError:
            pass
    match = re.search(r"try again in\s+((?:\d+(?:\.\d+)?(?:ms|s|m|h))+)", body_text)
    if match:
        return sum(float(n) * _UNIT_SECONDS[u] for n, u in _DURATION_TOKEN.findall(match.group(1)))
    return None


async def _request_with_rate_limit_retry(config: "LLMConfig", do_request, provider: str) -> Dict[str, Any]:
    """Run the blocking request in a thread; on a short-hint 429, wait and retry exactly once.

    Free-tier token-per-minute limits produce frequent 429s that clear within ~2s, so one retry
    turns most of them into a slightly slower success instead of a failed turn. Long or unknown
    waits are re-raised so the circuit breaker can degrade gracefully instead of stalling the user.
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, do_request)
    except RateLimitError as e:
        wait = e.retry_after_s
        if wait is None or wait > config.rate_limit_max_wait_s:
            raise
        logger.warning("%s rate limited; retrying once in %.2fs", provider, wait)
        await asyncio.sleep(wait + 0.1)
        return await loop.run_in_executor(None, do_request)


def _usage(res_json: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """Provider-reported token counts, if present and well-formed."""
    u = res_json.get("usage") if isinstance(res_json, dict) else None
    if not isinstance(u, dict):
        return None
    out = {k: int(u[k]) for k in ("prompt_tokens", "completion_tokens", "total_tokens") if isinstance(u.get(k), (int, float))}
    return out or None


class LLMBackend(ABC):
    @abstractmethod
    async def generate(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        """Generate reasoning or tool calls given conversation messages and tool schemas."""
        pass


class MockLLMBackend(LLMBackend):
    """Deterministic mock backend for rapid iteration and offline test suites."""

    def __init__(self, canned_responses: Optional[List[LLMResponse]] = None):
        self.canned_responses = list(canned_responses or [])
        self.call_history: List[List[Dict[str, str]]] = []

    def queue_response(self, response: LLMResponse) -> None:
        self.canned_responses.append(response)

    async def generate(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        self.call_history.append(messages)
        if self.canned_responses:
            return self.canned_responses.pop(0)

        # Default heuristic mock response
        last_msg = messages[-1]["content"].lower() if messages else ""
        if any(k in last_msg for k in ("agent", "bob", "clock", "code", "build", "script", "typescript", "component", "scout")):
            name = "bob" if "scout" not in last_msg else "scout"
            role = "TypeScript Generator" if "scout" not in last_msg else "Market & Flight Scout"
            goal = "Build AnalogClock component in TypeScript" if "scout" not in last_msg else "Search and compare travel options"
            return LLMResponse(
                response_type="tool_call",
                tool_name="spawn_agent",
                arguments={
                    "name": name,
                    "role": role,
                    "goal": goal,
                    "component": "AnalogClock",
                    "language": "TypeScript",
                },
            )
        elif "flight" in last_msg:
            return LLMResponse(
                response_type="tool_call",
                tool_name="search_flights",
                arguments={"origin": "BLR", "destination": "DEL"},
            )
        elif "hotel" in last_msg:
            return LLMResponse(
                response_type="tool_call",
                tool_name="book_hotel",
                arguments={"city": "Paris", "nights": 2},
            )

        return LLMResponse(
            response_type="spoken_response",
            content="How can I assist you with your booking?",
        )


class OpenRouterBackend(LLMBackend):
    """OpenRouter API client for dev-time testing and cloud eval."""

    def __init__(self, config: LLMConfig):
        self.config = config

    async def generate(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        import urllib.request
        import urllib.error

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://samsung.hackathon",
            "X-Title": "Interruptible-Agent",
        }
        payload: Dict[str, Any] = {
            "model": self.config.model_name,
            "messages": messages,
            "temperature": self.config.temperature,
        }
        if tools:
            payload["tools"] = tools

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.config.base_url.rstrip('/')}/chat/completions",
            data=data,
            headers=headers,
            method="POST",
        )

        def _do_request():
            try:
                with urllib.request.urlopen(req, timeout=self.config.timeout_s) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                err_text = e.read().decode("utf-8", errors="replace")
                logger.error("OpenRouter HTTP %d error: %s", e.code, err_text)
                if e.code == 429:
                    raise RateLimitError(
                        f"OpenRouter HTTP 429: {err_text}",
                        _parse_retry_after(e.headers.get("Retry-After") if e.headers else None, err_text),
                    ) from e
                raise RuntimeError(f"OpenRouter HTTP {e.code}: {err_text}") from e
            except urllib.error.URLError as e:
                logger.error("OpenRouter network error: %s", e)
                raise

        res_json = await _request_with_rate_limit_retry(self.config, _do_request, "OpenRouter")
        
        choice = res_json["choices"][0]["message"]
        if "tool_calls" in choice and choice["tool_calls"]:
            tc = choice["tool_calls"][0]["function"]
            args = json.loads(tc["arguments"])
            return LLMResponse(
                response_type="tool_call",
                tool_name=tc["name"],
                arguments=args,
                raw_text=json.dumps(tc),
                usage=_usage(res_json),
            )
        cleaned_content, memory_update = _extract_memory_update(choice.get("content", ""))
        return LLMResponse(
            response_type="spoken_response",
            content=cleaned_content,
            raw_text=choice.get("content", ""),
            memory_update=memory_update,
            usage=_usage(res_json),
        )


class GroqBackend(LLMBackend):
    """Groq LPU-hosted inference client (OpenAI-compatible chat completions API).

    Chosen for this interruptible, full-duplex agent because Groq's inference
    latency (time-to-first-token, tokens/sec) is substantially lower than routing
    through OpenRouter, which matters directly for the epoch/barge-in model's
    responsiveness budget (see CircuitBreakerLLMClient's hard timeout deadline).
    """

    def __init__(self, config: LLMConfig):
        self.config = config

    async def generate(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        import urllib.request
        import urllib.error

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            # Groq's API sits behind Cloudflare, which blocks urllib's default
            # "Python-urllib/x.y" User-Agent as a bot signature (HTTP 403 / error
            # code 1010) even with a valid API key -- a normal-looking UA avoids it.
            "User-Agent": "interruptible-agent/1.0 (+https://github.com)",
        }
        payload: Dict[str, Any] = {
            "model": self.config.model_name,
            "messages": messages,
            "temperature": self.config.temperature,
        }
        if tools:
            payload["tools"] = tools

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.config.base_url.rstrip('/')}/chat/completions",
            data=data,
            headers=headers,
            method="POST",
        )

        def _do_request():
            try:
                with urllib.request.urlopen(req, timeout=self.config.timeout_s) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                err_text = e.read().decode("utf-8", errors="replace")
                logger.error("Groq HTTP %d error: %s", e.code, err_text)
                if e.code == 429:
                    raise RateLimitError(
                        f"Groq HTTP 429: {err_text}",
                        _parse_retry_after(e.headers.get("Retry-After") if e.headers else None, err_text),
                    ) from e
                if e.code == 400 and _is_tool_rejection(err_text):
                    raise ToolCallRejectedError(f"Groq HTTP 400: {err_text}") from e
                raise RuntimeError(f"Groq HTTP {e.code}: {err_text}") from e
            except urllib.error.URLError as e:
                logger.error("Groq network error: %s", e)
                raise

        try:
            try:
                res_json = await _request_with_rate_limit_retry(self.config, _do_request, "Groq")
            except ToolCallRejectedError as first:
                logger.warning("Groq rejected the model's tool call; sampling once more: %s", str(first)[:160])
                res_json = await _request_with_rate_limit_retry(self.config, _do_request, "Groq")
        except ToolCallRejectedError:
            # Twice in a row: ask the user instead of failing the turn or tripping the breaker.
            return LLMResponse(
                response_type="clarification",
                content="I couldn't work out that request cleanly -- could you rephrase it or give me the details again?",
            )

        choice = res_json["choices"][0]["message"]
        if "tool_calls" in choice and choice["tool_calls"]:
            tc = choice["tool_calls"][0]["function"]
            args = validate_and_repair(tc["arguments"])
            return LLMResponse(
                response_type="tool_call",
                tool_name=tc["name"],
                arguments=args,
                raw_text=json.dumps(tc),
                usage=_usage(res_json),
            )

        cleaned_content, memory_update = _extract_memory_update(choice.get("content", ""))
        return LLMResponse(
            response_type="spoken_response",
            content=cleaned_content,
            raw_text=choice.get("content", ""),
            memory_update=memory_update,
            usage=_usage(res_json),
        )


class LocalQwenBackend(LLMBackend):
    """Local OpenAI-compatible inference client (e.g. vLLM or local endpoint)."""

    def __init__(self, config: LLMConfig):
        self.config = config

    async def generate(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        import urllib.request
        import urllib.error

        headers = {"Content-Type": "application/json"}
        payload: Dict[str, Any] = {
            "model": self.config.model_name,
            "messages": messages,
            "temperature": self.config.temperature,
        }
        if tools:
            payload["tools"] = tools

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.config.base_url.rstrip('/')}/chat/completions",
            data=data,
            headers=headers,
            method="POST",
        )

        def _do_request():
            with urllib.request.urlopen(req, timeout=self.config.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))

        loop = asyncio.get_running_loop()
        res_json = await loop.run_in_executor(None, _do_request)

        choice = res_json["choices"][0]["message"]
        if "tool_calls" in choice and choice["tool_calls"]:
            tc = choice["tool_calls"][0]["function"]
            args = validate_and_repair(tc["arguments"])
            return LLMResponse(
                response_type="tool_call",
                tool_name=tc["name"],
                arguments=args,
                raw_text=json.dumps(tc),
            )
        cleaned_content, memory_update = _extract_memory_update(choice.get("content", ""))
        return LLMResponse(
            response_type="spoken_response",
            content=cleaned_content,
            raw_text=choice.get("content", ""),
            memory_update=memory_update,
        )


class CircuitBreakerLLMClient:
    """Wraps an LLMBackend with hard deadlines, failure tracking, and graceful fallback (§7.1).

    States: CLOSED (normal) -> OPEN after `max_consecutive_timeouts` consecutive failures
    (timeouts or backend errors like HTTP 429 / network errors) -> HALF-OPEN once
    `circuit_cooldown_s` has elapsed, letting a single probe request through. A successful
    probe closes the circuit; a failed one re-opens it for another cooldown. Without the
    half-open step, one bad minute at the provider would degrade every request until restart.
    """

    def __init__(self, backend: LLMBackend, config: LLMConfig, fallback_backend: Optional[LLMBackend] = None):
        self.backend = backend
        self.config = config
        self.fallback_backend = fallback_backend
        self.fallback_calls = 0
        self.consecutive_timeouts = 0
        self.is_circuit_open = False
        self._opened_at: Optional[float] = None
        self._probe_in_flight = False

    def _cooldown_elapsed(self) -> bool:
        return self._opened_at is not None and (clock.monotonic() - self._opened_at) >= self.config.circuit_cooldown_s

    def _record_failure(self) -> None:
        self.consecutive_timeouts += 1
        if self.is_circuit_open or self.consecutive_timeouts >= self.config.max_consecutive_timeouts:
            # (Re)open and restart the cooldown clock -- also covers a failed half-open probe.
            self.is_circuit_open = True
            self._opened_at = clock.monotonic()

    def _record_success(self) -> None:
        if self.is_circuit_open:
            logger.info("Circuit breaker probe succeeded. Closing circuit.")
        self.consecutive_timeouts = 0
        self.is_circuit_open = False
        self._opened_at = None

    async def generate(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
        on_slow=None,
    ) -> LLMResponse:
        """`on_slow` (async, no args) is awaited once if the soft deadline passes before the model answers."""
        is_probe = False
        if self.is_circuit_open:
            if self._cooldown_elapsed() and not self._probe_in_flight:
                # Half-open: exactly one caller probes the primary; everyone else keeps using
                # the fallback (or the static message, if no fallback is configured).
                is_probe = True
                self._probe_in_flight = True
                logger.info("Circuit breaker HALF-OPEN. Probing LLM backend.")
            elif self.fallback_backend is not None:
                return await self._call_fallback(messages, tools)
            else:
                logger.warning("Circuit breaker is OPEN. Returning fallback response.")
                return LLMResponse(
                    response_type="clarification",
                    content="I'm still processing your request, please give me a moment.",
                )

        try:
            response = await self._generate_with_deadlines(messages, tools, on_slow)
            self._record_success()
            return response

        except (asyncio.TimeoutError, TimeoutError):
            self._record_failure()
            logger.warning(
                "LLM call exceeded timeout deadline (%.2fs). Consecutive failures: %d",
                self.config.timeout_s,
                self.consecutive_timeouts,
            )
            if self.is_circuit_open and self.fallback_backend is not None:
                return await self._call_fallback(messages, tools)
            # Graceful fallback: clarification instead of blowing latency score (§7.1)
            return LLMResponse(
                response_type="clarification",
                content="Still pulling that data together, one second...",
            )

        except Exception as e:
            # Provider errors (HTTP 429/5xx, network drops, malformed JSON) used to escape as
            # raw exceptions and the user got no reply at all. Treat them as breaker failures.
            self._record_failure()
            logger.error("LLM backend error (%s: %s). Consecutive failures: %d", type(e).__name__, e, self.consecutive_timeouts)
            if self.is_circuit_open and self.fallback_backend is not None:
                return await self._call_fallback(messages, tools)
            return LLMResponse(
                response_type="clarification",
                content="I hit a snag reaching my reasoning engine — could you try that again?",
            )

        finally:
            if is_probe:
                self._probe_in_flight = False

    async def _generate_with_deadlines(self, messages, tools, on_slow) -> LLMResponse:
        """Hard deadline `timeout_s`; a soft deadline fires `on_slow` once and then keeps waiting."""
        soft, hard = self.config.soft_deadline_s, self.config.timeout_s
        if on_slow is None or not (0 < soft < hard):
            return await asyncio.wait_for(self.backend.generate(messages, tools), timeout=hard)
        task = asyncio.ensure_future(self.backend.generate(messages, tools))
        try:
            done, _ = await asyncio.wait({task}, timeout=soft)
            if not done:
                try:
                    await on_slow()
                except Exception:  # noqa: BLE001 - a failing progress line must never fail the request
                    logger.exception("on_slow callback failed")
                return await asyncio.wait_for(task, timeout=max(0.0, hard - soft))
            return task.result()
        finally:
            if not task.done():
                task.cancel()

    async def _call_fallback(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]],
    ) -> LLMResponse:
        """Route to the smaller/faster fallback backend while the primary's circuit is open (§7.1)."""
        self.fallback_calls += 1
        try:
            response = await asyncio.wait_for(
                self.fallback_backend.generate(messages, tools),
                timeout=self.config.timeout_s,
            )
            response.via_fallback = True
            return response
        except Exception as e:
            logger.error("Fallback LLM backend also failed (%s: %s).", type(e).__name__, e)
            return LLMResponse(
                response_type="clarification",
                content="I'm having trouble reaching my reasoning engine right now — please try again shortly.",
                via_fallback=True,
            )


def get_backend(config: LLMConfig) -> LLMBackend:
    """Factory creating LLM backend according to configuration."""
    if config.backend_type == "groq":
        return GroqBackend(config)
    elif config.backend_type == "openrouter":
        return OpenRouterBackend(config)
    elif config.backend_type == "local":
        return LocalQwenBackend(config)
    return MockLLMBackend()


def get_fallback_backend() -> Optional[LLMBackend]:
    """Build a smaller/faster fallback backend from LLM_FALLBACK_* env vars (§7.1).

    Disabled (returns None) unless LLM_FALLBACK_MODEL_NAME is set, since a fallback with no
    model configured would just be a second copy of the primary backend.
    """
    model_name = os.getenv("LLM_FALLBACK_MODEL_NAME")
    if not model_name:
        return None
    fallback_api_key = (
        os.getenv("LLM_FALLBACK_API_KEY")
        or os.getenv("GROQ_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    config = LLMConfig(
        backend_type=os.getenv("LLM_FALLBACK_BACKEND_TYPE", _default_backend_type()),
        model_name=model_name,
        api_key=fallback_api_key,
        base_url=os.getenv("LLM_FALLBACK_BASE_URL") or LLMConfig().base_url,
        timeout_s=float(os.getenv("LLM_FALLBACK_TIMEOUT_S", "4.0")),
    )
    return get_backend(config)
