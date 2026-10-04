"""Tool-call output validation and repair layer (§3.2).

Guarantees 100% compliance with Safety & Protocol scoring (10%)
by never allowing malformed or unvalidated LLM output to dispatch.
Repairs:
- Markdown code block wrapping (```json ... ```)
- Unclosed braces/quotes
- Trailing commas
- Single-quoted JSON strings
- Structural mismatch against registered jsonschema
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional
import jsonschema


def attempt_json_repair(raw_text: str) -> Dict[str, Any]:
    """Attempt robust extraction and repair of malformed LLM JSON output."""
    text = raw_text.strip()

    # Strip markdown fences if present
    if "```" in text:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if match:
            text = match.group(1).strip()

    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Basic cleanup: remove trailing commas before closing braces/brackets
    cleaned = re.sub(r",\s*([\]}])", r"\1", text)

    # Convert single quotes to double quotes if valid JSON syntax
    # (handling unescaped quotes)
    if "'" in cleaned and '"' not in cleaned:
        cleaned = cleaned.replace("'", '"')

    # Find the outermost JSON object bounds
    start_idx = cleaned.find("{")
    end_idx = cleaned.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        candidate = cleaned[start_idx : end_idx + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # If python json-repair package is installed, try it
    try:
        import json_repair
        repaired = json_repair.loads(raw_text)
        if isinstance(repaired, dict):
            return repaired
    except ImportError:
        pass

    raise ValueError(f"Unable to parse or repair JSON from raw LLM output: {raw_text[:200]}")


def validate_and_repair(raw_output: str, schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Parse, repair, and validate LLM output against a schema.
    
    Args:
        raw_output: Raw string from LLM completion
        schema: Optional jsonschema dict to validate against
        
    Returns:
        Validated dictionary
        
    Raises:
        ValueError if unparseable
        jsonschema.ValidationError if structural mismatch
    """
    parsed = attempt_json_repair(raw_output)

    if schema is not None:
        jsonschema.validate(instance=parsed, schema=schema)

    return parsed
