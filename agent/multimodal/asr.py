"""ASR module for raw audio processing using faster-whisper (§3, §4).

Handles audio-only scenarios (30% of suite).
Features:
- Direct PCM 16kHz float32 zero-copy ingestion
- Container formats (WAV, WebM, MP3) supported via PyAV
- Silero VAD filtering to discard background silence
- Lazy-loading and pre-warmed inference (§7.7)
"""

from __future__ import annotations

import io
import logging
from typing import Optional, Union
import numpy as np

logger = logging.getLogger("agent.asr")


class ASRProcessor:
    def __init__(
        self,
        model_size: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
        enable_vad: bool = True,
    ):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.enable_vad = enable_vad
        self._model = None

    def _ensure_model_loaded(self) -> None:
        """Lazy load the Whisper model on first invocation."""
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
                logger.info("Loading faster-whisper model (%s) on %s (%s)...", self.model_size, self.device, self.compute_type)
                self._model = WhisperModel(
                    self.model_size,
                    device=self.device,
                    compute_type=self.compute_type,
                )
            except Exception as e:
                logger.warning("Could not load faster-whisper: %s. Audio will return empty/mock transcription.", e)

    def transcribe_audio_bytes(self, audio_bytes: bytes, format: str = "pcm_16khz") -> str:
        """Transcribe raw audio bytes into text string.
        
        Args:
            audio_bytes: Raw binary audio data.
            format: "pcm_16khz" (raw 16-bit PCM) or container format ("wav", "webm", etc.)
        """
        if not audio_bytes:
            return ""

        self._ensure_model_loaded()
        if self._model is None:
            return ""

        try:
            if format.lower() == "pcm_16khz":
                # Ensure even byte count for 16-bit PCM
                if len(audio_bytes) % 2 != 0:
                    audio_bytes = audio_bytes[: len(audio_bytes) - 1]
                if len(audio_bytes) == 0:
                    return ""
                
                # Convert 16-bit integer PCM to normalized float32
                audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                audio_input = audio_np
            else:
                # Compressed/Container format (WAV, WebM, OGG)
                audio_input = io.BytesIO(audio_bytes)

            segments, _ = self._model.transcribe(
                audio_input,
                beam_size=1,
                vad_filter=self.enable_vad,
                vad_parameters=dict(min_silence_duration_ms=400) if self.enable_vad else None,
            )
            text_segments = [s.text.strip() for s in segments]
            return " ".join(t for t in text_segments if t).strip()

        except Exception as e:
            logger.error("Error during audio transcription: %s", e)
            return ""

    def warmup(self) -> None:
        """Pre-warm model with dummy audio to eliminate first-call latency penalty (§7.7)."""
        try:
            self._ensure_model_loaded()
            if self._model is not None:
                # 0.5s of silence (16kHz 16-bit PCM = 16000 bytes)
                dummy_pcm = b"\x00" * 16000
                self.transcribe_audio_bytes(dummy_pcm, format="pcm_16khz")
                logger.info("ASR warm-up completed successfully.")
        except Exception as e:
            logger.warning("ASR warm-up notice: %s", e)
