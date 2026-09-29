"""Deterministic slot/entity extraction from tool-call arguments.

Live probing showed the LLM returns a tool call (with no text) for nearly every action turn, so
a text-only `MEMORY_UPDATE` can never capture them. The arguments the model chose for the tool
already are the resolved parameters ("Mumbai" -> "Goa" after a correction), so we record them
as entities directly -- no dependence on the model following an output format.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

# spawn_agent arguments describe the worker (persona, plan, artifact spec), not user facts.
SKIP_TOOLS = {"spawn_agent"}

ENTITY_TYPE_BY_KEY: Dict[str, str] = {
    "origin": "LOCATION",
    "destination": "LOCATION",
    "city": "LOCATION",
    "date": "DATE",
    "nights": "QUANTITY",
    "flight": "FLIGHT",
    "hotel_name": "HOTEL",
    "booking_id": "BOOKING",
}


def entities_from_tool_call(tool_name: str, arguments: Dict[str, Any]) -> List[Tuple[str, str, Any]]:
    """Return (entity_type, key, value) for each scalar, non-empty tool argument."""
    if tool_name in SKIP_TOOLS or not arguments:
        return []
    entities: List[Tuple[str, str, Any]] = []
    for key, value in arguments.items():
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        if isinstance(value, str) and not value.strip():
            continue
        entities.append((ENTITY_TYPE_BY_KEY.get(key, "PARAM"), key, value))
    return entities
