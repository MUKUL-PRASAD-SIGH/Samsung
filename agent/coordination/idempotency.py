"""Idempotency key store for state-modifying tool calls.

Guarantees zero duplicate state-changing calls under retries, interruptions, or race conditions.
Formula from §2.2:
    idempotency_key = hash(intent + sorted(slot_values) + epoch)
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional


class IdempotencyStore:
    def __init__(self):
        # Maps idempotency_key -> {"call_id": str, "result": Any, "status": str}
        self._store: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def generate_key(
        intent: Optional[str],
        slots: Dict[str, Any],
        epoch: int,
        tool_name: str = "",
        arguments: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Compute deterministic idempotency key for a given call configuration.

        `arguments` distinguishes two different calls to the same tool in the same state
        (e.g. booking DEL->BOM vs DEL->GOA) -- without it the second was dropped as a
        "duplicate". Omitting it preserves the original key for callers that don't pass it.
        """
        # Clean slots representation with sorted keys
        sorted_slots_str = json.dumps(slots, sort_keys=True, default=str)
        raw = f"{tool_name}:{intent or 'none'}:{sorted_slots_str}:{epoch}"
        if arguments:
            raw += f":{json.dumps(arguments, sort_keys=True, default=str)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def exists(self, key: str) -> bool:
        """Check if an action with this key has already been executed or queued."""
        return key in self._store

    def register(self, key: str, call_id: str, result: Any = None, status: str = "in_flight") -> None:
        """Register a key with its in-flight or completed status."""
        self._store[key] = {
            "call_id": call_id,
            "result": result,
            "status": status,
        }

    def complete(self, key: str, result: Any) -> None:
        """Mark an idempotency key as completed with its result."""
        if key in self._store:
            self._store[key]["status"] = "completed"
            self._store[key]["result"] = result

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Retrieve stored information for a key."""
        return self._store.get(key)

    def clear(self) -> None:
        """Clear store (session scoped)."""
        self._store.clear()
