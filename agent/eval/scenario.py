"""Scenario definitions: a scripted user timeline plus what a correct agent must end up doing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from agent.coordination.fault_injection import FaultInjectionConfig
from agent.llm_client import LLMResponse

Acceptable = Union[str, int, float, Tuple[Any, ...]]  # one value, or a tuple of acceptable alternatives


@dataclass
class Step:
    """One stimulus on the timeline, `at_s` seconds after scenario start."""

    at_s: float
    kind: str = "text"            # "text" | "interrupt" (explicit InterruptSignalEvent) | "voice" | "frame"
    text: str = ""                # text steps
    frame_text: str = ""          # frame steps: text rendered onto the synthetic image (e.g. a poster reading "GOA")
    audio: Optional[str] = None   # voice steps: fixture file name in tests/fixtures/audio
    lead_s: float = 0.3           # voice: silence before speech begins
    tail_s: float = 1.5           # voice: silence after speech (lets the endpointer close the utterance)
    interrupts: bool = False      # this stimulus is meant to interrupt in-flight work
    is_turn: bool = True          # counts as a user turn for latency / answered checks


@dataclass
class ExpectedCall:
    """A tool execution that must have COMPLETED (i.e. survived any later interruption)."""

    tool: Union[str, Tuple[str, ...]]
    args: Dict[str, Acceptable] = field(default_factory=dict)  # required arg values (subset match)
    count: int = 1


@dataclass
class Scenario:
    name: str
    description: str
    tags: Tuple[str, ...]
    steps: List[Step]
    expected_calls: List[ExpectedCall] = field(default_factory=list)
    expected_slots: Dict[str, Acceptable] = field(default_factory=dict)   # checked on the LAST emitted snapshot
    forbidden_slot_values: Dict[str, List[str]] = field(default_factory=dict)  # stale values that must not survive
    forbid_completed: List[str] = field(default_factory=list)             # tools that must NOT complete
    latency_s: Dict[str, float] = field(default_factory=dict)             # per-tool latency (default 1.5s)
    faults: Dict[str, FaultInjectionConfig] = field(default_factory=dict)
    mock_llm: List[LLMResponse] = field(default_factory=list)             # scripted LLM decisions (mock mode)
    mock_vision: List[str] = field(default_factory=list)                  # scripted vision answers (mock mode)
    vision_latency_s: float = 0.3                                          # mock vision latency
    expect_failure_notice: bool = False                                    # a failing tool must be reported
    speak: bool = False                                                    # the client has spoken replies switched on
    max_s: float = 25.0                                                    # scenario wall-clock cap

    @property
    def uses_voice(self) -> bool:
        return any(s.kind == "voice" for s in self.steps)
