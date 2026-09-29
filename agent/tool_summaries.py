"""Deterministic natural-language summaries of tool results and failures.

Used by the coordinator so every completed tool call gets a visible reply in the
conversation (previously only artifact-producing agent workers did), without adding an
extra LLM round-trip to the latency-sensitive path.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _flights(result: Dict[str, Any]) -> str:
    flights: List[Dict[str, Any]] = result.get("flights") or []
    if not flights:
        return "I couldn't find any flights for that route."
    first = flights[0]
    route = f"{first.get('origin', '?')} to {first.get('destination', '?')}"
    cheapest = min(flights, key=lambda f: _price(f.get("price")))
    options = "; ".join(
        f"{f.get('airline', f.get('flight'))} {f.get('flight')} at {f.get('departure')} for {f.get('price')}"
        for f in flights[:3]
    )
    return (
        f"I found {len(flights)} flights from {route}: {options}. "
        f"The cheapest is {cheapest.get('airline')} {cheapest.get('flight')} at {cheapest.get('price')}."
    )


def _price(value: Any) -> float:
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        return float("inf")


def _hotels(result: Dict[str, Any]) -> str:
    hotels: List[Dict[str, Any]] = result.get("hotels") or []
    if not hotels:
        return "I couldn't find any hotels there."
    options = "; ".join(
        f"{h.get('name')} ({h.get('stars')} stars, {h.get('price_per_night')}/night, rated {h.get('rating')})"
        for h in hotels[:3]
    )
    return f"I found {len(hotels)} hotels: {options}."


def _book_flight(result: Dict[str, Any]) -> str:
    return (
        f"Your flight {result.get('flight')} from {result.get('origin')} to {result.get('destination')} "
        f"is {result.get('status', 'booked')}. Booking ID: {result.get('booking_id')}."
    )


def _book_hotel(result: Dict[str, Any]) -> str:
    return (
        f"Your stay at {result.get('hotel')} in {result.get('city')} for {result.get('nights')} nights "
        f"is {result.get('status', 'booked')}. Reservation ID: {result.get('reservation_id')}."
    )


def _weather(result: Dict[str, Any]) -> str:
    return (
        f"It's {str(result.get('condition', 'unknown')).lower()} in {result.get('city')} right now: "
        f"{result.get('temp')} with {result.get('humidity')} humidity."
    )


def _cancel(result: Dict[str, Any]) -> str:
    return f"Booking {result.get('booking_id')} is {result.get('status', 'cancelled')}; your refund is {result.get('refund', 'pending')}."


_SUMMARIZERS = {
    "search_flights": _flights,
    "search_hotels": _hotels,
    "book_flight": _book_flight,
    "book_hotel": _book_hotel,
    "check_weather": _weather,
    "cancel_booking": _cancel,
}


def summarize_tool_result(tool_name: str, result: Any) -> str:
    """One conversational sentence (or two) describing a successful tool result."""
    summarizer = _SUMMARIZERS.get(tool_name)
    if summarizer and isinstance(result, dict):
        try:
            return summarizer(result)
        except Exception:
            pass  # malformed result shape: fall through to the generic message
    label = tool_name.replace("_", " ")
    return f"Done — {label} completed."


def summarize_tool_error(tool_name: str, error: Optional[str]) -> str:
    label = tool_name.replace("_", " ")
    detail = f" ({error})" if error else ""
    return f"Sorry, I couldn't complete {label}{detail}. Want me to try again?"
