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
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from agent.slow_path.validator import validate_and_repair

# Load environment variables from .env file if present
load_dotenv()

logger = logging.getLogger("agent.llm_client")


class LLMConfig(BaseModel):
    backend_type: str = Field(
        default_factory=lambda: "openrouter" if os.getenv("OPENROUTER_API_KEY") else ("local" if os.getenv("USE_LOCAL_LLM") else "mock")
    )
    model_name: str = Field(
        default_factory=lambda: os.getenv("LLM_MODEL_NAME", "qwen/qwen-2.5-7b-instruct")
    )
    api_key: Optional[str] = Field(
        default_factory=lambda: os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
    )
    base_url: str = Field(
        default_factory=lambda: os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1" if os.getenv("OPENROUTER_API_KEY") else "http://localhost:8000/v1")
    )
    timeout_s: float = Field(
        default_factory=lambda: float(os.getenv("LLM_TIMEOUT_S", "5.0" if os.getenv("OPENROUTER_API_KEY") else "2.0"))
    )
    max_consecutive_timeouts: int = 3
    temperature: float = 0.1


class LLMResponse(BaseModel):
    response_type: str  # "tool_call", "spoken_response", "clarification"
    content: Optional[str] = None
    tool_name: Optional[str] = None
    arguments: Dict[str, Any] = Field(default_factory=dict)
    raw_text: Optional[str] = None
    latency_s: float = 0.0


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
        return LLMResponse(
            response_type="spoken_response",
            content=choice.get("content", ""),
            raw_text=choice.get("content", ""),
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
        return LLMResponse(
            response_type="spoken_response",
            content=choice.get("content", ""),
            raw_text=choice.get("content", ""),
        )


class CircuitBreakerLLMClient:
    """Wraps an LLMBackend with hard deadlines, timeout tracking, and graceful fallback (§7.1)."""

    def __init__(self, backend: LLMBackend, config: LLMConfig):
        self.backend = backend
        self.config = config
        self.consecutive_timeouts = 0
        self.is_circuit_open = False

    async def generate(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        # If circuit is open, return immediate fallback clarification
        if self.is_circuit_open:
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
            self.consecutive_timeouts = 0
            return response

        except (asyncio.TimeoutError, TimeoutError):
            self.consecutive_timeouts += 1
            logger.warning(
                "LLM call exceeded timeout deadline (%.2fs). Consecutive timeouts: %d",
                self.config.timeout_s,
                self.consecutive_timeouts,
            )
            if self.consecutive_timeouts >= self.config.max_consecutive_timeouts:
                self.is_circuit_open = True

            # Graceful fallback: clarification instead of blowing latency score (§7.1)
            return LLMResponse(
                response_type="clarification",
                content="Still pulling that data together, one second...",
            )


def get_backend(config: LLMConfig) -> LLMBackend:
    """Factory creating LLM backend according to configuration."""
    if config.backend_type == "openrouter":
        return OpenRouterBackend(config)
    elif config.backend_type == "local":
        return LocalQwenBackend(config)
    return MockLLMBackend()
