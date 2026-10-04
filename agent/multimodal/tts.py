"""Spoken output (Phase C): text -> PCM16 audio, sentence by sentence.

Backends mirror `LLMBackend` / `VisionBackend`: a local `PiperTTSBackend` (ONNX, CPU, ~0.04 real-time factor here, so a
sentence is ready in ~0.1 s), and a deterministic `MockTTSBackend` for tests and the eval harness (silence whose length
follows the text, so interruption timing can be measured without a speaker). `TTS_BACKEND=piper|mock|off|auto`.

Synthesis is chunked per sentence so the first words start playing while the rest is still being generated, and so an
interrupt can cut the reply at a sentence boundary without wasting work on the remainder.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

logger = logging.getLogger("agent.tts")

DEFAULT_VOICE = Path(__file__).resolve().parents[2] / "models" / "piper" / "en_US-lessac-medium.onnx"
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass
class TTSAudio:
    pcm: bytes            # PCM16 little-endian, mono
    sample_rate: int

    @property
    def duration_s(self) -> float:
        return len(self.pcm) / 2 / self.sample_rate


def split_sentences(text: str, max_chars: int = 220) -> List[str]:
    """Sentence-sized chunks to synthesize. Very long sentences are split at commas/spaces so no single chunk delays the
    first audio. Markdown emphasis and bullets are stripped: they are formatting for the eye, not words to speak."""
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    out: List[str] = []
    for part in _SENTENCE_END.split(text.strip()):
        part = part.strip()
        while len(part) > max_chars:
            cut = max(part.rfind(", ", 0, max_chars), part.rfind(" ", 0, max_chars))
            cut = cut if cut > 40 else max_chars
            out.append(part[:cut].strip())
            part = part[cut:].lstrip(", ")
        if part:
            out.append(part)
    return out


def spoken_prefix(sentences: Sequence[str], durations_s: Sequence[float], played_s: float) -> str:
    """The words that had been spoken `played_s` seconds into a reply: whole sentences that finished, plus a proportional
    share of the one in progress. Sentences with no audio yet (synthesis never reached them) contribute nothing."""
    spoken: List[str] = []
    remaining = max(0.0, played_s)
    for sentence, dur in zip(sentences, durations_s):
        words = sentence.split()
        if dur <= 0 or remaining <= 0:
            break
        if remaining >= dur:
            spoken.extend(words)
            remaining -= dur
            continue
        spoken.extend(words[: int(len(words) * remaining / dur)])
        break
    return " ".join(spoken)


class TTSBackend(ABC):
    name = "tts"
    # True when synthesis is instantaneous and non-blocking (the mock): the speaker then runs it inline instead of in a
    # worker thread, which also keeps it deterministic under the virtual-time event loop.
    cheap = False

    @abstractmethod
    def synthesize(self, text: str) -> TTSAudio:
        """Blocking synthesis of one sentence (run it in a worker thread)."""


class MockTTSBackend(TTSBackend):
    """Deterministic: 55 ms of silence per character-ish (about 160 wpm), 16 kHz. Optional synthesis delay."""

    name = "mock"

    def __init__(self, sample_rate: int = 16000, delay_s: float = 0.0, seconds_per_word: float = 0.35):
        self.sample_rate, self.delay_s, self.seconds_per_word = sample_rate, delay_s, seconds_per_word
        self.cheap = delay_s == 0
        self.calls: List[str] = []

    def synthesize(self, text: str) -> TTSAudio:
        import time

        self.calls.append(text)
        if self.delay_s:
            time.sleep(self.delay_s)
        n = max(1, int(self.sample_rate * self.seconds_per_word * max(1, len(text.split()))))
        return TTSAudio(b"\x00\x00" * n, self.sample_rate)


class PiperTTSBackend(TTSBackend):
    name = "piper"

    def __init__(self, voice_path: Optional[str] = None):
        self.voice_path = str(voice_path or os.getenv("TTS_VOICE_PATH") or DEFAULT_VOICE)
        self._voice = None
        self._lock = threading.Lock()   # one ONNX session; Piper is not documented as thread-safe

    def _load(self):
        if self._voice is None:
            from piper import PiperVoice

            self._voice = PiperVoice.load(self.voice_path)
        return self._voice

    @property
    def sample_rate(self) -> int:
        return self._load().config.sample_rate

    def synthesize(self, text: str) -> TTSAudio:
        with self._lock:
            voice = self._load()
            pcm = b"".join(chunk.audio_int16_bytes for chunk in voice.synthesize(text))
            return TTSAudio(pcm, voice.config.sample_rate)

    def warmup(self) -> None:
        self.synthesize("Ready.")


def piper_available() -> bool:
    try:
        import piper  # noqa: F401
    except Exception:
        return False
    return Path(os.getenv("TTS_VOICE_PATH") or DEFAULT_VOICE).exists()


def get_tts_backend() -> Optional[TTSBackend]:
    """TTS_BACKEND=piper|mock|off|auto (default auto: Piper when installed and the voice file exists, else off)."""
    kind = os.getenv("TTS_BACKEND", "auto").strip().lower()
    if kind in ("off", "none", "0", "false"):
        return None
    if kind == "mock":
        return MockTTSBackend()
    if kind == "piper" or (kind == "auto" and piper_available()):
        if not piper_available():
            logger.warning("TTS_BACKEND=piper but piper-tts or the voice file (%s) is missing; speech disabled", DEFAULT_VOICE)
            return None
        return PiperTTSBackend()
    return None
