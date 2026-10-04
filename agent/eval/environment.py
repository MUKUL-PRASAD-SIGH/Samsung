"""Deterministic, instrumented mock tool environment (mirrors the spec's mock env: fixed latency +
injectable faults). Every handler records what it REALLY did -- started, completed, cancelled, failed --
which is independent ground truth the trace alone can't provide (e.g. a "cancelled" booking that
nonetheless ran to completion)."""

from __future__ import annotations

import asyncio
from agent import clock
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.coordination.fault_injection import FaultInjectedToolHandler, FaultInjectionConfig
from agent.coordination.tool_router import ToolRouter

DEFAULT_LATENCY_S = 1.5


@dataclass
class ExecRecord:
    tool: str
    args: Dict[str, Any]
    state_modifying: bool
    t_start: float
    t_end: Optional[float] = None
    completed: bool = False
    cancelled: bool = False
    error: Optional[str] = None


def _results() -> Dict[str, Any]:
    """Result payloads: same shapes as the default tools so reply summaries behave identically."""

    def flights(origin, destination, **kw):
        return {"flights": [
            {"flight": "AI-102", "airline": "Air India", "origin": origin, "destination": destination, "price": "$180", "departure": "08:30 AM"},
            {"flight": "6E-455", "airline": "IndiGo", "origin": origin, "destination": destination, "price": "$145", "departure": "11:15 AM"},
        ]}

    return {
        "search_flights": flights,
        "book_flight": lambda origin, destination, flight="6E-455", **kw: {"booking_id": "FL-98214", "status": "confirmed", "origin": origin, "destination": destination, "flight": flight},
        "search_hotels": lambda city, nights=2, **kw: {"hotels": [{"name": f"The Grand {city}", "stars": 5, "price_per_night": "$140", "rating": 4.8}]},
        "book_hotel": lambda city, hotel_name="Downtown Suites", nights=2, **kw: {"reservation_id": "HT-44012", "status": "confirmed", "hotel": hotel_name, "city": city, "nights": nights},
        "check_weather": lambda city, **kw: {"city": city, "condition": "Sunny", "temp": "28°C", "humidity": "45%"},
        "set_timer": lambda seconds=10, label="timer", **kw: {"label": label, "seconds": seconds, "status": "finished"},
        "cancel_booking": lambda booking_id, **kw: {"status": "cancelled", "booking_id": booking_id, "refund": "processed"},
    }


class Environment:
    def __init__(self, latency_s: Optional[Dict[str, float]] = None, faults: Optional[Dict[str, FaultInjectionConfig]] = None):
        self.executions: List[ExecRecord] = []
        self.router = ToolRouter(register_defaults=True)  # keep the real manifests/schemas
        latency_s = latency_s or {}
        faults = faults or {}
        for name, produce in _results().items():
            definition = self.router._tools[name]
            definition.handler = self._instrument(
                name, produce, latency_s.get(name, DEFAULT_LATENCY_S), faults.get(name), definition.is_state_modifying
            )

    def _instrument(self, name, produce, latency, fault, state_modifying):
        async def real(**kwargs):
            await asyncio.sleep(latency)
            return produce(**kwargs)

        handler = FaultInjectedToolHandler(real, fault) if fault else real

        async def wrapped(**kwargs):
            record = ExecRecord(tool=name, args=dict(kwargs), state_modifying=state_modifying, t_start=clock.now())
            self.executions.append(record)
            try:
                result = await handler(**kwargs)
            except asyncio.CancelledError:
                record.cancelled, record.t_end = True, clock.now()
                raise
            except Exception as e:  # injected faults surface to the coordinator, which must report them
                record.error, record.t_end = f"{type(e).__name__}: {e}", clock.now()
                raise
            record.completed, record.t_end = True, clock.now()
            return result

        return wrapped


def instrument_vision(env: "Environment", coordinator) -> None:
    """Record analyze_frame executions. The coordinator (not a router handler) runs this tool, so wrap it
    directly; that also records the no-frame path where the vision backend is never called."""
    original = coordinator._run_vision

    async def recorded(session_id, arguments):
        record = ExecRecord(tool="analyze_frame", args=dict(arguments), state_modifying=False, t_start=clock.now())
        env.executions.append(record)
        try:
            result = await original(session_id, arguments)
        except asyncio.CancelledError:
            record.cancelled, record.t_end = True, clock.now()
            raise
        except Exception as e:
            record.error, record.t_end = f"{type(e).__name__}: {e}", clock.now()
            raise
        record.completed, record.t_end = True, clock.now()
        return result

    coordinator._run_vision = recorded


def instrument_export(env: "Environment", coordinator) -> None:
    """Record export_artifact executions (run by the coordinator, which owns the session's artifacts). Exporting writes a
    file, so it is state-changing: a duplicate would be a real duplicate side effect the safety score must catch."""
    original = coordinator._run_export

    async def recorded(session_id, arguments):
        record = ExecRecord(tool="export_artifact", args=dict(arguments), state_modifying=True, t_start=clock.now())
        env.executions.append(record)
        try:
            result = await original(session_id, arguments)
        except asyncio.CancelledError:
            record.cancelled, record.t_end = True, clock.now()
            raise
        except Exception as e:
            record.error, record.t_end = f"{type(e).__name__}: {e}", clock.now()
            raise
        record.completed, record.t_end = True, clock.now()
        return result

    coordinator._run_export = recorded
