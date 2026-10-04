"""Minimal Prometheus-format metrics (Phase G), with no extra dependency.

What an operator needs to see for THIS system: how fast the agent reacts (first acknowledgement, interrupt -> cancel),
how the LLM provider is behaving (latency, outcomes, 429s, circuit breaker state, fallback use), and what is being
turned away (rate limits, bad origins, auth failures). Rendered at GET /metrics in the text exposition format.
"""

from __future__ import annotations

import threading
from typing import Dict, Iterable, List, Sequence, Tuple

LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.15, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)


def _labels(names: Sequence[str], values: Sequence[str]) -> str:
    if not names:
        return ""
    return "{" + ",".join(f'{n}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"' for n, v in zip(names, values)) + "}"


class _Metric:
    kind = ""

    def __init__(self, name: str, help_: str, label_names: Sequence[str] = ()):
        self.name, self.help, self.label_names = name, help_, tuple(label_names)
        self._lock = threading.Lock()

    def _key(self, labels: Dict[str, str]) -> Tuple[str, ...]:
        return tuple(str(labels.get(n, "")) for n in self.label_names)

    def header(self) -> List[str]:
        return [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]


class Counter(_Metric):
    kind = "counter"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._values: Dict[Tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        with self._lock:
            key = self._key(labels)
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        return self._values.get(self._key(labels), 0.0)

    def render(self) -> List[str]:
        out = self.header()
        for key, v in sorted(self._values.items()):
            out.append(f"{self.name}{_labels(self.label_names, key)} {v:g}")
        return out


class Gauge(_Metric):
    kind = "gauge"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._values: Dict[Tuple[str, ...], float] = {}

    def set(self, value: float, **labels: str) -> None:
        with self._lock:
            self._values[self._key(labels)] = float(value)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        with self._lock:
            key = self._key(labels)
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        return self._values.get(self._key(labels), 0.0)

    def render(self) -> List[str]:
        out = self.header()
        for key, v in sorted(self._values.items()):
            out.append(f"{self.name}{_labels(self.label_names, key)} {v:g}")
        return out


class Histogram(_Metric):
    kind = "histogram"

    def __init__(self, name, help_, label_names=(), buckets: Iterable[float] = LATENCY_BUCKETS):
        super().__init__(name, help_, label_names)
        self.buckets = tuple(sorted(buckets))
        self._data: Dict[Tuple[str, ...], List[float]] = {}   # key -> [bucket counts..., sum, count]

    def observe(self, value: float, **labels: str) -> None:
        with self._lock:
            row = self._data.setdefault(self._key(labels), [0.0] * (len(self.buckets) + 2))
            for i, b in enumerate(self.buckets):
                if value <= b:
                    row[i] += 1
            row[-2] += value
            row[-1] += 1

    def count(self, **labels: str) -> float:
        row = self._data.get(self._key(labels))
        return row[-1] if row else 0.0

    def render(self) -> List[str]:
        out = self.header()
        for key, row in sorted(self._data.items()):
            for b, c in zip(self.buckets, row):
                out.append(f"{self.name}_bucket{_labels(self.label_names + ('le',), key + (f'{b:g}',))} {c:g}")
            out.append(f"{self.name}_bucket{_labels(self.label_names + ('le',), key + ('+Inf',))} {row[-1]:g}")
            out.append(f"{self.name}_sum{_labels(self.label_names, key)} {row[-2]:g}")
            out.append(f"{self.name}_count{_labels(self.label_names, key)} {row[-1]:g}")
        return out


class Registry:
    def __init__(self):
        self._metrics: List[_Metric] = []

    def add(self, metric: _Metric):
        self._metrics.append(metric)
        return metric

    def render(self) -> str:
        lines: List[str] = []
        for m in self._metrics:
            lines += m.render()
        return "\n".join(lines) + "\n"


REGISTRY = Registry()

WS_CONNECTIONS = REGISTRY.add(Gauge("agent_ws_connections", "Open WebSocket connections."))
SESSIONS = REGISTRY.add(Gauge("agent_sessions", "Sessions held in memory (set at scrape time)."))
EVENTS = REGISTRY.add(Counter("agent_events_total", "Inbound events by type.", ["type"]))
ACTIONS = REGISTRY.add(Counter("agent_actions_total", "Outbound actions by type.", ["type"]))
FIRST_ACK = REGISTRY.add(Histogram(
    "agent_first_ack_seconds", "User text/interrupt event received -> first acknowledgement (filler) emitted."))
CANCEL_LATENCY = REGISTRY.add(Histogram(
    "agent_interrupt_cancel_seconds", "Interrupting event received -> first tool cancellation emitted."))
SPEECH_STOP = REGISTRY.add(Histogram(
    "agent_speech_stop_seconds", "Interrupt noticed -> the agent's voice stopped (spoken replies)."))
LLM_REQUESTS = REGISTRY.add(Counter(
    "agent_llm_requests_total", "LLM requests by outcome (ok|timeout|error|rate_limited|fallback|circuit_open).", ["outcome"]))
LLM_LATENCY = REGISTRY.add(Histogram("agent_llm_latency_seconds", "LLM request latency (successful requests)."))
LLM_CIRCUIT_OPEN = REGISTRY.add(Gauge("agent_llm_circuit_open", "1 while the LLM circuit breaker is open."))
LLM_SOFT_DEADLINE = REGISTRY.add(Counter("agent_llm_soft_deadline_total", "Requests that outlived the soft deadline."))
REJECTED = REGISTRY.add(Counter(
    "agent_rejected_total", "Connections/messages turned away by reason "
    "(origin|auth|session_id|ip_limit|session_limit|rate_limit|too_large).", ["reason"]))
TRACE_DROPPED = REGISTRY.add(Gauge("agent_trace_dropped_records", "Trace records dropped for failing validation."))
