"""Tests for Tier 2 Fast Path Templates (§3)."""

from agent.fast_path.templates import generate_filler


def test_filler_generation_slot_filling():
    # Slot filled successfully
    text = generate_filler(
        intent="search_flights",
        slots={"origin": "BLR", "destination": "DEL"},
        is_interruption=False,
    )
    assert "BLR" in text
    assert "DEL" in text
    assert "{" not in text


def test_filler_generation_partial_slots_fallback():
    # Partial slots (e.g. only destination)
    text = generate_filler(
        intent="search_flights",
        slots={"destination": "NYC"},
        is_interruption=False,
    )
    assert "{" not in text
    assert "flights" in text.lower()


def test_filler_generation_interruption_ack():
    text = generate_filler(is_interruption=True)
    assert any(w in text.lower() for w in ["changing", "switching", "stopping", "cancelled", "got it"])
