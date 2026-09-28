"""Unit and integration tests for raw audio ingestion and ASR processing (§3, §4)."""

import asyncio
import numpy as np
import pytest
from agent.coordinator import AgentCoordinator
from agent.coordination.tool_router import ToolRouter
from agent.multimodal.asr import ASRProcessor
from agent.schemas.events import AudioChunkEvent, InterruptSignalEvent
from agent.schemas.actions import ActionType, FillerAction, ToolCancelAction
from agent.llm_client import MockLLMBackend


class MockASR(ASRProcessor):
    def __init__(self, canned_text: str = "book a flight to Seattle"):
        super().__init__()
        self.canned_text = canned_text
        self.transcribe_called = False

    def transcribe_audio_bytes(self, audio_bytes: bytes, format: str = "pcm_16khz") -> str:
        self.transcribe_called = True
        return self.canned_text


def test_asr_empty_bytes():
    asr = ASRProcessor()
    assert asr.transcribe_audio_bytes(b"") == ""


@pytest.mark.asyncio
async def test_coordinator_audio_chunk_ingestion():
    """Verify that incoming AudioChunkEvent is transcribed and fed into the intent loop."""
    mock_asr = MockASR(canned_text="search flights to London")
    router = ToolRouter()
    coordinator = AgentCoordinator(
        tool_router=router,
        llm_backend=MockLLMBackend(),
        asr_processor=mock_asr,
        enable_debounce=False,  # immediate execution for test
    )
    await coordinator.start()
    session_id = "test_audio_sess_1"

    try:
        # Create 1 second of dummy PCM (16kHz 16-bit mono = 32000 bytes)
        dummy_pcm = b"\x00" * 32000

        await coordinator.post_event(
            AudioChunkEvent(
                session_id=session_id,
                audio_bytes=dummy_pcm,
                format="pcm_16khz",
                is_final=True,
            )
        )

        # Allow async task to process
        await asyncio.sleep(0.1)

        # Ensure ASR was called
        assert mock_asr.transcribe_called is True

        # Collect emitted actions
        actions = []
        while not coordinator.action_queue.empty():
            actions.append(await coordinator.get_next_action())

        # Should have emitted a fast-path filler for the transcribed text
        fillers = [a for a in actions if a.action_type == ActionType.FILLER]
        assert len(fillers) >= 1
        assert isinstance(fillers[0], FillerAction)

        # State snapshot should also have been emitted
        snapshots = [a for a in actions if a.action_type == ActionType.STATE_SNAPSHOT]
        assert len(snapshots) >= 1

    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_audio_buffering_until_final():
    """Verify that chunks accumulate in session audio buffer until is_final is True."""
    mock_asr = MockASR(canned_text="cancel reservation")
    coordinator = AgentCoordinator(
        llm_backend=MockLLMBackend(),
        asr_processor=mock_asr,
        enable_debounce=False,
    )
    await coordinator.start()
    session_id = "test_audio_buf_sess"

    try:
        chunk = b"\x01\x00" * 100  # 200 bytes

        # Send chunk 1 with is_final=False (< threshold)
        await coordinator.post_event(
            AudioChunkEvent(
                session_id=session_id,
                audio_bytes=chunk,
                is_final=False,
            )
        )
        await asyncio.sleep(0.05)
        # Should not have transcribed yet
        assert mock_asr.transcribe_called is False
        assert len(coordinator._audio_buffers[session_id]) == 200

        # Send chunk 2 with is_final=True -> triggers transcription
        await coordinator.post_event(
            AudioChunkEvent(
                session_id=session_id,
                audio_bytes=chunk,
                is_final=True,
            )
        )
        await asyncio.sleep(0.05)
        assert mock_asr.transcribe_called is True
        assert len(coordinator._audio_buffers[session_id]) == 0

    finally:
        await coordinator.stop()
