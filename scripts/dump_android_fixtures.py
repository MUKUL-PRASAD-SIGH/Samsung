"""Regenerate the protocol fixtures the Android app's unit tests parse.

    python scripts/dump_android_fixtures.py          # rewrite android/app/src/test/resources/server_messages.jsonl
    python scripts/dump_android_fixtures.py --check  # exit 1 if the checked-in file is stale (CI / tests/test_android_fixtures.py)

The messages are produced by the REAL pydantic action classes, so when the server's schema changes the Android tests fail
until the Kotlin models are updated -- instead of the app silently misreading a field in production.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from agent.schemas.actions import (
    AgentStepAction, AudioOutAction, ClarificationAction, FillerAction, GraphEdgePayload, GraphNodePayload, GraphUpdateAction,
    FileExportedAction, InFlightCallInfo, SpeechStateAction, SpokenResponseAction, StateSnapshotAction, ToolCallAction, ToolCancelAction,
    TranscriptAction, VoiceActivityAction,
)

OUT = Path(__file__).resolve().parents[1] / "android/app/src/test/resources/server_messages.jsonl"
SID = "android_demo"


def messages() -> list:
    t = 1_760_000_000.0                       # fixed, so the file is deterministic
    pcm = base64.b64encode(b"\x01\x00\xff\xff" * 40).decode()
    acts = [
        FillerAction(session_id=SID, epoch=1, text="Looking that up right now...", timestamp=t),
        SpokenResponseAction(session_id=SID, epoch=1, text="I found **2 flights** from DEL to BOM.\n- cheapest: IndiGo", timestamp=t),
        ClarificationAction(session_id=SID, epoch=2, question="Do you want me to change what I'm working on?", timestamp=t),
        ToolCallAction(session_id=SID, epoch=1, call_id="call_1", tool_name="search_flights",
                       arguments={"origin": "Delhi", "destination": "Mumbai", "nights": 2}, is_state_modifying=False, timestamp=t),
        ToolCancelAction(session_id=SID, epoch=2, call_id="call_1", tool_name="search_flights", reason="user_correction", timestamp=t),
        StateSnapshotAction(session_id=SID, epoch=2, intent="search_flights", slots={"origin": "Delhi", "destination": "Goa", "nights": 3},
                            in_flight_calls=[InFlightCallInfo(call_id="call_2", tool="search_flights", epoch=2, status="running")],
                            last_updated="2026-10-04T12:00:00+00:00", timestamp=t),
        AgentStepAction(session_id=SID, epoch=1, call_id="call_9", name="vector_craft", role="SVG Artist", step=3, total_steps=3,
                        thought="Done.", status="completed",
                        artifact={"title": "logo.svg", "language": "svg", "content": "<svg/>"}, timestamp=t),
        GraphUpdateAction(session_id=SID, epoch=1, op="append",
                          nodes=[GraphNodePayload(id="turn_1", node_type="turn", label="find flights", data={"x": 1}),
                                 GraphNodePayload(id="ent_1", node_type="entity", label="destination: Goa")],
                          edges=[GraphEdgePayload(source="turn_1", target="ent_1", edge_type="REFERENCES")], timestamp=t),
        TranscriptAction(session_id=SID, epoch=1, text="No wait", asr_model="base.en", latency_ms=312.5, is_partial=True,
                         utterance_id="utt_1", timestamp=t),
        TranscriptAction(session_id=SID, epoch=1, text="No wait, make it Goa.", asr_model="base.en", latency_ms=290.0,
                         is_partial=False, utterance_id="utt_1", timestamp=t),
        VoiceActivityAction(session_id=SID, epoch=1, state="speech_start", utterance_id="utt_1", timestamp=t),
        VoiceActivityAction(session_id=SID, epoch=2, state="barge_in", utterance_id="utt_1", detail="No wait", timestamp=t),
        AudioOutAction(session_id=SID, epoch=2, utterance_id="tts_1", seq=0, text="Sure.", sample_rate=22050, duration_ms=1.8,
                       audio_b64=pcm, is_last=True, timestamp=t),
        SpeechStateAction(session_id=SID, epoch=2, state="ducked", utterance_id="tts_1", reason="user_speech_start", timestamp=t),
        SpeechStateAction(session_id=SID, epoch=2, state="stopped", utterance_id="tts_1", reason="user_spoke",
                          text="I found two flights. The cheapest is IndiGo.", spoken_text="I found two", spoken_ms=900.0, timestamp=t),
    ]
    for i, a in enumerate(acts):
        a.action_id = f"action_{i:02d}"          # random uuids would make the file differ on every run
    acts.append(FileExportedAction(session_id=SID, epoch=2, call_id="call_7", filename="reverse.py", path="/home/me/kairos-exports/reverse.py",
                                   bytes=52, language="python", editor_uri="vscode://file/home/me/kairos-exports/reverse.py",
                                   download_path="/exports/reverse.py", opened_with="code", preview="def reverse(s):\n    return s[::-1]\n", timestamp=t))
    for i, a in enumerate(acts):
        a.action_id = f"action_{i:02d}"
    lines = [json.loads(a.model_dump_json()) for a in acts]
    lines += [{"type": "tts_status", "enabled": True, "available": True}, {"type": "error", "code": "rate_limit"}]
    return lines


def render() -> str:
    return "\n".join(json.dumps(m, sort_keys=True) for m in messages()) + "\n"


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        ok = OUT.exists() and OUT.read_text() == text
        print("up to date" if ok else f"STALE: run python scripts/dump_android_fixtures.py ({OUT})")
        return 0 if ok else 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    print(f"wrote {len(text.splitlines())} messages to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
