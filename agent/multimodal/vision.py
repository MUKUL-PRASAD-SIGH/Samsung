"""Vision grounding: answer a question about an image (a camera or screen frame).

The planner LLM is text-only, so when a turn refers to something visible ("the city on this poster") it
calls the read-only `analyze_frame` tool, which runs a vision-language model on the session's latest frame.
Vision is invoked ONLY on demand -- never per frame, never on text-only turns (spec §3: "invoke only when
the slow path needs frame grounding").

Backends mirror `LLMBackend`: a deterministic `MockVisionBackend` for tests/eval, and an
`OpenRouterVisionBackend` that walks an ordered list of FREE vision models with failover. Free models are
individually rate-limited upstream, occasionally restricted, and sometimes return malformed replies, so a
single hardcoded model is fragile; probing showed exactly that (429s, a 403 "agentic harnesses only", a reply
without `choices`) and that OpenRouter's `openrouter/free` auto-router can route a vision question to a
content-safety classifier, so it is deliberately NOT used.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger("agent.vision")

ALLOWED_MIME = ("image/jpeg", "image/png", "image/webp")
MAX_FRAME_BYTES = 2_000_000

# Ordered by observed reliability (verify against https://openrouter.ai/models?max_price=0 -- free
# availability changes). Failover walks this list; entries can be overridden with VISION_MODELS.
DEFAULT_FREE_VISION_MODELS = [
    "dots-studio/dots-3-note-preview:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "qwen/qwen3.8-27b:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
]

# Instructions go in the user turn: some free hosts (Gemma via AI Studio) reject a system role.
_INSTRUCTIONS = (
    "You are the vision component of a voice assistant. Answer the question about the image in one or two short "
    "sentences. If asked to read text, quote it exactly. If the image does not contain the answer, say so plainly "
    "instead of guessing."
)


class VisionError(RuntimeError):
    """The image could not be analyzed (all models failed, bad input, ...)."""


class _ModelUnavailable(Exception):
    def __init__(self, reason: str, cooldown_s: float):
        super().__init__(reason)
        self.cooldown_s = cooldown_s


@dataclass
class VisionResult:
    answer: str
    model: str
    latency_s: float


class VisionBackend(ABC):
    @abstractmethod
    async def analyze(self, image: bytes, mime: str, question: str) -> VisionResult:
        """Answer `question` about `image`."""


class MockVisionBackend(VisionBackend):
    """Deterministic backend for tests and the eval harness (scripted answers, optional latency)."""

    def __init__(self, answers: Optional[List[str]] = None, latency_s: float = 0.0):
        self.answers = list(answers or [])
        self.latency_s = latency_s
        self.calls: List[Dict[str, Any]] = []

    async def analyze(self, image: bytes, mime: str, question: str) -> VisionResult:
        self.calls.append({"question": question, "bytes": len(image), "mime": mime})
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        answer = self.answers.pop(0) if self.answers else "Mock vision: I can see an image."
        return VisionResult(answer=answer, model="mock", latency_s=self.latency_s)


class OpenRouterVisionBackend(VisionBackend):
    def __init__(
        self,
        api_key: Optional[str] = None,
        models: Optional[List[str]] = None,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_s: float = 30.0,
        # Some free vision models (e.g. dots-3-note-preview) REASON before answering; the reasoning counts
        # against max_tokens, so a small cap yields an empty answer (observed at 300). Answers stay short
        # because the prompt asks for one or two sentences; this is only headroom for the thinking.
        max_tokens: int = 1500,
        clock=time.monotonic,
    ):
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        self.models = list(models or DEFAULT_FREE_VISION_MODELS)
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self._clock = clock
        self._cooldown_until: Dict[str, float] = {}
        self.last_model: Optional[str] = None

    def _available_models(self) -> List[str]:
        now = self._clock()
        ready = [m for m in self.models if self._cooldown_until.get(m, 0.0) <= now]
        if ready:
            return ready
        # Everything is cooling down: try the one that recovers soonest rather than failing outright.
        return sorted(self.models, key=lambda m: self._cooldown_until.get(m, 0.0))[:1]

    def _request(self, model: str, data_url: str, question: str) -> str:
        """Blocking call (runs in a worker thread). Returns the answer text or raises _ModelUnavailable."""
        body = json.dumps({
            "model": model,
            "max_tokens": self.max_tokens,
            "temperature": 0.1,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": f"{_INSTRUCTIONS}\n\nQuestion: {question}"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body, method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://samsung.hackathon",
                "X-Title": "Interruptible-Agent",
                "User-Agent": "interruptible-agent/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:160]
            # 429: shared free capacity is busy (short cooldown). 402/403/404: structural, so back off longer.
            cooldown = 60.0 if e.code == 429 else 600.0 if e.code in (402, 403, 404) else 15.0
            raise _ModelUnavailable(f"HTTP {e.code}: {detail}", cooldown)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise _ModelUnavailable(f"{type(e).__name__}: {e}", 15.0)
        except json.JSONDecodeError as e:
            raise _ModelUnavailable(f"non-JSON reply: {e}", 15.0)

        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not choices:
            # OpenRouter sometimes returns HTTP 200 carrying an {"error": ...} body.
            raise _ModelUnavailable(f"reply without choices: {str(payload)[:120]}", 15.0)
        content = (choices[0].get("message") or {}).get("content")
        if isinstance(content, list):  # multimodal-style content parts
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        answer = (content or "").strip()
        if not answer:
            raise _ModelUnavailable("empty answer (reasoning-only reply)", 15.0)
        return answer

    async def analyze(self, image: bytes, mime: str, question: str) -> VisionResult:
        if not self.api_key:
            raise VisionError("OPENROUTER_API_KEY is not configured")
        if mime not in ALLOWED_MIME:
            raise VisionError(f"unsupported image type {mime!r}")
        if not image or len(image) > MAX_FRAME_BYTES:
            raise VisionError(f"image size {len(image)} bytes outside 1..{MAX_FRAME_BYTES}")

        data_url = f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"
        loop = asyncio.get_running_loop()
        failures: List[str] = []
        started = time.time()
        for model in self._available_models():
            try:
                answer = await loop.run_in_executor(None, self._request, model, data_url, question)
            except _ModelUnavailable as e:
                self._cooldown_until[model] = self._clock() + e.cooldown_s
                failures.append(f"{model}: {e}")
                logger.warning("Vision model %s unavailable (%s); trying the next one", model, e)
                continue
            self.last_model = model
            return VisionResult(answer=answer, model=model, latency_s=time.time() - started)
        raise VisionError("all vision models failed: " + " | ".join(failures))


def get_vision_backend() -> VisionBackend:
    """VISION_BACKEND=mock|openrouter (default: openrouter when a key exists, else mock);
    VISION_MODELS=comma,separated,ids ; VISION_TIMEOUT_S (default 30) ; VISION_MAX_TOKENS (default 1500)."""
    kind = os.getenv("VISION_BACKEND") or ("openrouter" if os.getenv("OPENROUTER_API_KEY") else "mock")
    if kind == "openrouter":
        models = [m.strip() for m in os.getenv("VISION_MODELS", "").split(",") if m.strip()] or None
        return OpenRouterVisionBackend(
            models=models,
            timeout_s=float(os.getenv("VISION_TIMEOUT_S", "30")),
            max_tokens=int(os.getenv("VISION_MAX_TOKENS", "1500")),
        )
    return MockVisionBackend()
