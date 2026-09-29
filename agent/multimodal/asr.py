"""ASR module for raw audio processing using faster-whisper (§3, §4).

Handles audio-only scenarios (30% of suite).
Features:
- Direct PCM 16kHz float32 zero-copy ingestion
- Container formats (WAV, WebM, MP3) supported via PyAV
- Silero VAD filtering to discard background silence
- Domain vocabulary biasing via initial_prompt (fixes e.g. "Goa" -> "go away")
- Lazy-loading and pre-warmed inference (§7.7)
- Env-configurable model/device/compute type (WHISPER_MODEL, WHISPER_DEVICE,
  WHISPER_COMPUTE_TYPE, WHISPER_INITIAL_PROMPT)
"""

from __future__ import annotations

import io
import logging
import os
import threading
from typing import Any, Dict, Optional
import numpy as np

logger = logging.getLogger("agent.asr")

DEFAULT_INITIAL_PROMPT = (
    "Travel booking assistant. Cities and airport codes: Delhi, Mumbai, Bangalore, Goa, "
    "Chennai, Kolkata, Hyderabad, Pune, BLR, DEL, BOM, GOI, MAA. "
    "Flights, hotels, weather, build a component in TypeScript, agent."
)


class ASRProcessor:
    def __init__(
        self,
        model_size: Optional[str] = None,
        device: Optional[str] = None,
        compute_type: Optional[str] = None,
        enable_vad: bool = True,
        initial_prompt: Optional[str] = None,
    ):
        self.model_size = model_size or os.getenv("WHISPER_MODEL", "base.en")
        self.device = device or os.getenv("WHISPER_DEVICE", "cpu")
        self.compute_type = compute_type or os.getenv("WHISPER_COMPUTE_TYPE", "int8")
        self.enable_vad = enable_vad
        self.initial_prompt = (
            initial_prompt
            if initial_prompt is not None
            else os.getenv("WHISPER_INITIAL_PROMPT", DEFAULT_INITIAL_PROMPT)
        ) or None
        self._model = None
        self._load_failed = False
        # transcribe_audio_bytes runs in worker threads (asyncio.to_thread); guard the
        # one-time model load and serialize inference on the shared model instance.
        self._lock = threading.Lock()

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def info(self) -> Dict[str, Any]:
        return {
            "model": self.model_size,
            "device": self.device,
            "compute_type": self.compute_type,
            "loaded": self.is_loaded,
            "load_failed": self._load_failed,
        }

    def _ensure_model_loaded(self) -> None:
        """Lazy load the Whisper model on first invocation (thread-safe)."""
        if self._model is not None or self._load_failed:
            return
        with self._lock:
            if self._model is not None or self._load_failed:
                return
            try:
                from faster_whisper import WhisperModel
                logger.info("Loading faster-whisper model (%s) on %s (%s)...", self.model_size, self.device, self.compute_type)
                self._model = WhisperModel(
                    self.model_size,
                    device=self.device,
                    compute_type=self.compute_type,
                )
            except Exception as e:
                self._load_failed = True
                logger.warning("Could not load faster-whisper: %s. Audio will return empty transcription.", e)

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

            with self._lock:
                segments, _ = self._model.transcribe(
                    audio_input,
                    beam_size=1,
                    vad_filter=self.enable_vad,
                    vad_parameters=dict(min_silence_duration_ms=400) if self.enable_vad else None,
                    initial_prompt=self.initial_prompt,
                    condition_on_previous_text=False,
                )
                # segments is a lazy generator: it must be consumed while holding the lock.
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
