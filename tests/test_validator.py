"""Tests for Tool-Call Output Validation and Repair (§3.2)."""

import pytest
from jsonschema.exceptions import ValidationError
from agent.slow_path.validator import validate_and_repair, attempt_json_repair


def test_repair_markdown_code_blocks():
    raw = """
    ```json
    {
        "city": "London",
        "days": 4
    }
    ```
    """
    res = attempt_json_repair(raw)
    assert res == {"city": "London", "days": 4}


def test_repair_trailing_commas():
    raw = '{"origin": "SFO", "destination": "JFK",}'
    res = attempt_json_repair(raw)
    assert res == {"origin": "SFO", "destination": "JFK"}


def test_validate_and_repair_schema_success():
    schema = {
        "type": "object",
        "properties": {
            "origin": {"type": "string"},
            "destination": {"type": "string"},
        },
        "required": ["origin", "destination"],
    }
    raw = '```json {"origin": "BOS", "destination": "LAX"} ```'
    validated = validate_and_repair(raw, schema=schema)
    assert validated["origin"] == "BOS"
    assert validated["destination"] == "LAX"


def test_validate_and_repair_schema_failure():
    schema = {
        "type": "object",
        "properties": {
            "origin": {"type": "string"},
            "destination": {"type": "string"},
        },
        "required": ["origin", "destination"],
    }
    raw = '{"origin": "BOS"}'  # missing required destination
    with pytest.raises(ValidationError):
        validate_and_repair(raw, schema=schema)
