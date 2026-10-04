"""The scenario suite. Each scenario carries (a) a scripted LLM for deterministic `mock` runs that
exercise the coordination layer, and (b) alias-tolerant expectations so the SAME scenarios also score
a live LLM (`--llm live`), where argument extraction and correction handling are the real test."""

from __future__ import annotations

from typing import List

from agent.coordination.fault_injection import FaultInjectionConfig
from agent.eval.scenario import ExpectedCall, Scenario, Step
from agent.llm_client import LLMResponse

DEL, BOM, GOA = ("Delhi", "DEL"), ("Mumbai", "BOM"), ("Goa", "GOI", "GOX")


def tool(name: str, **args) -> LLMResponse:
    return LLMResponse(response_type="tool_call", tool_name=name, arguments=args)


def say(text: str) -> LLMResponse:
    return LLMResponse(response_type="spoken_response", content=text)


SUITE: List[Scenario] = [
    # ------------------------------------------------------------------ task completion
    Scenario(
        name="flight_search", tags=("task",),
        description="Single read-only request with two arguments.",
        steps=[Step(0, text="Find flights from Delhi to Mumbai")],
        expected_calls=[ExpectedCall("search_flights", {"origin": DEL, "destination": BOM})],
        expected_slots={"origin": DEL, "destination": BOM},
        latency_s={"search_flights": 1.0},
        mock_llm=[tool("search_flights", origin="Delhi", destination="Mumbai")],
    ),
    Scenario(
        name="hotel_search_nights", tags=("task",),
        description="Numeric argument extraction.",
        steps=[Step(0, text="Find a hotel in Goa for 3 nights")],
        expected_calls=[ExpectedCall("search_hotels", {"city": GOA, "nights": 3})],
        expected_slots={"city": GOA, "nights": 3},
        latency_s={"search_hotels": 1.0},
        mock_llm=[tool("search_hotels", city="Goa", nights=3)],
    ),
    Scenario(
        name="weather_query", tags=("task",),
        description="Simplest one-argument tool.",
        steps=[Step(0, text="What's the weather like in Paris?")],
        expected_calls=[ExpectedCall("check_weather", {"city": "Paris"})],
        expected_slots={"city": "Paris"},
        latency_s={"check_weather": 0.8},
        mock_llm=[tool("check_weather", city="Paris")],
    ),
    Scenario(
        name="book_flight", tags=("task", "safety"),
        description="A state-changing request must execute exactly once.",
        steps=[Step(0, text="Book the IndiGo flight from Delhi to Mumbai")],
        expected_calls=[ExpectedCall("book_flight", {"origin": DEL, "destination": BOM})],
        expected_slots={"origin": DEL, "destination": BOM},
        latency_s={"book_flight": 1.0},
        mock_llm=[tool("book_flight", origin="Delhi", destination="Mumbai", flight="6E-455")],
    ),
    Scenario(
        name="multi_turn_carry_over", tags=("task", "context"),
        description="'There' must resolve to the previous turn's destination.",
        steps=[Step(0, text="Find flights from Delhi to Mumbai"), Step(3.2, text="What's the weather like there?")],
        expected_calls=[ExpectedCall("search_flights", {"destination": BOM}), ExpectedCall("check_weather", {"city": BOM})],
        expected_slots={"origin": DEL, "destination": BOM, "city": BOM},
        latency_s={"search_flights": 1.0, "check_weather": 0.8},
        mock_llm=[tool("search_flights", origin="Delhi", destination="Mumbai"), tool("check_weather", city="Mumbai")],
    ),
    Scenario(
        name="flights_then_hotel", tags=("task", "context"),
        description="'There' + a count: hotels in the destination city of the previous request.",
        steps=[Step(0, text="Find flights from Delhi to Goa"), Step(3.2, text="Now find hotels there for 2 nights")],
        expected_calls=[ExpectedCall("search_flights", {"destination": GOA}), ExpectedCall("search_hotels", {"city": GOA, "nights": 2})],
        expected_slots={"destination": GOA, "city": GOA, "nights": 2},
        latency_s={"search_flights": 1.0, "search_hotels": 1.0},
        mock_llm=[tool("search_flights", origin="Delhi", destination="Goa"), tool("search_hotels", city="Goa", nights=2)],
    ),
    # ------------------------------------------------------------------- interruptions
    Scenario(
        name="correction_mid_search", tags=("interrupt",),
        description="Mid-call correction of a read-only call: cancel, re-plan, no stale value in the snapshot.",
        steps=[Step(0, text="Find flights from Delhi to Mumbai"),
               Step(0.8, text="Actually make the destination Goa instead", interrupts=True)],
        expected_calls=[ExpectedCall("search_flights", {"origin": DEL, "destination": GOA})],
        expected_slots={"origin": DEL, "destination": GOA},
        forbidden_slot_values={"destination": ["Mumbai", "BOM"]},
        latency_s={"search_flights": 2.5},
        mock_llm=[tool("search_flights", origin="Delhi", destination="Mumbai"),
                  tool("search_flights", origin="Delhi", destination="Goa")],
    ),
    Scenario(
        name="correction_mid_booking", tags=("interrupt", "safety"),
        description="Correcting a STATE-CHANGING call in flight: the stale booking must never complete.",
        steps=[Step(0, text="Book a flight from Delhi to Mumbai"),
               Step(0.8, text="No wait, change it to Goa", interrupts=True)],
        expected_calls=[ExpectedCall("book_flight", {"origin": DEL, "destination": GOA})],
        expected_slots={"destination": GOA},
        forbidden_slot_values={"destination": ["Mumbai", "BOM"]},
        latency_s={"book_flight": 2.5},
        mock_llm=[tool("book_flight", origin="Delhi", destination="Mumbai"),
                  tool("book_flight", origin="Delhi", destination="Goa")],
    ),
    Scenario(
        name="stop_command", tags=("interrupt",),
        description="'Stop' with no replacement: cancel everything, start nothing.",
        steps=[Step(0, text="Find hotels in Goa"), Step(0.8, text="Stop, cancel that", interrupts=True)],
        forbid_completed=["search_hotels"],
        latency_s={"search_hotels": 2.5},
        mock_llm=[tool("search_hotels", city="Goa"), say("Okay, I've stopped that.")],
    ),
    Scenario(
        name="rapid_fire_corrections", tags=("interrupt", "safety"),
        description="Three corrections within 300ms: only the last destination may be booked, once.",
        steps=[Step(0, text="Book a flight from Delhi to New York"),
               Step(0.15, text="Actually make that Boston", interrupts=True),
               Step(0.30, text="No sorry, Chicago", interrupts=True)],
        expected_calls=[ExpectedCall("book_flight", {"origin": DEL, "destination": ("Chicago", "ORD")})],
        expected_slots={"destination": ("Chicago", "ORD")},
        forbidden_slot_values={"destination": ["New York", "Boston", "JFK", "BOS"]},
        latency_s={"book_flight": 2.5},
        mock_llm=[tool("book_flight", origin="Delhi", destination="New York"),
                  tool("book_flight", origin="Delhi", destination="Boston"),
                  tool("book_flight", origin="Delhi", destination="Chicago")],
    ),
    Scenario(
        name="explicit_interrupt_signal", tags=("interrupt",),
        description="A raw interrupt event (no new text) cancels in-flight work and updates the snapshot.",
        steps=[Step(0, text="Find flights from Delhi to Mumbai"), Step(0.8, kind="interrupt", interrupts=True, is_turn=False)],
        forbid_completed=["search_flights"],
        latency_s={"search_flights": 2.5},
        mock_llm=[tool("search_flights", origin="Delhi", destination="Mumbai")],
    ),
    # ------------------------------------------------------------------------- safety
    Scenario(
        name="duplicate_booking_race", tags=("safety",),
        description="The same booking requested twice while the first is in flight: exactly one executes.",
        steps=[Step(0, text="Book a flight from Delhi to Mumbai"), Step(0.5, text="Book a flight from Delhi to Mumbai")],
        expected_calls=[ExpectedCall("book_flight", {"origin": DEL, "destination": BOM})],
        latency_s={"book_flight": 2.5},
        mock_llm=[tool("book_flight", origin="Delhi", destination="Mumbai"),
                  tool("book_flight", origin="Delhi", destination="Mumbai")],
    ),
    Scenario(
        name="duplicate_booking_paraphrase", tags=("safety",),
        description="The same booking phrased two ways (airport codes vs city names) must still execute once.",
        steps=[Step(0, text="Book a flight from Delhi to Mumbai"), Step(0.5, text="Please book me a flight from Delhi to Mumbai")],
        expected_calls=[ExpectedCall("book_flight", {"origin": DEL, "destination": BOM})],
        latency_s={"book_flight": 2.5},
        mock_llm=[tool("book_flight", origin="DEL", destination="BOM"),
                  tool("book_flight", origin="Delhi", destination="Mumbai")],
    ),
    Scenario(
        name="tool_failure_reported", tags=("fault",),
        description="A tool that errors must be reported to the user, not swallowed, and not retried blindly.",
        steps=[Step(0, text="Find flights from Delhi to Mumbai")],
        faults={"search_flights": FaultInjectionConfig(failure_rate=1.0)},
        expect_failure_notice=True,
        latency_s={"search_flights": 0.5},
        mock_llm=[tool("search_flights", origin="Delhi", destination="Mumbai")],
    ),
    # --------------------------------------------------------------------------- voice
    Scenario(
        name="voice_task", tags=("task", "voice"),
        description="Spoken request end to end (streaming VAD + Whisper + planner).",
        steps=[Step(0, kind="voice", audio="book_flight.wav")],
        expected_calls=[ExpectedCall(("book_flight", "search_flights"), {"origin": DEL, "destination": BOM})],
        expected_slots={"origin": DEL, "destination": BOM},
        latency_s={"book_flight": 1.0, "search_flights": 1.0},
        mock_llm=[tool("book_flight", origin="Delhi", destination="Mumbai")],
        max_s=30,
    ),
    Scenario(
        name="voice_barge_in", tags=("interrupt", "voice"),
        description="Interrupt a running search BY VOICE, mid-sentence; measures cancel latency from speech onset.",
        steps=[Step(0, text="Find flights from Delhi to Mumbai"),
               Step(1.0, kind="voice", audio="correction_goa.wav", interrupts=True)],
        expected_calls=[ExpectedCall("search_flights", {"destination": GOA})],
        expected_slots={"destination": GOA},
        forbidden_slot_values={"destination": ["Mumbai", "BOM"]},
        latency_s={"search_flights": 4.0},
        mock_llm=[tool("search_flights", origin="Delhi", destination="Mumbai"),
                  tool("search_flights", origin="Mumbai", destination="Goa")],
        max_s=40,
    ),
    # -------------------------------------------------------------------------- spoken output
    Scenario(
        name="speak_barge_in", tags=("interrupt", "speech"), speak=True,
        description="Interrupt while the agent is TALKING (not while a tool runs): the voice must stop at once, the "
                    "cut-off reply be recorded as truncated, and the correction still be carried out.",
        steps=[Step(0, text="Find flights from Delhi to Mumbai"),
               Step(3.2, text="No wait, make it Goa instead", interrupts=True)],
        expected_calls=[ExpectedCall("search_flights", {"origin": DEL, "destination": GOA})],
        expected_slots={"origin": DEL, "destination": GOA},
        latency_s={"search_flights": 1.0},
        mock_llm=[tool("search_flights", origin="Delhi", destination="Mumbai"),
                  tool("search_flights", origin="Delhi", destination="Goa")],
        max_s=40,
    ),
    Scenario(
        name="speak_plain", tags=("task", "speech"), speak=True,
        description="Spoken replies on, nobody interrupts: the whole reply is spoken and the turn still completes.",
        steps=[Step(0, text="What's the weather like in Paris?")],
        expected_calls=[ExpectedCall("check_weather", {"city": "Paris"})],
        expected_slots={"city": "Paris"},
        latency_s={"check_weather": 0.8},
        mock_llm=[tool("check_weather", city="Paris")],
        max_s=40,
    ),
    # ------------------------------------------------------------------------- vision
    Scenario(
        name="vision_extract_arg", tags=("task", "vision"),
        description="A value that exists only in the shared image (a poster reading GOA) must become a tool argument.",
        steps=[Step(0, kind="frame", frame_text="GOA", is_turn=False),
               Step(0.3, text="Find flights from Delhi to the city on this poster")],
        expected_calls=[ExpectedCall("analyze_frame"),
                        ExpectedCall("search_flights", {"origin": DEL, "destination": GOA})],
        expected_slots={"origin": DEL, "destination": GOA},
        latency_s={"search_flights": 1.0},
        mock_llm=[tool("analyze_frame", question="What city is written on the poster?"),
                  tool("search_flights", origin="Delhi", destination="Goa")],
        mock_vision=["The poster reads: GOA"],
        max_s=60,
    ),
    Scenario(
        name="vision_no_frame", tags=("task", "vision"),
        description="Asked about the screen with nothing shared: say so honestly instead of inventing an answer.",
        steps=[Step(0, text="What's on my screen right now?")],
        expected_calls=[ExpectedCall("analyze_frame")],
        mock_llm=[tool("analyze_frame", question="What is on the user's screen?"),
                  say("I can't see anything yet -- please share your screen or camera and ask again.")],
        max_s=40,
    ),
    Scenario(
        name="vision_interrupt", tags=("interrupt", "vision"),
        description="Cancel a slow in-flight vision call: the observation must not complete or re-plan.",
        steps=[Step(0, kind="frame", frame_text="GOA", is_turn=False),
               Step(0.2, text="What city is written on this poster?"),
               Step(2.0, text="Stop, never mind that", interrupts=True)],
        forbid_completed=["analyze_frame"],
        mock_llm=[tool("analyze_frame", question="What city is written on the poster?"), say("Okay, I've stopped that.")],
        mock_vision=["The poster reads: GOA"], vision_latency_s=3.5,
        max_s=60,
    ),
    Scenario(
        name="vision_irrelevant_frame", tags=("task", "vision"),
        description="A frame is being shared but the question is not about it: do not spend a vision call.",
        steps=[Step(0, kind="frame", frame_text="GOA", is_turn=False), Step(0.3, text="What's the weather like in Paris?")],
        expected_calls=[ExpectedCall("check_weather", {"city": "Paris"})],
        forbid_completed=["analyze_frame"],
        expected_slots={"city": "Paris"},
        latency_s={"check_weather": 0.8},
        mock_llm=[tool("check_weather", city="Paris")],
    ),
]

BY_NAME = {s.name: s for s in SUITE}
