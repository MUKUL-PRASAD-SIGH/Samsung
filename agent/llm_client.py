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
                "4.0" if os.getenv("GROQ_API_KEY") else ("5.0" if os.getenv("OPENROUTER_API_KEY") else "2.0"),
            )
        )
    )
    max_consecutive_timeouts: int = 3
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
                raise RuntimeError(f"OpenRouter HTTP {e.code}: {err_text}") from e
            except urllib.error.URLError as e:
                logger.error("OpenRouter network error: %s", e)
                raise

        loop = asyncio.get_running_loop()
        res_json = await loop.run_in_executor(None, _do_request)
        
        choice = res_json["choices"][0]["message"]
        if "tool_calls" in choice and choice["tool_calls"]:
            tc = choice["tool_calls"][0]["function"]
            args = json.loads(tc["arguments"])
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
                raise RuntimeError(f"Groq HTTP {e.code}: {err_text}") from e
            except urllib.error.URLError as e:
                logger.error("Groq network error: %s", e)
                raise

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

    def __init__(self, backend: LLMBackend, config: LLMConfig):
        self.backend = backend
        self.config = config
        self.consecutive_timeouts = 0
        self.is_circuit_open = False
        self._opened_at: Optional[float] = None
        self._probe_in_flight = False

    def _cooldown_elapsed(self) -> bool:
        return self._opened_at is not None and (time.monotonic() - self._opened_at) >= self.config.circuit_cooldown_s

    def _record_failure(self) -> None:
        self.consecutive_timeouts += 1
        if self.is_circuit_open or self.consecutive_timeouts >= self.config.max_consecutive_timeouts:
            # (Re)open and restart the cooldown clock -- also covers a failed half-open probe.
            self.is_circuit_open = True
            self._opened_at = time.monotonic()

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
    ) -> LLMResponse:
        is_probe = False
        if self.is_circuit_open:
            if self._cooldown_elapsed() and not self._probe_in_flight:
                # Half-open: exactly one caller probes the backend; everyone else keeps the fallback.
                is_probe = True
                self._probe_in_flight = True
                logger.info("Circuit breaker HALF-OPEN. Probing LLM backend.")
            else:
                logger.warning("Circuit breaker is OPEN. Returning fallback response.")
                return LLMResponse(
                    response_type="clarification",
                    content="I'm still processing your request, please give me a moment.",
                )

        try:
            # Enforce hard deadline
            response = await asyncio.wait_for(
                self.backend.generate(messages, tools),
                timeout=self.config.timeout_s,
            )
            self._record_success()
            return response

        except (asyncio.TimeoutError, TimeoutError):
            self._record_failure()
            logger.warning(
                "LLM call exceeded timeout deadline (%.2fs). Consecutive failures: %d",
                self.config.timeout_s,
                self.consecutive_timeouts,
            )
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
            return LLMResponse(
                response_type="clarification",
                content="I hit a snag reaching my reasoning engine — could you try that again?",
            )

        finally:
            if is_probe:
                self._probe_in_flight = False


def get_backend(config: LLMConfig) -> LLMBackend:
    """Factory creating LLM backend according to configuration."""
    if config.backend_type == "groq":
        return GroqBackend(config)
    elif config.backend_type == "openrouter":
        return OpenRouterBackend(config)
    elif config.backend_type == "local":
        return LocalQwenBackend(config)
    return MockLLMBackend()
