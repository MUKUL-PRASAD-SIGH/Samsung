"""Validation of slot patches (spec §7.4: snapshot versioning with rollback on a failed patch).

A patch is checked BEFORE it is applied (types, sizes, ranges, date sanity) and the resulting state is checked
AFTER (cross-field rules); a bad resulting state is undone with `SessionState.rollback_snapshot()`. Either way a
bad LLM argument never becomes session state, and the caller gets a `SlotPatchError` whose `user_message` is safe
to ask the user.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict

from agent.coordination.canonical import canonical_value

MAX_TEXT_LEN = 200
MAX_NIGHTS = 60
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class SlotPatchError(ValueError):
    def __init__(self, field: str, message: str, user_message: str):
        super().__init__(f"{field}: {message}")
        self.field = field
        self.user_message = user_message


def validate_patch(diff: Dict[str, Any]) -> None:
    """Reject a patch that must not reach session state. Raises SlotPatchError; never mutates anything."""
    for key, value in diff.items():
        if not isinstance(key, str) or not key:
            raise SlotPatchError(str(key), "slot name must be a non-empty string", "I couldn't make sense of that detail.")
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise SlotPatchError(key, f"unsupported value type {type(value).__name__}", f"I couldn't understand the {key}.")
        if isinstance(value, str):
            if len(value) > MAX_TEXT_LEN or _CONTROL.search(value):
                raise SlotPatchError(key, "text too long or contains control characters", f"That {key} doesn't look right.")
        if key == "nights":
            if isinstance(value, float) or isinstance(value, str) or not (1 <= value <= MAX_NIGHTS):
                raise SlotPatchError(key, f"nights must be an integer in 1..{MAX_NIGHTS}, got {value!r}",
                                     f"How many nights did you mean? (1 to {MAX_NIGHTS})")
        if key == "date" and isinstance(value, str):
            m = _ISO_DATE.match(value.strip())
            if m:  # free text ("next Friday") is allowed; a precise date must be a real one
                try:
                    parsed = date(int(m[1]), int(m[2]), int(m[3]))
                except ValueError:
                    raise SlotPatchError(key, f"{value!r} is not a real calendar date", f"{value} isn't a real date. Which day did you mean?")
                if not (2000 <= parsed.year <= 2100):
                    raise SlotPatchError(key, f"year out of range in {value!r}", f"Did you really mean {value}?")


def validate_state(slots: Dict[str, Any]) -> None:
    """Cross-field rules on the state a patch would produce."""
    origin, destination = slots.get("origin"), slots.get("destination")
    if origin and destination and canonical_value("origin", origin) == canonical_value("destination", destination):
        raise SlotPatchError("destination", f"origin and destination are both {origin!r}",
                             f"The origin and destination are both {origin} -- where do you want to go?")
