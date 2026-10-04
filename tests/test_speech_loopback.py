"""The headline full-duplex guarantee, with the REAL pieces: the agent speaks (Piper), that audio is fed straight back
into its own microphone (as a speaker next to a mic would), and the agent must neither mistake itself for the user
nor interrupt itself. Then a REAL user barge-in over the top must stop it. Needs piper-tts, its voice file and Whisper."""

import asyncio
import subprocess

import pytest

from agent.coordinator import AgentCoordinator
from agent.multimodal.asr import ASRProcessor
from agent.multimodal.tts import PiperTTSBackend, piper_available
from agent.schemas.actions import ActionType, SpokenResponseAction
from agent.schemas.events import AudioChunkEvent, EventType

pytestmark = pytest.mark.skipif(not piper_available(), reason="piper-tts / voice file not installed")

REPLY = "Your flight from Delhi to Mumbai is confirmed. The booking ID is F L nine eight two one four."


def _to_16k(pcm: bytes, rate: int) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "-", "-ar", "16000", "-ac", "1",
         "-f", "s16le", "-"], input=pcm, capture_output=True, check=True).stdout


def _drain(c):
    out = []
    while not c.action_queue.empty():
        out.append(c.action_queue.get_nowait())
    return out


async def _feed(c, sid, pcm16k: bytes, pace=True):
    for i in range(0, len(pcm16k), 3200):
        await c.post_event(AudioChunkEvent(session_id=sid, audio_bytes=pcm16k[i:i + 3200], format="pcm_16khz", streaming=True))
        if pace:
            await asyncio.sleep(0.1)


async def _setup():
    asr = ASRProcessor()
    asr._ensure_model_loaded()
    if not asr.is_loaded:
        pytest.skip("Whisper unavailable")
    c = AgentCoordinator(tts_backend=PiperTTSBackend(), asr_processor=asr)
    await c.start()
    c.set_tts("lb", True)
    await c.post_event(AudioChunkEvent(session_id="lb", streaming=True, stream_control="start", format="pcm_16khz"))
    return c


async def test_the_agent_does_not_hear_itself_and_finishes_its_sentence():
    c = await _setup()
    try:
        s = c.get_or_create_session("lb")
        epoch0 = s.epoch
        await c.emit_action(SpokenResponseAction(session_id="lb", epoch=s.epoch, text=REPLY))
        feeders, heard_audio, states = [], [], []
        # loop each synthesized sentence back into the mic as soon as it is "played"
        for _ in range(400):                          # up to 20 s: generous, so a loaded machine can't fail this on timing
            await asyncio.sleep(0.05)
            for a in _drain(c):
                if a.action_type == ActionType.AUDIO_OUT:
                    import base64
                    heard_audio.append(a)
                    feeders.append(asyncio.create_task(_feed(c, "lb", _to_16k(base64.b64decode(a.audio_b64), a.sample_rate))))
                    # silence between sentences so the endpointer can close each looped utterance
                    feeders.append(asyncio.create_task(_feed(c, "lb", b"\x00\x00" * 16000)))
                elif a.action_type == ActionType.SPEECH_STATE:
                    states.append((a.state, a.reason))
            if any(st in ("finished", "stopped") for st, _ in states):
                break
        await asyncio.gather(*feeders)
        await asyncio.sleep(2.0)                      # let any partial/final of the looped audio finish transcribing
        for a in _drain(c):
            if a.action_type == ActionType.SPEECH_STATE:
                states.append((a.state, a.reason))
        user_texts = [e for e in list(c.event_queue._queue) if e.event_type == EventType.USER_TEXT]
        assert s.epoch == epoch0, "the agent interrupted ITSELF (its own voice was taken for a barge-in)"
        assert not any(st == "stopped" for st, _ in states), f"the agent cut itself off: {states}"
        assert not user_texts, f"its own words were posted as user input: {[e.text for e in user_texts]}"
        assert ("finished", None) in states, f"the reply never finished: {states}"
        assert len(heard_audio) >= 2, "the reply should have been synthesized in several sentences"
    finally:
        await c.stop()


async def test_a_real_user_talking_over_the_agent_stops_it_mid_sentence():
    c = await _setup()
    try:
        s = c.get_or_create_session("lb")
        await c.emit_action(SpokenResponseAction(session_id="lb", epoch=s.epoch, text=REPLY))
        for _ in range(100):                           # wait until it is actually talking
            await asyncio.sleep(0.05)
            if c._speakers["lb"].speaking and any(a.action_type == ActionType.AUDIO_OUT for a in list(c.action_queue._queue)):
                break
        _drain(c)
        fixture = __import__("pathlib").Path(__file__).parent / "fixtures" / "audio" / "correction_goa.wav"
        user = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(fixture), "-ar", "16000", "-ac", "1", "-f", "s16le", "-"],
                              capture_output=True, check=True).stdout
        await _feed(c, "lb", b"\x00\x00" * 4800 + user + b"\x00\x00" * 24000)
        await asyncio.sleep(2.0)
        acts = _drain(c)
        stopped = [a for a in acts if a.action_type == ActionType.SPEECH_STATE and a.state == "stopped"]
        ducked = [a for a in acts if a.action_type == ActionType.SPEECH_STATE and a.state == "ducked"]
        assert ducked, "the client should have been told to duck when the user started talking"
        assert stopped, f"the agent kept talking over the user: {[a.action_type for a in acts][:12]}"
        assert stopped[0].reason in ("user_spoke", "epoch_changed", "interrupt")
        assert 0 < len(stopped[0].spoken_text.split()) < len(REPLY.split()) and stopped[0].spoken_text != REPLY
    finally:
        await c.stop()
