"""Canonical forms for argument/slot values, used so that *equivalent* requests compare equal.

An LLM phrases the same booking differently from call to call ("Mumbai" vs "BOM", "delhi" vs "DEL").
Idempotency keyed on raw strings treats those as different requests, and a double booking results.
Location-ish values are therefore mapped to their IATA code (when known) and everything else is
lower-cased and whitespace-normalized before hashing.

The city table is deliberately modest -- unknown places fall back to lower-case comparison, which is
still strictly better than raw strings. Extend it as needed (or swap in a real geocoder).
"""

from __future__ import annotations

from typing import Any, Dict

LOCATION_KEYS = {"origin", "destination", "city", "from", "to", "location"}

_CITY_TO_IATA: Dict[str, str] = {
    # India
    "delhi": "DEL", "new delhi": "DEL", "mumbai": "BOM", "bombay": "BOM", "bangalore": "BLR", "bengaluru": "BLR",
    "goa": "GOI", "chennai": "MAA", "madras": "MAA", "kolkata": "CCU", "calcutta": "CCU", "hyderabad": "HYD",
    "pune": "PNQ", "ahmedabad": "AMD", "jaipur": "JAI", "kochi": "COK", "cochin": "COK", "lucknow": "LKO",
    "goa dabolim": "GOI", "goa mopa": "GOX",
    # World
    "new york": "JFK", "new york city": "JFK", "nyc": "JFK", "boston": "BOS", "chicago": "ORD", "san francisco": "SFO",
    "los angeles": "LAX", "seattle": "SEA", "washington": "IAD", "miami": "MIA", "london": "LHR", "paris": "CDG",
    "frankfurt": "FRA", "amsterdam": "AMS", "dubai": "DXB", "singapore": "SIN", "tokyo": "HND", "seoul": "ICN",
    "sydney": "SYD", "toronto": "YYZ", "hong kong": "HKG", "bangkok": "BKK",
}
_KNOWN_CODES = set(_CITY_TO_IATA.values())


def canonical_value(key: str, value: Any) -> Any:
    if isinstance(value, str):
        v = " ".join(value.strip().lower().split())
        if key in LOCATION_KEYS:
            if v in _CITY_TO_IATA:
                return _CITY_TO_IATA[v]
            if len(v) == 3 and v.isalpha():
                return v.upper()
        return v
    return value


def canonical_mapping(values: Dict[str, Any]) -> Dict[str, Any]:
    return {k: canonical_value(k, v) for k, v in values.items()}
