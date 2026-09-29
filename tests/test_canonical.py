"""Equivalent requests must compare equal, or a re-phrased duplicate books twice."""

from agent.coordination.canonical import canonical_mapping, canonical_value
from agent.coordination.idempotency import IdempotencyStore
from agent.coordination.state_machine import SessionState


def test_city_names_and_airport_codes_canonicalize_together():
    assert canonical_value("destination", "Mumbai") == canonical_value("destination", "BOM") == "BOM"
    assert canonical_value("origin", " new  delhi ") == canonical_value("origin", "del") == "DEL"
    assert canonical_value("city", "Goa") == "GOI"


def test_unknown_places_and_non_location_keys_only_normalize_case_and_space():
    assert canonical_value("destination", "  Timbuktu ") == "timbuktu"
    assert canonical_value("hotel_name", "The Grand") == "the grand"   # not treated as a place
    assert canonical_value("nights", 3) == 3
    assert canonical_value("destination", "xyz") == "XYZ"               # 3 letters -> looks like a code


def test_idempotency_key_ignores_phrasing_but_not_substance():
    key = IdempotencyStore.generate_key
    a = key("book", {"origin": "DEL", "destination": "BOM"}, 1, "book_flight", {"origin": "DEL", "destination": "BOM"})
    b = key("book", {"origin": "Delhi", "destination": "Mumbai"}, 1, "book_flight", {"origin": "delhi", "destination": "mumbai"})
    c = key("book", {"origin": "DEL", "destination": "GOI"}, 1, "book_flight", {"origin": "DEL", "destination": "GOI"})
    assert a == b and a != c


def test_rephrased_state_changing_call_is_blocked_by_the_session():
    s = SessionState("canon")
    first = s.register_tool_call("c1", "book_flight", {"origin": "DEL", "destination": "BOM"}, is_state_modifying=True)
    again = s.register_tool_call("c2", "book_flight", {"origin": "Delhi", "destination": "Mumbai"}, is_state_modifying=True)
    other = s.register_tool_call("c3", "book_flight", {"origin": "Delhi", "destination": "Goa"}, is_state_modifying=True)
    assert first is not None and again is None and other is not None
