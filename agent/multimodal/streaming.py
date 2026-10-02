"""Continuous voice streaming: frame-level VAD, utterance segmentation, and partial-transcript timing.

Push-to-talk sends one recording when the user releases a button, so the agent can only react after
the whole sentence is over. Streaming lets the server hear the user *while they speak*: it detects
speech onset, emits a partial-transcript request every ~0.8s (so a correction like "wait, actually…"
can cancel stale work about a second in), and ends the utterance automatically after a pause.

This module is deliberately model-light and clock-free: time is measured in audio frames, so the
segmentation logic is deterministic and unit-testable with a scripted VAD.
"""

from __future__ import annotations

import logging
import os
import re
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Protocol, Union

import numpy as np

logger = logging.getLogger("agent.voice")

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512  # 32ms: the frame size Silero VAD expects at 16 kHz
FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_MS = FRAME_SAMPLES * 1000 / SAMPLE_RATE


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class VoiceConfig:
    speech_threshold: float = 0.5      # frame counts as speech at/above this probability
    silence_threshold: float = 0.35    # hysteresis: once speaking, only drop below this counts as silence
    min_speech_ms: float = 128         # sustained speech required before an utterance starts (rejects clicks)
    endpoint_ms: float = 500           # silence that ends an utterance (adapted per utterance, see endpoint_hint_ms)
    preroll_ms: float = 320            # audio kept from before onset so the first word isn't clipped
    max_utterance_s: float = 20.0      # hard cap; forces an endpoint on run-on speech
    partial_interval_ms: float = 500   # cadence of partial-transcript requests while speaking
    min_partial_ms: float = 600        # don't transcribe less audio than this for a partial
    tail_partial_ms: float = 200       # once silence has lasted this long, transcribe everything said so far (0 = off):
                                       # the text is then ready before the endpoint fires and the final can reuse it

    @classmethod
    def from_env(cls) -> "VoiceConfig":
        d = cls()
        return cls(
            speech_threshold=_env_float("VOICE_VAD_THRESHOLD", d.speech_threshold),
            silence_threshold=_env_float("VOICE_VAD_SILENCE_THRESHOLD", d.silence_threshold),
            min_speech_ms=_env_float("VOICE_MIN_SPEECH_MS", d.min_speech_ms),
            endpoint_ms=_env_float("VOICE_ENDPOINT_MS", d.endpoint_ms),
            preroll_ms=_env_float("VOICE_PREROLL_MS", d.preroll_ms),
            max_utterance_s=_env_float("VOICE_MAX_UTTERANCE_S", d.max_utterance_s),
            partial_interval_ms=_env_float("VOICE_PARTIAL_INTERVAL_MS", d.partial_interval_ms),
            min_partial_ms=_env_float("VOICE_MIN_PARTIAL_MS", d.min_partial_ms),
            tail_partial_ms=_env_float("VOICE_TAIL_PARTIAL_MS", d.tail_partial_ms),
        )


# Words a speaker says when more is coming ("book a flight to ... [pause]"): don't endpoint on the pause.
_CONTINUATION_WORDS = frozenset(
    "and or but to from for at in on with the a an of by then also plus into toward towards via after before "
    "between next my your our that this those these is are was".split()
)


def endpoint_hint_ms(partials: List[str], base_ms: float) -> float:
    """Adaptive endpointing (spec latency lever): how long a silence should end this utterance.

    * last partial ends mid-thought (comma or a dangling "to"/"and"/"the"...) -> wait longer (x1.6, <= 1000 ms),
      so "book a flight to ... Goa" isn't cut at the pause;
    * the last two partials agree and read as a complete request (>= 3 words, or terminal punctuation) ->
      the transcript has stopped changing, so end sooner (x0.6, >= 300 ms).
    """
    if not partials:
        return base_ms
    last = partials[-1].strip().lower()
    if not last:
        return base_ms
    words = re.findall(r"[a-z0-9']+", last)
    if last.endswith(",") or (words and words[-1] in _CONTINUATION_WORDS):
        return min(1000.0, base_ms * 1.6)
    if len(partials) >= 2 and _norm(partials[-2]) == _norm(last) and (len(words) >= 3 or last[-1] in ".?!"):
        return max(300.0, base_ms * 0.6)
    return base_ms


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", text.lower()))


# ----------------------------------------------------------------------------- events
@dataclass
class SpeechStart:
    utterance_id: str


@dataclass
class PartialDue:
    """Consumer should (if idle) transcribe `pcm` and publish it as a partial transcript."""
    utterance_id: str
    pcm: bytes
    # Taken after the speaker went quiet (>= tail_partial_ms of silence), so it covers ALL the speech said so far
    # and its text can stand in for the final transcript. Ordinary cadence partials may stop mid-word.
    is_tail: bool = False


@dataclass
class UtteranceEnd:
    utterance_id: str
    pcm: bytes
    speech_ms: float
    reason: str  # "endpoint" | "max_length" | "flush"


VoiceEvent = Union[SpeechStart, PartialDue, UtteranceEnd]


# ------------------------------------------------------------------------------- VAD
class FrameVAD(Protocol):
    def prob(self, frame: np.ndarray) -> float: ...
    def reset(self) -> None: ...


class SileroStreamingVAD:
    """Silero VAD run frame-by-frame with its recurrent state carried across calls.

    faster-whisper's SileroVADModel.__call__ zeroes the LSTM state on every call (fine for a whole
    file, wrong for a live stream), so we drive its ONNX session directly and keep h/c ourselves.
    """

    _CONTEXT = 64

    def __init__(self):
        from faster_whisper.vad import get_vad_model

        self._session = get_vad_model().session
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype="float32")
        self._c = np.zeros((1, 1, 128), dtype="float32")
        self._ctx = np.zeros((1, self._CONTEXT), dtype="float32")

    def prob(self, frame: np.ndarray) -> float:
        frame = frame.reshape(1, -1).astype("float32")
        x = np.concatenate([self._ctx, frame], axis=1)
        out, self._h, self._c = self._session.run(None, {"input": x, "h": self._h, "c": self._c})
        self._ctx = frame[:, -self._CONTEXT:]
        return float(np.asarray(out).reshape(-1)[0])


class EnergyVAD:
    """Crude RMS fallback used only if Silero/onnxruntime can't be loaded."""

    def __init__(self, threshold: float = 0.012):
        self.threshold = threshold

    def reset(self) -> None:
        pass

    def prob(self, frame: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(np.square(frame)))) if frame.size else 0.0
        return 1.0 if rms >= self.threshold else 0.0


def make_vad() -> FrameVAD:
    try:
        return SileroStreamingVAD()
    except Exception as e:  # pragma: no cover - depends on the environment
        logger.warning("Silero VAD unavailable (%s); falling back to energy VAD.", e)
        return EnergyVAD()


# ------------------------------------------------------------------------ segmentation
class VoiceStream:
    """Feed raw 16 kHz mono PCM16 bytes in any chunk size; get back speech/utterance events."""

    def __init__(self, vad: Optional[FrameVAD] = None, config: Optional[VoiceConfig] = None):
        self.vad = vad or make_vad()
        self.cfg = config or VoiceConfig.from_env()
        self._min_speech_frames = max(1, round(self.cfg.min_speech_ms / FRAME_MS))
        self._endpoint_frames = max(1, round(self.cfg.endpoint_ms / FRAME_MS))
        self._preroll_frames = max(0, round(self.cfg.preroll_ms / FRAME_MS))
        self._max_frames = round(self.cfg.max_utterance_s * 1000 / FRAME_MS)
        self._partial_frames = max(1, round(self.cfg.partial_interval_ms / FRAME_MS))
        self._min_partial_frames = max(1, round(self.cfg.min_partial_ms / FRAME_MS))
        self._tail_partial_frames = round(self.cfg.tail_partial_ms / FRAME_MS) if self.cfg.tail_partial_ms > 0 else 0
        self._utt_counter = 0
        self.reset()

    # -- lifecycle
    def reset(self) -> None:
        self.vad.reset()
        self._remainder = b""
        # Holds the lead-in AND the onset frames that triggered detection, so the first word isn't clipped.
        self._preroll: Deque[bytes] = deque(maxlen=self._preroll_frames + self._min_speech_frames)
        self._speaking = False
        self._utt_id: Optional[str] = None
        self._frames: List[bytes] = []
        self._speech_run = 0
        self._silence_run = 0
        self._speech_frames = 0
        self._since_partial = 0

    def set_endpoint_ms(self, ms: float) -> None:
        """Override the silence needed to end the CURRENT utterance (reset when it ends)."""
        self._endpoint_frames = max(1, round(ms / FRAME_MS))

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    @property
    def utterance_id(self) -> Optional[str]:
        return self._utt_id

    # -- input
    def feed(self, pcm: bytes) -> List[VoiceEvent]:
        events: List[VoiceEvent] = []
        data = self._remainder + pcm
        usable = len(data) - (len(data) % FRAME_BYTES)
        self._remainder = data[usable:]
        for off in range(0, usable, FRAME_BYTES):
            self._process_frame(data[off : off + FRAME_BYTES], events)
        return events

    def flush(self) -> List[VoiceEvent]:
        """Stream is ending: close out an utterance that is still in progress."""
        events: List[VoiceEvent] = []
        if self._speaking:
            self._end_utterance("flush", events)
        self._remainder = b""
        return events

    # -- internals
    def _process_frame(self, raw: bytes, events: List[VoiceEvent]) -> None:
        frame = np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0
        p = self.vad.prob(frame)

        if not self._speaking:
            if p >= self.cfg.speech_threshold:
                self._speech_run += 1
            else:
                self._speech_run = 0
            self._preroll.append(raw)
            if self._speech_run >= self._min_speech_frames:
                self._begin_utterance(events)
            return

        self._frames.append(raw)
        if p >= self.cfg.silence_threshold:
            self._silence_run = 0
            self._speech_frames += 1
        else:
            self._silence_run += 1

        if self._silence_run >= self._endpoint_frames:
            self._end_utterance("endpoint", events)
            return
        if len(self._frames) >= self._max_frames:
            self._end_utterance("max_length", events)
            return

        # Speech has (probably) just ended: transcribe it NOW, in parallel with the remaining endpoint wait.
        if (self._tail_partial_frames and self._silence_run == self._tail_partial_frames
                and self._tail_partial_frames < self._endpoint_frames and len(self._frames) >= self._min_partial_frames):
            self._since_partial = 0
            events.append(PartialDue(self._utt_id, b"".join(self._frames), is_tail=True))  # type: ignore[arg-type]
            return

        self._since_partial += 1
        if self._since_partial >= self._partial_frames and len(self._frames) >= self._min_partial_frames:
            self._since_partial = 0
            events.append(PartialDue(self._utt_id, b"".join(self._frames)))  # type: ignore[arg-type]

    def _begin_utterance(self, events: List[VoiceEvent]) -> None:
        self._utt_counter += 1
        self._utt_id = f"utt_{self._utt_counter}"
        self._speaking = True
        self._frames = list(self._preroll)
        self._preroll.clear()
        self._silence_run = 0
        self._speech_frames = self._speech_run
        self._since_partial = 0
        events.append(SpeechStart(self._utt_id))

    def _end_utterance(self, reason: str, events: List[VoiceEvent]) -> None:
        frames = self._frames
        # Trim the trailing silence used for endpointing, but keep a short tail so the last
        # word isn't clipped by an aggressive VAD.
        if reason == "endpoint" and self._silence_run > 4:
            frames = frames[: len(frames) - (self._silence_run - 4)]
        events.append(
            UtteranceEnd(
                utterance_id=self._utt_id,  # type: ignore[arg-type]
                pcm=b"".join(frames),
                speech_ms=self._speech_frames * FRAME_MS,
                reason=reason,
            )
        )
        self._speaking = False
        self._utt_id = None
        self._frames = []
        self._speech_run = 0
        self._silence_run = 0
        self._speech_frames = 0
        self._since_partial = 0
        self._endpoint_frames = max(1, round(self.cfg.endpoint_ms / FRAME_MS))
        self._preroll.clear()
