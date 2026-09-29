"""Action schemas for the interruptible real-time agent system.

Covers:
- Spoken fillers / acks (Tier 2 fast path)
- Tool calls (Tier 3 slow path)
- Tool cancellations (Epoch model coordination)
- Final or intermediate spoken responses
- State snapshots (§2.3)
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ActionType(str, Enum):
    FILLER = "filler"
    SPOKEN_RESPONSE = "spoken_response"
    TOOL_CALL = "tool_call"
    TOOL_CANCEL = "tool_cancel"
    STATE_SNAPSHOT = "state_snapshot"
    CLARIFICATION = "clarification"
    AGENT_STEP = "agent_step"
    GRAPH_SNAPSHOT = "graph_update"
    TRANSCRIPT = "transcript"


class BaseAction(BaseModel):
    action_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    action_type: ActionType
    epoch: int
    timestamp: float = Field(default_factory=time.time)
    payload: Dict[str, Any] = Field(default_factory=dict)


class FillerAction(BaseAction):
    action_type: ActionType = ActionType.FILLER
    text: str


class SpokenResponseAction(BaseAction):
    action_type: ActionType = ActionType.SPOKEN_RESPONSE
    text: str
    is_final: bool = True


class ClarificationAction(BaseAction):
    action_type: ActionType = ActionType.CLARIFICATION
    question: str
    target_slot: Optional[str] = None


class ToolCallAction(BaseAction):
    action_type: ActionType = ActionType.TOOL_CALL
    call_id: str
    tool_name: str
    arguments: Dict[str, Any]
    is_state_modifying: bool = False
    idempotency_key: Optional[str] = None


class ToolCancelAction(BaseAction):
    action_type: ActionType = ActionType.TOOL_CANCEL
    call_id: str
    tool_name: str
    reason: str = "epoch_stale"


class InFlightCallInfo(BaseModel):
    call_id: str
    tool: str
    epoch: int
    status: str = "pending"  # "pending", "running", "completed", "cancelled"


class StateSnapshotAction(BaseAction):
    action_type: ActionType = ActionType.STATE_SNAPSHOT
    intent: Optional[str] = None
    slots: Dict[str, Any] = Field(default_factory=dict)
    in_flight_calls: List[InFlightCallInfo] = Field(default_factory=list)
    last_updated: str


class AgentStepAction(BaseAction):
    action_type: ActionType = ActionType.AGENT_STEP
    call_id: str
    name: str
    role: str
    step: int
    total_steps: int
    thought: str
    status: str = "working"  # "working", "completed", "cancelled"
    artifact: Optional[Dict[str, Any]] = None


class TranscriptAction(BaseAction):
    """What the ASR heard for a submitted voice recording (empty text = no speech detected)."""

    action_type: ActionType = ActionType.TRANSCRIPT
    text: str
    asr_model: Optional[str] = None
    latency_ms: Optional[float] = None


class GraphNodePayload(BaseModel):
    id: str
    node_type: str  # "turn" | "entity" | "artifact"
    label: str
    data: Dict[str, Any] = Field(default_factory=dict)


class GraphEdgePayload(BaseModel):
    source: str
    target: str
    edge_type: str  # "NEXT_TURN" | "SUPERSEDES" | "REFERENCES" | "PRODUCED" | "BRANCHES_FROM"


class GraphUpdateAction(BaseAction):
    action_type: ActionType = ActionType.GRAPH_SNAPSHOT
    nodes: List[GraphNodePayload] = Field(default_factory=list)
    edges: List[GraphEdgePayload] = Field(default_factory=list)
    op: str = "append"  # "append" (incremental) | "full" (full resync, e.g. after rollback)

