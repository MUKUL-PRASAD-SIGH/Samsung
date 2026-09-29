"""Continuous voice streaming: segmentation logic (scripted VAD) and real-audio behavior."""

import asyncio
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from agent.coordination.tool_router import ToolRouter
from agent.coordinator import AgentCoordinator
from agent.llm_client import MockLLMBackend
from agent.multimodal.asr import ASRProcessor
from agent.multimodal.streaming import (
    FRAME_BYTES, FRAME_MS, PartialDue, SpeechStart, UtteranceEnd, VoiceConfig, VoiceStream,
)
from agent.schemas.actions import ActionType, ToolCancelAction, TranscriptAction, VoiceActivityAction
from agent.schemas.events import AudioChunkEvent

FIXTURES = Path(__file__).parent / "fixtures" / "audio"


# =========================================================== segmentation (scripted VAD)
class ScriptedVAD:
    """Returns one scripted probability per frame ('S' speech, 'm' in-between dip, '.' silence)."""

    PROBS = {"S": 0.95, "m": 0.42, ".": 0.02}

    def __init__(self, pattern: str):
        self.probs = [self.PROBS[c] for c in pattern]
        self.i = 0

    def reset(self):
        pass

    def prob(self, frame):
        p = self.probs[self.i] if self.i < len(self.probs) else 0.02
        self.i += 1
        return p


def _frames(n, marker=0):
    return (np.full(512, marker, dtype=np.int16)).tobytes() * n


def _stream(pattern, **cfg):
    config = VoiceConfig(**{"min_speech_ms": 128, "endpoint_ms": 320, "preroll_ms": 96,
                            "partial_interval_ms": 320, "min_partial_ms": 192, **cfg})
    return VoiceStream(vad=ScriptedVAD(pattern), config=config), len(pattern)


def _run(pattern, **cfg):
    vs, n = _stream(pattern, **cfg)
    return vs, vs.feed(_frames(n))


def test_utterance_starts_after_min_speech_and_ends_after_endpoint():
    _, events = _run("..." + "S" * 12 + "." * 12)
    kinds = [type(e).__name__ for e in events]
    assert kinds[0] == "SpeechStart" and kinds[-1] == "UtteranceEnd"
    end = events[-1]
    assert end.reason == "endpoint" and end.speech_ms >= 12 * FRAME_MS


def test_short_blip_is_ignored():
    _, events = _run("..SS....." + "." * 20)  # 2 speech frames < min_speech (4)
    assert events == []


def test_hysteresis_keeps_utterance_alive_through_mid_probability_dips():
    _, events = _run("." + "S" * 6 + "m" * 30 + "S" * 6 + "." * 12)
    ends = [e for e in events if isinstance(e, UtteranceEnd)]
    assert len(ends) == 1  # the 'm' (0.42) frames sit between the thresholds and don't endpoint


def test_preroll_is_included_and_trailing_silence_trimmed():
    vs, n = _stream("." * 8 + "S" * 8 + "." * 14, preroll_ms=96)
    pcm = b"".join(_frames(1, marker=i + 1) for i in range(n))  # frame i carries marker i+1
    events = vs.feed(pcm)
    end = next(e for e in events if isinstance(e, UtteranceEnd))
    markers = [np.frombuffer(end.pcm[i:i + FRAME_BYTES], dtype=np.int16)[0] for i in range(0, len(end.pcm), FRAME_BYTES)]
    # onset detected at frame index 11 (8 silence + 4th speech frame); preroll = 3 frames + 4 onset frames
    assert markers[0] == 8 + 1 - 3           # first kept frame is 3 frames before speech began
    assert len(markers) <= 3 + 8 + 4 + 1     # lead-in + speech + <=4 tail frames, not the full 10 silent frames


def test_chunking_does_not_change_results():
    pattern = "..." + "S" * 10 + "." * 12 + "S" * 8 + "." * 12
    vs_a, n = _stream(pattern)
    whole = vs_a.feed(_frames(n))
    vs_b, _ = _stream(pattern)
    audio = _frames(n)
    pieces = []
    for i in range(0, len(audio), 37):     # awkward odd-sized chunks, incl. splitting mid-sample
        pieces += vs_b.feed(audio[i:i + 37])
    assert [type(e).__name__ for e in whole] == [type(e).__name__ for e in pieces]
    assert [len(getattr(e, "pcm", b"")) for e in whole] == [len(getattr(e, "pcm", b"")) for e in pieces]


def test_partials_fire_on_cadence_only_while_speaking():
    _, events = _run("." * 4 + "S" * 40 + "." * 14)
    partials = [e for e in events if isinstance(e, PartialDue)]
    assert len(partials) >= 3
    sizes = [len(p.pcm) for p in partials]
    assert sizes == sorted(sizes) and len(set(sizes)) == len(sizes)   # each snapshot is longer
    starts = [i for i, e in enumerate(events) if isinstance(e, SpeechStart)]
    ends = [i for i, e in enumerate(events) if isinstance(e, UtteranceEnd)]
    assert all(starts[0] < events.index(p) < ends[0] for p in partials)


def test_max_length_forces_an_endpoint_and_speech_continues_as_new_utterance():
    _, events = _run("S" * 80, max_utterance_s=1.0)
    ends = [e for e in events if isinstance(e, UtteranceEnd)]
    assert ends and ends[0].reason == "max_length"
    assert len([e for e in events if isinstance(e, SpeechStart)]) >= 2


def test_flush_closes_an_open_utterance():
    vs, n = _stream("." * 3 + "S" * 10)
    events = vs.feed(_frames(n))
    assert not any(isinstance(e, UtteranceEnd) for e in events)
    flushed = vs.flush()
    assert len(flushed) == 1 and flushed[0].reason == "flush"
    assert vs.flush() == []


def test_sequential_utterances_get_increasing_ids():
    _, events = _run("S" * 8 + "." * 12 + "S" * 8 + "." * 12)
    ids = [e.utterance_id for e in events if isinstance(e, SpeechStart)]
    assert ids == ["utt_1", "utt_2"]


# ====================================================================== real audio helpers
def _pcm(name, lead_s=0.5, tail_s=1.5):
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(FIXTURES / name), "-ar", "16000", "-ac", "1", "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    return b"\x00\x00" * int(16000 * lead_s) + raw + b"\x00\x00" * int(16000 * tail_s)


def _norm(t):
    return "".join(c for c in t.lower() if c.isalnum() or c == " ").strip()


@pytest.fixture(scope="module")
def asr():
    p = ASRProcessor()
    p._ensure_model_loaded()
    if not p.is_loaded:
        pytest.skip("faster-whisper model unavailable (offline / not cached)")
    return p


def _real_stream():
    try:
        return VoiceStream()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Silero VAD unavailable: {e}")


def test_real_vad_segments_and_whisper_transcribes_a_single_utterance(asr):
    vs = _real_stream()
    audio = _pcm("book_flight.wav")
    events = []
    for i in range(0, len(audio), 3200):                 # 100ms chunks, like the browser
        events += vs.feed(audio[i:i + 3200])
    kinds = [type(e).__name__ for e in events]
    assert kinds[0] == "SpeechStart" and kinds[-1] == "UtteranceEnd" and "PartialDue" in kinds
    end = events[-1]
    assert _norm(asr.transcribe_audio_bytes(end.pcm, "pcm_16khz")) == "book a flight from delhi to mumbai"


def test_real_vad_splits_two_utterances_separated_by_a_pause(asr):
    vs = _real_stream()
    audio = _pcm("book_flight.wav", tail_s=1.2) + _pcm("correction_goa.wav", lead_s=0.0)
    events = []
    for i in range(0, len(audio), 3200):
        events += vs.feed(audio[i:i + 3200])
    events += vs.flush()
    ends = [e for e in events if isinstance(e, UtteranceEnd)]
    assert len(ends) == 2 and ends[0].utterance_id != ends[1].utterance_id
    assert "delhi" in _norm(asr.transcribe_audio_bytes(ends[0].pcm, "pcm_16khz"))
    assert "goa" in _norm(asr.transcribe_audio_bytes(ends[1].pcm, "pcm_16khz"))


def test_real_vad_ignores_silence_and_low_noise():
    vs = _real_stream()
    noise = (np.random.default_rng(0).normal(0, 0.004, 16000 * 4) * 32768).astype(np.int16).tobytes()
    events = []
    for i in range(0, len(noise), 3200):
        events += vs.feed(noise[i:i + 3200])
    assert events == []


# ===================================================== coordinator: streaming + barge-in
async def _stream_audio(coordinator, sid, audio, pace=1.0, chunk=3200):
    await coordinator.post_event(AudioChunkEvent(session_id=sid, streaming=True, stream_control="start"))
    for i in range(0, len(audio), chunk):
        await coordinator.post_event(AudioChunkEvent(session_id=sid, audio_bytes=audio[i:i + chunk], format="pcm_16khz", streaming=True))
        await asyncio.sleep(0.1 * pace)


async def _drain(coordinator, out, settle=0.0):
    if settle:
        await asyncio.sleep(settle)
    while not coordinator.action_queue.empty():
        out.append(coordinator.action_queue.get_nowait())


def _slow_router(duration=6.0):
    router = ToolRouter(register_defaults=False)

    async def slow(city: str = "x", **kw):
        await asyncio.sleep(duration)
        return {"city": city, "condition": "Sunny", "temp": "1", "humidity": "1"}

    router.register_tool(name="check_weather", description="t",
                         parameters_schema={"type": "object", "properties": {"city": {"type": "string"}}},
                         is_state_modifying=False, handler=slow)
    return router


@pytest.mark.asyncio
async def test_streaming_emits_activity_partials_and_a_final_that_reaches_the_planner(asr):
    coordinator = AgentCoordinator(llm_backend=MockLLMBackend(), asr_processor=asr, enable_debounce=False)
    await coordinator.start()
    _real_stream()
    try:
        actions = []
        await _stream_audio(coordinator, "vs1", _pcm("book_flight.wav"), pace=0.5)
        for _ in range(40):
            await _drain(coordinator, actions, 0.1)
            if any(a.action_type == ActionType.FILLER for a in actions):
                break
        types = [a.action_type for a in actions]
        states = [a.state for a in actions if isinstance(a, VoiceActivityAction)]
        assert states[0] == "listening" and "speech_start" in states and "speech_end" in states

        transcripts = [a for a in actions if isinstance(a, TranscriptAction)]
        partials = [t for t in transcripts if t.is_partial]
        finals = [t for t in transcripts if not t.is_partial]
        assert partials and len(finals) == 1
        assert all(t.utterance_id == finals[0].utterance_id for t in transcripts)
        assert _norm(finals[0].text) == "book a flight from delhi to mumbai"
        # ordering: partials refine the utterance before its final, and the agent reacts after the final
        assert types.index(ActionType.TRANSCRIPT) < types.index(ActionType.FILLER)
        assert max(actions.index(p) for p in partials) < actions.index(finals[0])
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_voice_barge_in_cancels_running_work_mid_sentence_and_bumps_epoch_once(asr):
    coordinator = AgentCoordinator(tool_router=_slow_router(), llm_backend=MockLLMBackend(), asr_processor=asr, enable_debounce=False)
    await coordinator.start()
    _real_stream()
    try:
        await coordinator.dispatch_tool_call("bi1", "check_weather", {"city": "Mumbai"}, "call_slow")
        session = coordinator.sessions["bi1"]
        assert session.epoch == 1

        actions = []
        # "Wait, actually make that Mumbai to Goa." streamed at real-time pace
        streaming = asyncio.create_task(_stream_audio(coordinator, "bi1", _pcm("correction_goa.wav")))
        while not streaming.done():
            await _drain(coordinator, actions, 0.05)
        for _ in range(60):
            await _drain(coordinator, actions, 0.1)
            if any(a.action_type == ActionType.FILLER for a in actions):
                break

        cancel = next((a for a in actions if isinstance(a, ToolCancelAction)), None)
        speech_end = next((a for a in actions if isinstance(a, VoiceActivityAction) and a.state == "speech_end"), None)
        barge_in = next((a for a in actions if isinstance(a, VoiceActivityAction) and a.state == "barge_in"), None)
        assert cancel is not None, "voice barge-in never cancelled the running tool call"
        assert barge_in is not None, "no barge_in voice-activity event was emitted"
        assert speech_end is not None

        # the interrupt fired from a PARTIAL transcript, i.e. while the user was still talking
        assert cancel.call_id == "call_slow"
        assert barge_in.timestamp < speech_end.timestamp
        assert cancel.timestamp < speech_end.timestamp
        lead_s = speech_end.timestamp - cancel.timestamp
        print(f"\n[voice barge-in] work cancelled {lead_s:.2f}s before the utterance ended")
        assert lead_s > 0.5

        final = [a for a in actions if isinstance(a, TranscriptAction) and not a.is_partial][-1]
        assert "goa" in _norm(final.text) and "wait" in _norm(final.text)
        # the final transcript must not bump the epoch a second time
        assert session.epoch == 2
        assert sum(isinstance(a, ToolCancelAction) for a in actions) == 1
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_non_interrupting_speech_does_not_cancel_running_work(asr):
    coordinator = AgentCoordinator(tool_router=_slow_router(), llm_backend=MockLLMBackend(), asr_processor=asr, enable_debounce=False)
    await coordinator.start()
    _real_stream()
    try:
        await coordinator.dispatch_tool_call("bi2", "check_weather", {"city": "Mumbai"}, "call_slow")
        session = coordinator.sessions["bi2"]
        actions = []
        streaming = asyncio.create_task(_stream_audio(coordinator, "bi2", _pcm("book_flight.wav"), pace=0.5))
        while not streaming.done():
            await _drain(coordinator, actions, 0.05)
        for _ in range(60):
            await _drain(coordinator, actions, 0.1)
            if any(a.action_type == ActionType.FILLER for a in actions):
                break
        assert not any(isinstance(a, ToolCancelAction) for a in actions)
        assert not any(isinstance(a, VoiceActivityAction) and a.state == "barge_in" for a in actions)
        assert session.epoch == 1
        assert session.in_flight_calls["call_slow"].status in ("pending", "running")
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_stop_flushes_an_utterance_still_in_progress(asr):
    coordinator = AgentCoordinator(llm_backend=MockLLMBackend(), asr_processor=asr, enable_debounce=False)
    await coordinator.start()
    _real_stream()
    try:
        audio = _pcm("book_flight.wav", tail_s=0.0)        # speech ends the stream: no trailing pause
        await _stream_audio(coordinator, "fl1", audio, pace=0.3)
        await coordinator.post_event(AudioChunkEvent(session_id="fl1", streaming=True, stream_control="stop"))
        actions = []
        for _ in range(50):
            await _drain(coordinator, actions, 0.1)
            if any(isinstance(a, TranscriptAction) and not a.is_partial for a in actions):
                break
        finals = [a for a in actions if isinstance(a, TranscriptAction) and not a.is_partial]
        assert len(finals) == 1 and "delhi" in _norm(finals[0].text)
        assert [a.state for a in actions if isinstance(a, VoiceActivityAction)][-1] in ("idle", "speech_end")
        assert "fl1" not in coordinator._voice
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_audio_without_a_start_control_is_ignored(asr):
    coordinator = AgentCoordinator(llm_backend=MockLLMBackend(), asr_processor=asr, enable_debounce=False)
    await coordinator.start()
    try:
        await coordinator.post_event(AudioChunkEvent(session_id="ns1", audio_bytes=_pcm("book_flight.wav"), format="pcm_16khz", streaming=True))
        actions = []
        await _drain(coordinator, actions, 0.3)
        assert actions == []
    finally:
        await coordinator.stop()
