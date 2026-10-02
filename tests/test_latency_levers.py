"""Phase B latency levers: soft+hard LLM deadlines, adaptive endpointing, provider usage."""

import asyncio

import numpy as np

from agent import clock
from agent.llm_client import CircuitBreakerLLMClient, LLMBackend, LLMConfig, LLMResponse, _usage
from agent.multimodal.streaming import FRAME_MS, VoiceConfig, VoiceStream, endpoint_hint_ms


class _Slow(LLMBackend):
    def __init__(self, delay):
        self.delay, self.cancelled = delay, False

    async def generate(self, messages, tools=None):
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return LLMResponse(response_type="spoken_response", content="done")


def _client(delay, soft=1.0, hard=5.0):
    backend = _Slow(delay)
    return backend, CircuitBreakerLLMClient(backend, LLMConfig(backend_type="mock", timeout_s=hard, soft_deadline_s=soft))


def _run(coro):
    return clock.run_virtual(coro)


def test_soft_deadline_fires_once_then_keeps_waiting_for_the_answer():
    async def main():
        backend, client = _client(3.0)
        fired = []

        async def on_slow():
            fired.append(clock.monotonic())

        t0 = clock.monotonic()
        resp = await client.generate([], on_slow=on_slow)
        return resp, fired, clock.monotonic() - t0, t0

    resp, fired, total, t0 = _run(main())
    assert resp.content == "done" and resp.response_type == "spoken_response"   # the slow answer was NOT discarded
    assert len(fired) == 1 and abs(fired[0] - t0 - 1.0) < 0.05 and abs(total - 3.0) < 0.05


def test_fast_answers_never_trigger_the_progress_line():
    async def main():
        _, client = _client(0.2)
        fired = []

        async def on_slow():
            fired.append(1)

        return await client.generate([], on_slow=on_slow), fired

    resp, fired = _run(main())
    assert resp.content == "done" and fired == []


def test_hard_deadline_still_applies_and_cancels_the_call():
    async def main():
        backend, client = _client(60.0)
        fired = []

        async def on_slow():
            fired.append(1)

        t0 = clock.monotonic()
        resp = await client.generate([], on_slow=on_slow)
        return backend, resp, fired, clock.monotonic() - t0

    backend, resp, fired, took = _run(main())
    assert fired == [1] and backend.cancelled and abs(took - 5.0) < 0.05
    assert resp.response_type == "clarification"          # graceful degradation, as before


def test_a_failing_progress_callback_cannot_fail_the_request():
    async def main():
        _, client = _client(2.0)

        async def boom():
            raise RuntimeError("ui gone")

        return await client.generate([], on_slow=boom)

    assert _run(main()).content == "done"


def test_no_callback_or_disabled_soft_deadline_behaves_as_before():
    async def main():
        _, client = _client(2.0, soft=0.0)
        called = []

        async def cb():
            called.append(1)

        return (await client.generate([], on_slow=cb)).content, called

    assert _run(main()) == ("done", [])


def test_provider_usage_is_parsed_defensively():
    assert _usage({"usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}}) == \
        {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
    assert _usage({}) is None and _usage({"usage": "x"}) is None and _usage({"usage": {"prompt_tokens": "n/a"}}) is None


# ------------------------------------------------------------------------ adaptive endpointing
def test_endpoint_hint_waits_after_a_dangling_word_and_ends_early_when_stable():
    base = 500
    assert endpoint_hint_ms([], base) == base
    assert endpoint_hint_ms(["book a flight to"], base) > base            # mid-thought
    assert endpoint_hint_ms(["find flights,"], base) > base
    assert endpoint_hint_ms(["book a flight to Goa"], base) == base       # one sample: not yet trusted
    assert endpoint_hint_ms(["book a flight to Goa", "Book a flight to Goa."], base) < base
    assert endpoint_hint_ms(["stop", "stop"], base) == base               # too short to call complete
    assert endpoint_hint_ms(["book a flight to Goa", "book a flight to"], base) > base  # transcript still changing
    assert 300 <= endpoint_hint_ms(["a b c", "a b c"], 400) <= 400 and endpoint_hint_ms(["x to"], 900) <= 1000


class _Const:
    def __init__(self, p): self.p = p
    def reset(self): pass
    def prob(self, frame): return self.p


def _speech_then_silence(stream, speech_ms, silence_ms):
    pcm = lambda ms: (np.ones(int(16 * ms), dtype=np.int16) * 1000).tobytes()
    stream.vad.p = 1.0
    events = stream.feed(pcm(speech_ms))
    stream.vad.p = 0.0
    return events, stream.feed(pcm(silence_ms))


def test_endpoint_override_shortens_and_resets_per_utterance():
    cfg = VoiceConfig(endpoint_ms=600, min_partial_ms=10_000)
    s = VoiceStream(vad=_Const(1.0), config=cfg)
    s.vad.p = 1.0
    _speech_then_silence(s, 500, 0)
    assert s.is_speaking
    s.set_endpoint_ms(300)
    s.vad.p = 0.0
    ev = s.feed((np.zeros(int(16 * 350), dtype=np.int16)).tobytes())   # 350 ms silence: past 300, short of 600
    assert any(type(e).__name__ == "UtteranceEnd" for e in ev)
    # next utterance is back on the configured 600 ms
    s.vad.p = 1.0
    s.feed((np.ones(int(16 * 500), dtype=np.int16) * 1000).tobytes())
    s.vad.p = 0.0
    assert not any(type(e).__name__ == "UtteranceEnd" for e in s.feed(np.zeros(int(16 * 350), dtype=np.int16).tobytes()))


def test_tail_partial_fires_once_after_speech_stops_and_is_marked_reusable():
    cfg = VoiceConfig(endpoint_ms=600, tail_partial_ms=200, min_partial_ms=300, partial_interval_ms=10_000)
    s = VoiceStream(vad=_Const(1.0), config=cfg)
    s.vad.p = 1.0
    s.feed((np.ones(int(16 * 800), dtype=np.int16) * 1000).tobytes())
    s.vad.p = 0.0
    events = s.feed(np.zeros(int(16 * 550), dtype=np.int16).tobytes())     # silence, but short of the 600 ms endpoint
    tails = [e for e in events if type(e).__name__ == "PartialDue"]
    assert len(tails) == 1 and tails[0].is_tail
    more = s.feed(np.zeros(int(16 * 200), dtype=np.int16).tobytes())
    assert any(type(e).__name__ == "UtteranceEnd" for e in more)


async def _final_with(is_tail, pcm_len=32000):
    """Drive _voice_final with a stub ASR; returns (text posted, ASR calls)."""
    from agent.coordinator import AgentCoordinator, _VoiceRuntime
    from agent.multimodal.streaming import PartialDue, UtteranceEnd

    c = AgentCoordinator()
    calls = []

    class StubASR:
        model_size = "stub"

        def transcribe_audio_bytes(self, pcm, fmt):
            calls.append(len(pcm))
            return "full final text"

    c.asr_processor = StubASR()
    s = c.get_or_create_session("v")
    rt = _VoiceRuntime(stream=VoiceStream(vad=_Const(0.0)))
    # the partial as _voice_partial would have recorded it
    if is_tail:
        rt.last_partial["u1"] = (pcm_len, "tail partial text")
    await c._voice_final(s, rt, UtteranceEnd("u1", b"\x00" * pcm_len, 1000.0, "endpoint"), None)
    posted = [e for e in [c.event_queue.get_nowait() for _ in range(c.event_queue.qsize())] if getattr(e, "text", None)]
    return posted[-1].text, calls


async def test_final_reuses_only_a_tail_partial_and_otherwise_transcribes():
    text, calls = await _final_with(is_tail=True)
    assert text == "tail partial text" and calls == []            # no second ASR pass over the same audio
    text, calls = await _final_with(is_tail=False)
    assert text == "full final text" and len(calls) == 1          # a mid-speech partial must never stand in
