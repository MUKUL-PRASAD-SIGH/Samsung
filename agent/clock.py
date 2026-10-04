"""Injectable time source (spec: the evaluator may drive the agent with a virtual-clock harness).

Every timestamp and duration in the agent goes through `now()` / `monotonic()` instead of `time.time()`, and
every delay is an `asyncio.sleep`/`wait_for`. Under the real clock this is a no-op. Inside `run_virtual()` the
asyncio loop's own clock is virtual: whenever every task is idle the loop jumps straight to the next timer, so
seconds of simulated tool latency and debouncing run in milliseconds, while `now()` stays consistent with the
loop's sleeps (so latency measurements remain meaningful).

Limits: virtual time only makes sense for work that lives entirely on the event loop. Anything that blocks a
worker thread (real Whisper, real HTTP) would see time race ahead, so use it with the mock LLM/tools only.
"""

from __future__ import annotations

import asyncio
import selectors
import time
from typing import Awaitable, Optional, TypeVar

T = TypeVar("T")

_virtual_loop: Optional["VirtualTimeLoop"] = None


def now() -> float:
    """Wall-clock seconds (virtual seconds since the epoch-anchored start inside run_virtual)."""
    loop = _virtual_loop
    return loop.wall_start + loop.time() if loop is not None else time.time()


def monotonic() -> float:
    loop = _virtual_loop
    return loop.time() if loop is not None else time.monotonic()


class _JumpingSelector:
    """Wraps the real selector: never blocks; when nothing is ready, advances virtual time by the timeout."""

    def __init__(self, inner: selectors.BaseSelector, loop: "VirtualTimeLoop"):
        self._inner, self._loop = inner, loop

    def select(self, timeout=None):
        if timeout is None:  # nothing scheduled: only an external wakeup (thread callback) can help
            return self._inner.select(None)
        events = self._inner.select(0)
        if not events and timeout > 0:
            # Always make progress: a timeout below float resolution at the current time would otherwise leave
            # virtual time unchanged and spin forever.
            self._loop._vtime += max(timeout, 1e-6)
        return events

    def __getattr__(self, name):
        return getattr(self._inner, name)


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        super().__init__()
        self._vtime = 0.0
        self.wall_start = time.time()
        self._selector = _JumpingSelector(self._selector, self)

    def time(self) -> float:
        return self._vtime


def run_virtual(coro: Awaitable[T]) -> T:
    """Run `coro` to completion on a virtual-time event loop (see module docstring for limits)."""
    global _virtual_loop
    loop = VirtualTimeLoop()
    _virtual_loop = loop
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        _virtual_loop = None
        asyncio.set_event_loop(None)
        loop.close()
