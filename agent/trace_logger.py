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
import os
from typing import Any, Dict, List, Optional

import jsonschema

from agent.schemas.actions import BaseAction, ToolCallAction, ToolCancelAction
from agent.schemas.events import BaseEvent

logger = logging.getLogger("agent.trace")

DEFAULT_MAX_HISTORY = 5000
# Roll the trace file over once it exceeds this size, keeping one prior file (`.1`).
DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024

# Versioned so a future field change can be detected by consumers reading old trace files (§7.6).
TRACE_SCHEMA_VERSION = 1

TRACE_RECORD_SCHEMA = {
    "type": "object",
    "required": ["schema_version", "record_type", "session_id", "timestamp", "payload"],
    "properties": {
        "schema_version": {"type": "integer", "minimum": 1},
        "record_type": {"enum": ["event", "action", "classification"]},
        "event_type": {"type": "string"},
        "action_type": {"type": "string"},
        "session_id": {"type": "string", "minLength": 1},
        "event_id": {"type": "string"},
        "action_id": {"type": "string"},
        "epoch": {"type": "integer", "minimum": 0},
        "timestamp": {"type": "number"},
        "payload": {"type": "object"},
    },
    "additionalProperties": False,
}

_TRACE_VALIDATOR = jsonschema.Draft202012Validator(TRACE_RECORD_SCHEMA)


def _redact_bytes(values: Dict[str, Any]) -> Dict[str, Any]:
    """Replace raw media (audio/video frames) with a size marker: keeps the trace small and JSON-serializable."""
    def redact(k, v):
        if isinstance(v, (bytes, bytearray)):
            return f"<{len(v)} bytes>"
        if k == "audio_b64" and isinstance(v, str):      # synthesized speech: a size marker, not 100 KB of base64
            return f"<{len(v) * 3 // 4} bytes>"
        return v

    return {k: redact(k, v) for k, v in values.items()}


class TraceValidationError(Exception):
    """Raised when an emitted action violates trace invariants."""
    pass


class TraceLogger:
    def __init__(
        self,
        log_file: Optional[str] = None,
        max_history: int = DEFAULT_MAX_HISTORY,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        strict: bool = True,
    ):
        self.log_file = log_file
        self.max_file_bytes = max_file_bytes
        self.max_history = max_history
        # Dev/CI: raise on a malformed record so the bug is caught where it happened.
        # Eval/prod: drop the record and count it instead of taking the session down.
        self.strict = strict
        self.dropped_count = 0
        self.trace_history: List[Dict[str, Any]] = []
        # Per-session tracked state for invariant checking
        self._session_epochs: Dict[str, int] = {}
        self._registered_call_ids: Dict[str, set] = {}

    def log_event(self, event: BaseEvent) -> Optional[Dict[str, Any]]:
        """Record an incoming event in the trace log."""
        record = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "record_type": "event",
            "event_type": event.event_type.value,
            "session_id": event.session_id,
            "event_id": event.event_id,
            "timestamp": event.timestamp,
            "payload": _redact_bytes(event.model_dump(exclude={"payload"})),
        }
        if not self._validate_schema(record):
            return None
        self._append_record(record)
        return record

    def log_action(self, action: BaseAction) -> Optional[Dict[str, Any]]:
        """Validate and record an outgoing action in the trace log."""
        self._validate_invariants(action)

        record = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "record_type": "action",
            "action_type": action.action_type.value,
            "session_id": action.session_id,
            "action_id": action.action_id,
            "epoch": action.epoch,
            "timestamp": action.timestamp,
            "payload": _redact_bytes(action.model_dump()),
        }
        if not self._validate_schema(record):
            return None
        self._append_record(record)
        return record

    def log_classification(self, session_id: str, text: str, result: Dict[str, Any], timestamp: float) -> Optional[Dict[str, Any]]:
        """Record a Tier-1 interrupt decision with its raw scores/features (§7.2), so a threshold is auditable."""
        record = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "record_type": "classification",
            "session_id": session_id,
            "timestamp": timestamp,
            "payload": {"text": text, **result},
        }
        if not self._validate_schema(record):
            return None
        self._append_record(record)
        return record

    def _validate_schema(self, record: Dict[str, Any]) -> bool:
        """Enforce the versioned trace record schema (§7.6). Returns False if the record was dropped."""
        try:
            _TRACE_VALIDATOR.validate(record)
        except jsonschema.ValidationError as e:
            if self.strict:
                raise TraceValidationError(f"Trace record failed schema validation: {e.message}") from e
            self.dropped_count += 1
            logger.warning("Dropping malformed trace record (%s): %s", record.get("record_type"), e.message)
            return False
        return True

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
        if len(self.trace_history) > self.max_history:
            del self.trace_history[: len(self.trace_history) - self.max_history]
        if self.log_file:
            self._rotate_if_needed()
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

    def _rotate_if_needed(self) -> None:
        """Rename the current trace file to `<path>.1` once it exceeds the size cap."""
        try:
            if os.path.getsize(self.log_file) < self.max_file_bytes:
                return
        except OSError:
            return
        rotated = f"{self.log_file}.1"
        try:
            os.replace(self.log_file, rotated)
        except OSError as e:
            logger.warning("Trace log rotation failed for %s: %s", self.log_file, e)
