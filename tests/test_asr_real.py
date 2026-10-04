"""Tests that exercise the REAL faster-whisper engine on real speech fixtures (synthesized
with Piper TTS: "Book a flight from Delhi to Mumbai." / "Wait, actually make that Mumbai to Goa.").

Skipped automatically when the Whisper model can't be loaded (e.g. offline with no HF cache)."""

import asyncio
from pathlib import Path

import pytest

from agent.coordinator import AgentCoordinator
from agent.llm_client import MockLLMBackend
from agent.multimodal.asr import ASRProcessor
from agent.schemas.actions import ActionType
from agent.schemas.events import AudioChunkEvent

FIXTURES = Path(__file__).parent / "fixtures" / "audio"


@pytest.fixture(scope="module")
def asr():
    processor = ASRProcessor()
    processor._ensure_model_loaded()
    if not processor.is_loaded:
        pytest.skip("faster-whisper model unavailable (offline / not cached)")
    return processor


def _norm(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum() or c == " ").strip()


@pytest.mark.parametrize("name,fmt", [("book_flight.wav", "wav"), ("book_flight.webm", "webm")])
def test_transcribes_container_formats(asr, name, fmt):
    text = asr.transcribe_audio_bytes((FIXTURES / name).read_bytes(), fmt)
    assert _norm(text) == "book a flight from delhi to mumbai"


def test_transcribes_raw_pcm(asr):
    import subprocess

    pcm = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(FIXTURES / "book_flight.wav"), "-ar", "16000", "-ac", "1", "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    assert "delhi" in _norm(asr.transcribe_audio_bytes(pcm, "pcm_16khz"))


def test_domain_prompt_fixes_proper_noun(asr):
    """Without the domain prompt base.en hears 'Goa' as 'go away'."""
    text = asr.transcribe_audio_bytes((FIXTURES / "correction_goa.wav").read_bytes(), "wav")
    assert "goa" in _norm(text)
    assert "go away" not in _norm(text)


def test_silence_returns_empty(asr):
    assert asr.transcribe_audio_bytes((FIXTURES / "silence.pcm").read_bytes(), "pcm_16khz") == ""


def test_concurrent_transcription_is_thread_safe(asr):
    from concurrent.futures import ThreadPoolExecutor

    data = (FIXTURES / "book_flight.wav").read_bytes()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: asr.transcribe_audio_bytes(data, "wav"), range(4)))
    assert all("delhi" in _norm(r) for r in results)


def test_info_reports_loaded_state(asr):
    info = asr.info()
    assert info["loaded"] is True and info["load_failed"] is False


def test_unloadable_model_degrades_gracefully():
    broken = ASRProcessor(model_size="definitely-not-a-real-model-xyz", device="cpu")
    assert broken.transcribe_audio_bytes(b"\x00\x01" * 100, "pcm_16khz") == ""
    assert broken.info()["load_failed"] is True


@pytest.mark.asyncio
async def test_coordinator_emits_transcript_and_runs_pipeline_with_real_asr(asr):
    coordinator = AgentCoordinator(llm_backend=MockLLMBackend(), asr_processor=asr, enable_debounce=False)
    await coordinator.start()
    try:
        await coordinator.post_event(
            AudioChunkEvent(
                session_id="real_asr_sess",
                audio_bytes=(FIXTURES / "book_flight.webm").read_bytes(),
                format="webm",
                is_final=True,
            )
        )
        collected = []
        for _ in range(60):
            await asyncio.sleep(0.1)
            while not coordinator.action_queue.empty():
                collected.append(coordinator.action_queue.get_nowait())
            if any(a.action_type == ActionType.FILLER for a in collected):
                break

        transcripts = [a for a in collected if a.action_type == ActionType.TRANSCRIPT]
        assert len(transcripts) == 1
        transcript = transcripts[0]
        assert "delhi" in _norm(transcript.text)
        assert transcript.asr_model == asr.model_size
        assert transcript.latency_ms is not None

        # The transcript also flowed into the normal intent pipeline (user turn was planned),
        # and the client is told what was heard before the agent reacts to it.
        types = [a.action_type for a in collected]
        assert ActionType.FILLER in types
        assert types.index(ActionType.TRANSCRIPT) < types.index(ActionType.FILLER)
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_coordinator_emits_empty_transcript_for_silence(asr):
    coordinator = AgentCoordinator(llm_backend=MockLLMBackend(), asr_processor=asr, enable_debounce=False)
    await coordinator.start()
    try:
        await coordinator.post_event(
            AudioChunkEvent(
                session_id="silence_sess",
                audio_bytes=(FIXTURES / "silence.pcm").read_bytes(),
                format="pcm_16khz",
                is_final=True,
            )
        )
        await asyncio.sleep(0.5)
        actions = []
        while not coordinator.action_queue.empty():
            actions.append(coordinator.action_queue.get_nowait())
        transcripts = [a for a in actions if a.action_type == ActionType.TRANSCRIPT]
        assert len(transcripts) == 1 and transcripts[0].text == ""
        assert not any(a.action_type in (ActionType.FILLER, ActionType.TOOL_CALL) for a in actions)
    finally:
        await coordinator.stop()
