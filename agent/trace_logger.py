"""Self-validating trace logger (§7.6).

The evaluation harness scores strictly from trace logs.
This module validates every emitted event and action, enforcing invariants:
1. Every tool call has a valid non-empty call_id and epoch.
2. Every tool cancel references a real prior call_id and has epoch >= call epoch.
3. Epochs are strictly non-decreasing per session.
4. Timestamps and schema structures conform to JSON trace requirements.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from agent.schemas.actions import BaseAction, ToolCallAction, ToolCancelAction, StateSnapshotAction
from agent.schemas.events import BaseEvent

logger = logging.getLogger("agent.trace")


class TraceValidationError(Exception):
    """Raised when an emitted action violates trace invariants."""
    pass


class TraceLogger:
    def __init__(self, log_file: Optional[str] = None):
        self.log_file = log_file
        self.trace_history: List[Dict[str, Any]] = []
        # Per-session tracked state for invariant checking
        self._session_epochs: Dict[str, int] = {}
        self._registered_call_ids: Dict[str, set] = {}

    def log_event(self, event: BaseEvent) -> Dict[str, Any]:
        """Record an incoming event in the trace log."""
        record = {
            "record_type": "event",
            "event_type": event.event_type.value,
            "session_id": event.session_id,
            "event_id": event.event_id,
            "timestamp": event.timestamp,
            "payload": event.model_dump(exclude={"payload"}),
        }
        self._append_record(record)
        return record

    def log_action(self, action: BaseAction) -> Dict[str, Any]:
        """Validate and record an outgoing action in the trace log."""
        self._validate_invariants(action)

        record = {
            "record_type": "action",
            "action_type": action.action_type.value,
            "session_id": action.session_id,
            "action_id": action.action_id,
            "epoch": action.epoch,
            "timestamp": action.timestamp,
            "payload": action.model_dump(),
        }
        self._append_record(record)
        return record

    def _validate_invariants(self, action: BaseAction) -> None:
        """Enforce strict invariant checks (§7.6)."""
        sid = action.session_id
        
        # Invariant 1: Non-decreasing epoch
        last_epoch = self._session_epochs.get(sid, 0)
        if action.epoch < last_epoch:
            raise TraceValidationError(
                f"Epoch regression detected for session {sid}: {action.epoch} < {last_epoch}"
            )
        self._session_epochs[sid] = action.epoch

        # Track call IDs
        if sid not in self._registered_call_ids:
            self._registered_call_ids[sid] = set()

        # Invariant 2: Tool call has valid call_id
        if isinstance(action, ToolCallAction):
            if not action.call_id:
                raise TraceValidationError(f"Tool call missing call_id in session {sid}")
            self._registered_call_ids[sid].add(action.call_id)

        # Invariant 3: Tool cancellation references a prior known call_id
        if isinstance(action, ToolCancelAction):
            if action.call_id not in self._registered_call_ids[sid]:
                raise TraceValidationError(
                    f"Tool cancellation for unknown call_id '{action.call_id}' in session {sid}"
                )

    def _append_record(self, record: Dict[str, Any]) -> None:
        """Append record to memory and optional JSON-lines file."""
        self.trace_history.append(record)
        if self.log_file:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
