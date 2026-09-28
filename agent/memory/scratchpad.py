"""Tier 1: Ephemeral Turn Scratchpad ("L1 Working Cache").

Absorbs barge-in noise, keystroke drafts, slot diffs, and abort signals for the
*current* turn only. All slot mutations funnel through SessionState.patch_slots()
so the existing diff/rollback ring buffer (agent/coordination/state_machine.py)
remains the single source of truth for slot state -- this module never writes
to session.slots directly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from agent.coordination.state_machine import SessionState


@dataclass
class SlotDelta:
    slot_name: str
    original_value: Any
    revised_value: Any
    epoch: int
    timestamp: float = field(default_factory=time.time)


@dataclass
class InFlightCallRecord:
    call_id: str
    tool_name: str
    arguments: Dict[str, Any]
    status: str  # "running", "cancelled", "completed"
    epoch: int


@dataclass
class EntityCandidate:
    entity_type: str
    key: str
    value: Any
    epoch: int


@dataclass
class CanonicalTurn:
    session_id: str
    turn_id: str
    epoch: int
    cleaned_user_intent: str
    final_slots: Dict[str, Any]
    agent_response: str
    artifacts_produced: List[Dict[str, Any]] = field(default_factory=list)
    entity_candidates: List[EntityCandidate] = field(default_factory=list)
    intent_shift: Optional[str] = None
    aborted_calls_count: int = 0
    timestamp: float = field(default_factory=time.time)


class TurnScratchpad:
    """High-velocity, volatile scratchpad scoped to the current turn/active utterance."""

    def __init__(self, session: "SessionState"):
        self._session = session
        self.raw_chunks: List[str] = []
        self.slot_deltas: List[SlotDelta] = []
        self.aborted_calls: List[InFlightCallRecord] = []
        self.entity_candidates: List[EntityCandidate] = []
        self.intent_shift: Optional[str] = None
        self.turn_epoch_start: int = session.epoch
        self._turn_counter: int = 0

    def append_utterance_chunk(self, chunk: str) -> None:
        self.raw_chunks.append(chunk)

    def record_slot_diff(self, slot_name: str, new_val: Any) -> None:
        """Record a candidate slot change for this turn. Not applied to session.slots
        until commit() -- keeps SessionState.slots pristine while a turn is in flight."""
        original = self._session.slots.get(slot_name)
        self.slot_deltas.append(
            SlotDelta(
                slot_name=slot_name,
                original_value=original,
                revised_value=new_val,
                epoch=self._session.epoch,
            )
        )

    def record_entity_candidate(self, entity_type: str, key: str, value: Any) -> None:
        self.entity_candidates.append(
            EntityCandidate(entity_type=entity_type, key=key, value=value, epoch=self._session.epoch)
        )

    def record_intent_shift(self, new_intent: str) -> None:
        self.intent_shift = new_intent

    def mark_aborted(self, call_id: str, tool_name: str, reason: str) -> None:
        """Called when session.bump_epoch() cancels an in-flight call belonging to this turn."""
        self.aborted_calls.append(
            InFlightCallRecord(
                call_id=call_id,
                tool_name=tool_name,
                arguments={},
                status="cancelled",
                epoch=self._session.epoch,
            )
        )

    def _next_turn_id(self) -> str:
        self._turn_counter += 1
        return f"{self._session.session_id}_turn_{self._turn_counter}_{int(time.time() * 1000)}"

    def commit(self, agent_response: str, artifacts: Optional[List[Dict[str, Any]]] = None) -> CanonicalTurn:
        """Distill this turn's noisy inputs into a clean CanonicalTurn and purge the scratchpad.

        The only place this class mutates SessionState: applies the final (last-write-wins)
        slot diff via session.patch_slots(), reusing the exact same method/behavior already
        covered by tests/test_state_machine.py.
        """
        final_diff: Dict[str, Any] = {}
        for delta in self.slot_deltas:
            final_diff[delta.slot_name] = delta.revised_value
        for cand in self.entity_candidates:
            final_diff[cand.key] = cand.value

        if final_diff or self.intent_shift:
            self._session.patch_slots(final_diff, new_intent=self.intent_shift)

        cleaned_intent = " ".join(self.raw_chunks[-1:]) if self.raw_chunks else ""

        canonical_turn = CanonicalTurn(
            session_id=self._session.session_id,
            turn_id=self._next_turn_id(),
            epoch=self._session.epoch,
            cleaned_user_intent=cleaned_intent,
            final_slots=dict(self._session.slots),
            agent_response=agent_response,
            artifacts_produced=list(artifacts or []),
            entity_candidates=list(self.entity_candidates),
            intent_shift=self.intent_shift,
            aborted_calls_count=len(self.aborted_calls),
        )

        # Purge: reset scratchpad for the next turn.
        self.raw_chunks = []
        self.slot_deltas = []
        self.aborted_calls = []
        self.entity_candidates = []
        self.intent_shift = None
        self.turn_epoch_start = self._session.epoch

        return canonical_turn
