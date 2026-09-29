"""Session state machine and epoch coordination layer.

Implements:
- Monotonically increasing epoch model (§2.1)
- In-flight call registry with immediate cancellation of stale calls
- Diff-based slot state patching (§2.3)
- Snapshot versioning with rollback buffer (§7.4)
- Session snapshot generation matching eval rubric
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from pydantic import BaseModel, Field

from agent.schemas.actions import (
    ToolCallAction,
    ToolCancelAction,
    StateSnapshotAction,
    InFlightCallInfo,
)
from agent.coordination.idempotency import IdempotencyStore
from agent.memory.tool_slots import MISSING

if TYPE_CHECKING:
    from agent.memory.scratchpad import TurnScratchpad
    from agent.memory.graph_memory import GraphMemory


class InFlightCall(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    call_id: str
    tool_name: str
    epoch: int
    arguments: Dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"  # "pending", "running", "completed", "cancelled"
    created_at: float = Field(default_factory=time.time)
    asyncio_task: Optional[asyncio.Task] = None
    is_state_modifying: bool = False
    idempotency_key: Optional[str] = None


class SessionState:
    def __init__(self, session_id: str, max_history: int = 10):
        self.session_id: str = session_id
        self.epoch: int = 1
        self.intent: Optional[str] = None
        self.slots: Dict[str, Any] = {}
        self.in_flight_calls: Dict[str, InFlightCall] = {}
        self.idempotency_store: IdempotencyStore = IdempotencyStore()
        self.last_activity: float = time.time()

        # History ring buffer for rollback (§7.4)
        self.max_history: int = max_history
        self._history: List[Dict[str, Any]] = []
        self._lock = asyncio.Lock()

        # 2-tier cognitive memory (agent/memory/*). Lazily initialized via
        # ensure_memory() to avoid a state_machine.py <-> memory/*.py import
        # cycle; stays None for sessions that never opt in (e.g. bare
        # SessionState() instances constructed directly by tests), so all
        # existing behavior below is completely unaffected.
        # call_id -> {slot: (value before the call, value the call set)}; lets an aborted call's
        # slot changes be undone so abandoned values never survive in the snapshot.
        self._call_slot_undo: Dict[str, Dict[str, Any]] = {}
        self.scratchpad: Optional["TurnScratchpad"] = None
        self.graph_memory: Optional["GraphMemory"] = None

        # Record initial snapshot
        self._save_to_history()

    def touch(self) -> None:
        """Mark the session as recently active; resets the idle-eviction clock (gap #8)."""
        self.last_activity = time.time()

    def ensure_memory(self) -> None:
        """Lazily attach the L1 scratchpad and L2 graph memory to this session."""
        if self.graph_memory is None:
            from agent.memory.graph_memory import GraphMemory
            self.graph_memory = GraphMemory(self)
        if self.scratchpad is None:
            from agent.memory.scratchpad import TurnScratchpad
            self.scratchpad = TurnScratchpad(self)

    def _save_to_history(self) -> None:
        """Save current slot/intent state to the history ring buffer."""
        snapshot = {
            "epoch": self.epoch,
            "intent": self.intent,
            "slots": dict(self.slots),
            "timestamp": time.time(),
        }
        self._history.append(snapshot)
        if len(self._history) > self.max_history:
            self._history.pop(0)

    def rollback_snapshot(self) -> bool:
        """Rollback to the most recent previous valid snapshot (§7.4)."""
        if len(self._history) > 1:
            # Drop current bad state
            self._history.pop()
            previous = self._history[-1]
            self.slots = dict(previous["slots"])
            self.intent = previous["intent"]
            return True
        return False

    def patch_slots(self, slot_diff: Dict[str, Any], new_intent: Optional[str] = None) -> None:
        """Apply a diff-based patch to slot state, preserving existing slots (§2.3)."""
        if new_intent is not None:
            self.intent = new_intent
        self.slots.update(slot_diff)
        self._save_to_history()

    def stage_call_slots(self, call_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
        """Patch slots with a tool call's arguments at DISPATCH time (§2.3: the snapshot shows slots
        while the call is still in flight). Returns each slot's previous value (MISSING if none).

        The undo record lets `revert_call_slots` restore them if the call is aborted, so a value the
        user abandoned ("Mumbai" before "actually, Goa") can't linger in the snapshot.
        """
        previous = {k: self.slots.get(k, MISSING) for k in values}
        self._call_slot_undo[call_id] = {k: (previous[k], v) for k, v in values.items()}
        self.patch_slots(values)
        return previous

    def revert_call_slots(self, call_id: str) -> None:
        """Undo a call's slot changes -- only where the slot still holds the value that call set."""
        undo = self._call_slot_undo.pop(call_id, None)
        if not undo:
            return
        changed = False
        for key, (previous, set_value) in undo.items():
            if key in self.slots and self.slots[key] == set_value:
                if previous is MISSING:
                    del self.slots[key]
                else:
                    self.slots[key] = previous
                changed = True
        if changed:
            self._save_to_history()

    def bump_epoch(self, reason: str = "user_interrupt") -> List[ToolCancelAction]:
        """Increment epoch and immediately cancel all stale in-flight calls (§2.1).
        
        Returns:
            List of ToolCancelAction objects to be dispatched to the Action Queue.
        """
        self.epoch += 1
        cancellations: List[ToolCancelAction] = []

        for call_id, call in self.in_flight_calls.items():
            if call.epoch < self.epoch and call.status in ("pending", "running"):
                # Abort asyncio task if running
                if call.asyncio_task and not call.asyncio_task.done():
                    call.asyncio_task.cancel()
                call.status = "cancelled"
                self.revert_call_slots(call.call_id)
                cancellations.append(
                    ToolCancelAction(
                        session_id=self.session_id,
                        epoch=self.epoch,
                        call_id=call.call_id,
                        tool_name=call.tool_name,
                        reason=reason,
                    )
                )
                if self.scratchpad is not None:
                    self.scratchpad.mark_aborted(call.call_id, call.tool_name, reason, call.arguments)

        return cancellations

    def register_tool_call(
        self,
        call_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        is_state_modifying: bool = False,
        task: Optional[asyncio.Task] = None,
    ) -> Optional[ToolCallAction]:
        """Register a new tool call under the current epoch.
        
        Applies idempotency check for state-modifying calls (§2.2).
        Returns ToolCallAction if valid, or None if skipped due to idempotency.
        """
        idempotency_key = None
        if is_state_modifying:
            idempotency_key = self.idempotency_store.generate_key(
                intent=self.intent,
                slots=self.slots,
                epoch=self.epoch,
                tool_name=tool_name,
                arguments=arguments,
            )
            if self.idempotency_store.exists(idempotency_key):
                # Duplicate state-modifying call detected: skip to prevent duplicate action
                return None
            self.idempotency_store.register(idempotency_key, call_id=call_id)

        call = InFlightCall(
            call_id=call_id,
            tool_name=tool_name,
            epoch=self.epoch,
            arguments=arguments,
            status="pending",
            asyncio_task=task,
            is_state_modifying=is_state_modifying,
            idempotency_key=idempotency_key,
        )
        self.in_flight_calls[call_id] = call

        return ToolCallAction(
            session_id=self.session_id,
            epoch=self.epoch,
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            is_state_modifying=is_state_modifying,
            idempotency_key=idempotency_key,
        )

    def attach_task(self, call_id: str, task: asyncio.Task) -> None:
        """Attach background asyncio task to an in-flight call for tracking and cancellation."""
        if call_id in self.in_flight_calls:
            self.in_flight_calls[call_id].asyncio_task = task
            self.in_flight_calls[call_id].status = "running"

    def complete_tool_call(self, call_id: str, result: Any = None, error: Optional[str] = None) -> bool:
        """Mark a tool call as completed.
        
        Returns False if the call was already cancelled or superseded by a newer epoch.
        """
        call = self.in_flight_calls.get(call_id)
        if not call:
            return False

        # If call was cancelled or belongs to an older epoch, discard result
        if call.status == "cancelled" or call.epoch < self.epoch:
            return False

        call.status = "completed"
        self._call_slot_undo.pop(call_id, None)  # the call finished: its slot values are now permanent
        if call.idempotency_key:
            self.idempotency_store.complete(call.idempotency_key, result=result)
        return True

    def get_snapshot(self) -> StateSnapshotAction:
        """Build the session snapshot conforming to §2.3."""
        in_flight_list = [
            InFlightCallInfo(
                call_id=c.call_id,
                tool=c.tool_name,
                epoch=c.epoch,
                status=c.status,
            )
            for c in self.in_flight_calls.values()
            if c.status in ("pending", "running")
        ]

        now_iso = datetime.now(timezone.utc).isoformat()
        return StateSnapshotAction(
            session_id=self.session_id,
            epoch=self.epoch,
            intent=self.intent,
            slots=dict(self.slots),
            in_flight_calls=in_flight_list,
            last_updated=now_iso,
        )
