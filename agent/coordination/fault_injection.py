"""Fault Injection Test Double Harness (§7.3).

Mirrors the evaluation environment's deterministic latency and fault injection:
- Configurable artificial latency (fixed or range)
- Configurable error rate (injecting exceptions or malformed responses)
- Configurable timeout injection
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Callable, Coroutine, Dict, Optional


class FaultInjectionConfig:
    def __init__(
        self,
        delay_seconds: float = 0.0,
        failure_rate: float = 0.0,
        timeout_seconds: Optional[float] = None,
        malformed_response_rate: float = 0.0,
    ):
        self.delay_seconds = delay_seconds
        self.failure_rate = failure_rate
        self.timeout_seconds = timeout_seconds
        self.malformed_response_rate = malformed_response_rate


class FaultInjectedToolHandler:
    def __init__(
        self,
        real_handler: Callable[..., Coroutine[Any, Any, Any]],
        config: Optional[FaultInjectionConfig] = None,
    ):
        self.real_handler = real_handler
        self.config = config or FaultInjectionConfig()
        self.call_count = 0

    async def __call__(self, **kwargs) -> Any:
        self.call_count += 1

        # 1. Artificial delay
        if self.config.delay_seconds > 0:
            await asyncio.sleep(self.config.delay_seconds)

        # 2. Timeout simulation
        if self.config.timeout_seconds is not None:
            if self.config.timeout_seconds <= 0:
                raise asyncio.TimeoutError("Simulated tool timeout")
            await asyncio.sleep(self.config.timeout_seconds)

        # 3. Simulated failure
        if self.config.failure_rate > 0:
            if random.random() < self.config.failure_rate:
                raise RuntimeError("Simulated tool backend 500 error")

        # 4. Execute real handler
        result = await self.real_handler(**kwargs)

        # 5. Simulated malformed response
        if self.config.malformed_response_rate > 0:
            if random.random() < self.config.malformed_response_rate:
                return "{unparseable_malformed_json: missing_brace"

        return result
