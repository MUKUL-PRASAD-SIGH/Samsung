"""Runs a Scenario against a real AgentCoordinator and captures everything the scorer needs."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from agent import clock
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from agent.coordinator import AgentCoordinator
from agent.eval.environment import Environment, ExecRecord, instrument_vision
from agent.eval.scenario import Scenario, Step
from agent.llm_client import LLMBackend, LLMConfig, LLMResponse, MockLLMBackend, get_backend
from agent.multimodal.asr import ASRProcessor
from agent.multimodal.vision import MockVisionBackend, OpenRouterVisionBackend, VisionBackend
from agent.schemas.events import AudioChunkEvent, InterruptSignalEvent, UserTextEvent, VideoFrameEvent
from agent.trace_logger import TraceLogger

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "audio"


@dataclass
class StepTiming:
    index: int
    t_sent: float                   # when the stimulus began (text: event created; voice: first audio frame)
    t_onset: float                  # when the user's intent began (voice: first speech, after lead silence)
    t_done: float                   # when the user finished (text: same as t_sent; voice: end of speech)


@dataclass
class LLMCall:
    t_start: float
    duration_s: float
    error: Optional[str]
    est_tokens: int


@dataclass
class RunRecord:
    scenario: Scenario
    mode: str
    trace: List[Dict[str, Any]]
    executions: List[ExecRecord]
    step_timings: List[StepTiming]
    llm_calls: List[LLMCall]
    router: Any
    t0: float
    wall_s: float
    timed_out: bool = False

    @property
    def infra_errors(self) -> int:
        """LLM calls that failed at the provider (429s, timeouts, network) -- not agent mistakes."""
        return sum(1 for c in self.llm_calls if c.error)


class RecordingBackend(LLMBackend):
    """Transparent wrapper recording every LLM call's latency, failure, and estimated token cost."""

    def __init__(self, inner: LLMBackend):
        self.inner = inner
        self.calls: List[LLMCall] = []

    async def generate(self, messages, tools=None) -> LLMResponse:
        est = int((len(json.dumps(messages)) + len(json.dumps(tools or []))) / 3.5)
        start = clock.now()
        try:
            resp = await self.inner.generate(messages, tools)
            self.calls.append(LLMCall(start, clock.now() - start, None, est))
            return resp
        except BaseException as e:
            self.calls.append(LLMCall(start, clock.now() - start, f"{type(e).__name__}: {str(e)[:120]}", est))
            raise


class Pacer:
    """Keeps live-LLM runs under a tokens-per-minute budget so a 429 isn't mistaken for an agent failure."""

    def __init__(self, tpm: int):
        self.tpm = tpm
        self._usage: Deque[Tuple[float, int]] = deque()

    def _prune(self, now: float) -> int:
        while self._usage and now - self._usage[0][0] >= 60:
            self._usage.popleft()
        return sum(t for _, t in self._usage)

    async def reserve(self, tokens: int) -> float:
        waited = 0.0
        while True:
            now = clock.now()
            if self._prune(now) + tokens <= self.tpm or not self._usage:
                self._usage.append((now, tokens))
                return waited
            delay = max(0.25, 60 - (now - self._usage[0][0]) + 0.05)
            waited += delay
            await asyncio.sleep(delay)


def _wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def _pcm16k(path: Path, lead_s: float, tail_s: float) -> bytes:
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(path), "-ar", "16000", "-ac", "1", "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    return b"\x00\x00" * int(16000 * lead_s) + raw + b"\x00\x00" * int(16000 * tail_s)


def render_frame(text: str) -> bytes:
    """A synthetic camera/screen frame: white text on a blue banner (deterministic, no fixture files)."""
    import io

    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (640, 360), (18, 60, 120))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 64)
    except OSError:
        font = ImageFont.load_default(size=64)
    ImageDraw.Draw(img).text((40, 130), text, fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _is_quiet(c: AgentCoordinator) -> bool:
    if c.event_queue._unfinished_tasks:  # an event is queued or mid-handling (e.g. awaiting the planner)
        return False
    if any(not t.done() for t in c._debounce_tasks.values()) or c._turn_tasks:
        return False
    if any(evts for evts in c._pending_events.values()):
        return False
    if any(rt.tasks or rt.stream.is_speaking for rt in c._voice.values()):
        return False
    for s in c.sessions.values():
        if any(call.status in ("pending", "running") for call in s.in_flight_calls.values()):
            return False
    return True


async def _run_voice(c: AgentCoordinator, sid: str, step: Step, timing_box: Dict[str, float]) -> None:
    audio = _pcm16k(FIXTURES / step.audio, step.lead_s, step.tail_s)
    t_start = clock.now()
    timing_box["t_sent"] = t_start
    timing_box["t_onset"] = t_start + step.lead_s
    timing_box["t_done"] = t_start + step.lead_s + _wav_seconds(FIXTURES / step.audio)
    await c.post_event(AudioChunkEvent(session_id=sid, streaming=True, stream_control="start"))
    for i in range(0, len(audio), 3200):  # 100 ms frames at real-time pace, like the browser
        await c.post_event(AudioChunkEvent(session_id=sid, audio_bytes=audio[i:i + 3200], format="pcm_16khz", streaming=True))
        await asyncio.sleep(0.1)
    await c.post_event(AudioChunkEvent(session_id=sid, streaming=True, stream_control="stop"))


async def run_scenario(
    scenario: Scenario,
    mode: str = "mock",
    asr: Optional[ASRProcessor] = None,
    pacer: Optional[Pacer] = None,
    session_id: Optional[str] = None,
) -> RunRecord:
    sid = session_id or f"eval_{scenario.name}"
    env = Environment(scenario.latency_s, scenario.faults)
    inner: LLMBackend = MockLLMBackend(canned_responses=list(scenario.mock_llm)) if mode == "mock" else get_backend(LLMConfig())
    backend = RecordingBackend(inner)
    vision: VisionBackend = (
        MockVisionBackend(list(scenario.mock_vision), latency_s=scenario.vision_latency_s)
        if mode == "mock" else OpenRouterVisionBackend()
    )
    trace = TraceLogger()
    coordinator = AgentCoordinator(
        tool_router=env.router, trace_logger=trace, llm_backend=backend,
        asr_processor=asr or ASRProcessor(), vision_backend=vision, enable_debounce=True,
    )
    instrument_vision(env, coordinator)

    if pacer is not None:
        # scripted length ~= number of planner calls (an observation tool adds a continuation call)
        turns = sum(1 for s in scenario.steps if s.is_turn)
        await pacer.reserve(max(turns, len(scenario.mock_llm)) * 1700)

    await coordinator.start()
    t0 = clock.now()
    timings: Dict[int, Dict[str, float]] = {}
    voice_tasks: List[asyncio.Task] = []
    try:
        for i, step in enumerate(scenario.steps):
            await asyncio.sleep(max(0.0, t0 + step.at_s - clock.now()))
            box: Dict[str, float] = {}
            timings[i] = box
            if step.kind == "voice":
                voice_tasks.append(asyncio.create_task(_run_voice(coordinator, sid, step, box)))
                await asyncio.sleep(0)  # let it stamp its timings
            elif step.kind == "frame":
                ev = VideoFrameEvent(session_id=sid, frame_data=render_frame(step.frame_text), mime="image/jpeg", source="harness")
                box.update(t_sent=ev.timestamp, t_onset=ev.timestamp, t_done=ev.timestamp)
                await coordinator.post_event(ev)
            elif step.kind == "interrupt":
                ev = InterruptSignalEvent(session_id=sid, reason="user_barge_in")
                box.update(t_sent=ev.timestamp, t_onset=ev.timestamp, t_done=ev.timestamp)
                await coordinator.post_event(ev)
            else:
                ev = UserTextEvent(session_id=sid, text=step.text)
                box.update(t_sent=ev.timestamp, t_onset=ev.timestamp, t_done=ev.timestamp)
                await coordinator.post_event(ev)

        if voice_tasks:
            await asyncio.gather(*voice_tasks)

        # Wait for quiescence: nothing queued/in flight and no new trace records for a moment.
        timed_out, stable_since, last_len = False, clock.now(), -1
        while True:
            await asyncio.sleep(0.1)
            now = clock.now()
            if now - t0 > scenario.max_s:
                timed_out = True
                break
            if len(trace.trace_history) != last_len or not _is_quiet(coordinator):
                last_len, stable_since = len(trace.trace_history), now
            elif now - stable_since >= 0.7:
                break
    finally:
        await coordinator.stop()

    return RunRecord(
        scenario=scenario, mode=mode, trace=list(trace.trace_history), executions=env.executions,
        step_timings=[StepTiming(i, **{k: timings[i][k] for k in ("t_sent", "t_onset", "t_done")}) for i in sorted(timings)],
        llm_calls=backend.calls, router=env.router, t0=t0, wall_s=clock.now() - t0, timed_out=timed_out,
    )
