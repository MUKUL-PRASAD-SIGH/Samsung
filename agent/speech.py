"""The agent's voice for one session (Phase C): speak replies, stay interruptible, never hear itself.

Full-duplex rules implemented here:

* Replies are synthesized sentence by sentence and PACED to real time (a short lookahead of audio is kept ahead of
  the playhead). That keeps the server's estimate of "how much has been heard" accurate, and means a cut-off wastes
  almost no synthesis.
* A reply belongs to the epoch it was planned under. The moment the session epoch moves on (correction, barge-in,
  interrupt) the speaker stops, polling every ~25 ms, so speech ends well inside the 150 ms target after the epoch bump.
* When a reply is cut off, the words actually spoken are recorded (`SpeechStateAction(stopped, spoken_text=...)`)
  and written to memory, so the agent never assumes the user heard the part they were cut off before.
* Echo: the agent's own voice must not be taken for the user. Recently spoken words are remembered and a transcript
  that is (mostly) a replay of them is flagged by `is_echo`, so the coordinator can ignore it.
* Truthfulness: speech is just a rendering of replies the planner already emitted, which only claim a completion after
  the matching tool result. Nothing here speaks ahead of that.
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Deque, List, Optional

from agent import clock
from agent.multimodal.tts import TTSBackend, split_sentences, spoken_prefix
from agent.schemas.actions import AudioOutAction, BaseAction, SpeechStateAction

logger = logging.getLogger("agent.speech")

LOOKAHEAD_S = 0.6        # audio kept buffered ahead of the playhead
POLL_S = 0.025           # how quickly a stop request / epoch change is noticed
ECHO_WINDOW_S = 4.5      # the mic hears what was played in roughly the last few seconds (ASR + VAD latency)
ECHO_OVERLAP = 0.6       # fraction of a transcript's characters that must line up with what the agent just said.
                         # Whisper garbles synthetic speech played back through a mic ("booking ID is F L" -> "giving ID is a
                         # film"), and those transcripts still align ~65-75% -- real user speech essentially never does.
ECHO_MIN_CHARS = 10      # shorter transcripts are too ambiguous to call: "book it" may be the user answering us
_DIGIT_WORDS = "zero one two three four five six seven eight nine".split()


def skeleton(text: str) -> str:
    """Spelling-independent form for comparing what we SAID with what Whisper WROTE: lower-case, digits spelled out
    ("FL-98214" -> "flnineeighttwoonefour"), everything but letters removed. Whisper writes numbers as digits and
    spelled-out IDs as one token, while we spoke words, so a word-set comparison misses exactly the content most
    likely to be misread."""
    text = re.sub(r"\d", lambda m: " " + _DIGIT_WORDS[int(m.group())] + " ", text.lower())
    return re.sub(r"[^a-z]", "", text)


def is_substantial(text: str) -> bool:
    """Long enough to judge (see ECHO_MIN_CHARS)."""
    return len(skeleton(text)) >= ECHO_MIN_CHARS


@dataclass
class _Item:
    text: str
    epoch: int


class _Stop(Exception):
    def __init__(self, reason: str):
        self.reason = reason


class SessionSpeaker:
    def __init__(
        self,
        session,
        backend: TTSBackend,
        emit: Callable[[BaseAction], Awaitable[None]],
        on_truncated: Optional[Callable[[str, str], None]] = None,
        on_activity: Optional[Callable[[bool], None]] = None,
        lookahead_s: float = LOOKAHEAD_S,
        poll_s: float = POLL_S,
    ):
        self.session, self.backend, self._emit = session, backend, emit
        self.enabled = False
        self._on_truncated, self._on_activity = on_truncated, on_activity
        self.lookahead_s, self.poll_s = lookahead_s, poll_s
        self._queue: Deque[_Item] = deque()
        self._runner: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._stop_reason: Optional[str] = None
        self._counter = 0
        self.speaking = False
        self.ducked = False
        self._current_utt: Optional[str] = None
        self._timeline: Deque[tuple] = deque(maxlen=40)   # (play_start, play_end, sentence skeleton), monotonic seconds
        self._last_spoke_at: Optional[float] = None
        self.stop_latencies_s: List[float] = []     # stop requested/epoch seen -> `stopped` emitted (for the eval)

    # ----------------------------------------------------------------------------------------- control
    def enqueue(self, text: str, epoch: int) -> None:
        if not self.enabled or not text.strip():
            return
        self._queue.append(_Item(text, epoch))
        if self._runner is None or self._runner.done():
            self._runner = asyncio.create_task(self._run())

    def stop(self, reason: str) -> None:
        """Cut off whatever is being said and drop everything queued (the user has the floor)."""
        self._queue.clear()
        if self.speaking:
            self._stop_reason = self._stop_reason or reason
            self._stop_requested_at = clock.monotonic()
            self._wake.set()

    async def duck(self) -> None:
        """The user started talking: the client lowers the volume while we find out whether it was a real barge-in."""
        if self.speaking and not self.ducked and self._current_utt:
            self.ducked = True
            await self._state("ducked", self._current_utt, reason="user_speech_start")

    async def resume(self) -> None:
        """It was noise or our own echo: back to full volume."""
        if self.ducked and self._current_utt:
            self.ducked = False
            await self._state("resumed", self._current_utt, reason="false_alarm")

    def close(self) -> None:
        self.enabled = False
        self._queue.clear()
        if self._runner and not self._runner.done():
            self._runner.cancel()
        self.speaking = False

    # ------------------------------------------------------------------------------------------- echo
    def is_echo(self, transcript: str) -> bool:
        """True when `transcript` is (mostly) the agent's own recent speech coming back through the microphone.

        Compared against what was playing in the last ECHO_WINDOW_S seconds (not everything ever said), so a user who
        genuinely repeats one of our earlier words isn't swallowed, and only for transcripts long enough to judge."""
        heard = skeleton(transcript)
        if len(heard) < ECHO_MIN_CHARS or not self._timeline:
            return False
        now = clock.monotonic()
        if not self.speaking and (self._last_spoke_at is None or now - self._last_spoke_at > ECHO_WINDOW_S):
            return False
        window = "".join(sk for start, end, sk in self._timeline if end >= now - ECHO_WINDOW_S and start <= now + 0.5)
        if not window:
            return False
        blocks = difflib.SequenceMatcher(None, window, heard, autojunk=False).get_matching_blocks()
        matched = sum(b.size for b in blocks if b.size >= 3)       # ignore chance 1-2 letter coincidences
        return matched / len(heard) >= ECHO_OVERLAP

    # ----------------------------------------------------------------------------------------- internals
    async def _state(self, state: str, utt: str, **kw) -> None:
        await self._emit(SpeechStateAction(
            session_id=self.session.session_id, epoch=self.session.epoch, state=state, utterance_id=utt, **kw))

    def _set_activity(self, speaking: bool) -> None:
        self.speaking = speaking
        if self._on_activity:
            try:
                self._on_activity(speaking)
            except Exception:  # noqa: BLE001
                logger.exception("on_activity callback failed")

    async def _sleep_until(self, deadline: float, epoch: int) -> None:
        """Sleep until `deadline` (monotonic) in small steps, raising _Stop as soon as the user takes the floor."""
        while True:
            self._check(epoch)
            remaining = deadline - clock.monotonic()
            if remaining <= 0.001:    # sub-millisecond leftovers are done (also avoids a float-resolution spin on a virtual clock)
                return
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=min(self.poll_s, remaining))
            except asyncio.TimeoutError:
                pass

    def _check(self, epoch: int) -> None:
        if self._stop_reason:
            raise _Stop(self._stop_reason)
        if self.session.epoch != epoch:
            self._stop_requested_at = getattr(self, "_stop_requested_at", None) or clock.monotonic()
            raise _Stop("epoch_changed")

    async def _run(self) -> None:
        while self._queue and self.enabled:
            item = self._queue.popleft()
            if item.epoch < self.session.epoch:
                continue    # planned for an epoch the user already moved past: never voice it
            self._stop_reason = None
            self._stop_requested_at = None
            await self._speak(item)

    async def _speak(self, item: _Item) -> None:
        self._counter += 1
        utt = f"tts_{self._counter}"
        self._current_utt = utt
        sentences = split_sentences(item.text) or [item.text]
        durations: List[float] = []
        cum, t_start, seq = 0.0, None, 0
        self._set_activity(True)
        try:
            await self._state("started", utt, text=item.text)
            for i, sentence in enumerate(sentences):
                if t_start is not None:                      # pace: stay `lookahead_s` ahead of the playhead
                    await self._sleep_until(t_start + cum - self.lookahead_s, item.epoch)
                self._check(item.epoch)
                audio = (self.backend.synthesize(sentence) if self.backend.cheap
                         else await asyncio.to_thread(self.backend.synthesize, sentence))
                self._check(item.epoch)                      # the user may have spoken while we were synthesizing
                last = i == len(sentences) - 1
                await self._emit(AudioOutAction(
                    session_id=self.session.session_id, epoch=item.epoch, utterance_id=utt, seq=seq, text=sentence,
                    sample_rate=audio.sample_rate, duration_ms=round(audio.duration_s * 1000, 1),
                    audio_b64=base64.b64encode(audio.pcm).decode("ascii"), is_last=last))
                if t_start is None:
                    t_start = clock.monotonic()
                self._timeline.append((t_start + cum, t_start + cum + audio.duration_s, skeleton(sentence)))
                durations.append(audio.duration_s)
                cum += audio.duration_s
                seq += 1
            await self._sleep_until(t_start + cum, item.epoch)   # let the last sentence finish playing
            self._last_spoke_at = clock.monotonic()
            await self._state("finished", utt, text=item.text, spoken_text=item.text, spoken_ms=round(cum * 1000, 1))
        except _Stop as stop:
            played = min(clock.monotonic() - t_start, cum) if t_start is not None else 0.0
            spoken = spoken_prefix(sentences, durations, played)
            self._last_spoke_at = clock.monotonic()
            requested = getattr(self, "_stop_requested_at", None)
            await self._state("stopped", utt, reason=stop.reason, text=item.text, spoken_text=spoken,
                              spoken_ms=round(played * 1000, 1))
            if requested is not None:
                self.stop_latencies_s.append(clock.monotonic() - requested)
            self._queue.clear()
            if self._on_truncated and spoken != item.text:
                self._on_truncated(item.text, spoken)
        finally:
            self.ducked = False
            self._current_utt = None
            self._set_activity(bool(self._queue))
