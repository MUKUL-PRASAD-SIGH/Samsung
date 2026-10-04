"""Phase C: spoken output and full-duplex behaviour -- pacing, instant stop on interruption, truncation memory, echo."""

import asyncio

import pytest

from agent import clock
from agent.coordination.state_machine import SessionState
from agent.coordinator import AgentCoordinator
from agent.multimodal.tts import (MockTTSBackend, PiperTTSBackend, piper_available, split_sentences,
                                  spoken_prefix)
from agent.schemas.actions import ActionType, FillerAction, SpokenResponseAction
from agent.speech import SessionSpeaker


# ------------------------------------------------------------------------------------------ chunking
def test_split_sentences_strips_markdown_and_splits_long_runs():
    assert split_sentences("Found **2 flights**. The cheapest is `IndiGo`!  Want it?") == \
        ["Found 2 flights.", "The cheapest is IndiGo!", "Want it?"]
    assert split_sentences("- first\n- second") == ["first", "second"]
    long = ", ".join(["word"] * 100)
    chunks = split_sentences(long, max_chars=80)
    assert all(len(c) <= 80 for c in chunks) and " ".join(chunks).replace(",", "").split() == ["word"] * 100
    assert split_sentences("   ") == []


def test_spoken_prefix_is_whole_sentences_plus_a_proportional_part():
    sents, durs = ["one two three four.", "five six."], [2.0, 1.0]
    assert spoken_prefix(sents, durs, 0.0) == ""
    assert spoken_prefix(sents, durs, 1.0) == "one two"                  # half of the first sentence
    assert spoken_prefix(sents, durs, 2.0) == "one two three four."
    assert spoken_prefix(sents, durs, 2.5) == "one two three four. five"
    assert spoken_prefix(sents, durs, 99) == "one two three four. five six."
    assert spoken_prefix(sents + ["never synthesized."], durs, 99) == "one two three four. five six."   # no audio, not spoken


# ------------------------------------------------------------------------------------------ the speaker
class _Rig:
    def __init__(self, text_words_per_s=0.35):
        self.session = SessionState("sp")
        self.actions = []
        self.truncated = []
        self.backend = MockTTSBackend(seconds_per_word=text_words_per_s)
        self.speaker = SessionSpeaker(self.session, self.backend, self._emit,
                                      on_truncated=lambda full, spoken: self.truncated.append((full, spoken)))
        self.speaker.enabled = True

    async def _emit(self, a):
        a.timestamp = clock.monotonic()          # virtual-clock-consistent stamps for latency checks
        self.actions.append(a)

    def states(self):
        return [(a.state, a.reason) for a in self.actions if a.action_type == ActionType.SPEECH_STATE]

    def audio(self):
        return [a for a in self.actions if a.action_type == ActionType.AUDIO_OUT]

    def state(self, name):
        return next(a for a in self.actions if a.action_type == ActionType.SPEECH_STATE and a.state == name)


REPLY = "I found two flights. The cheapest is IndiGo at 145 dollars. Want me to book it?"   # 3 sentences, 16 words


def test_reply_is_spoken_sentence_by_sentence_and_paced_to_real_time():
    async def main():
        rig = _Rig()
        t0 = clock.monotonic()
        rig.speaker.enqueue(REPLY, rig.session.epoch)
        await asyncio.sleep(0.1)
        early = len(rig.audio())                  # only the first sentence(s) so far: lookahead, not a dump
        await asyncio.wait_for(rig.speaker._runner, 30)
        return rig, early, clock.monotonic() - t0

    rig, early, took = clock.run_virtual(main())
    assert [a.text for a in rig.audio()] == ["I found two flights.", "The cheapest is IndiGo at 145 dollars.", "Want me to book it?"]
    assert [a.seq for a in rig.audio()] == [0, 1, 2] and rig.audio()[-1].is_last and not rig.audio()[0].is_last
    assert early < 3
    assert rig.states() == [("started", None), ("finished", None)]
    assert abs(took - 16 * 0.35) < 0.2            # 16 words at 0.35 s: played in real time, not instantly
    assert rig.state("finished").spoken_text == REPLY and rig.truncated == []


def test_a_reply_from_a_past_epoch_is_never_spoken():
    async def main():
        rig = _Rig()
        stale_epoch = rig.session.epoch
        rig.session.bump_epoch("user_correction")
        rig.speaker.enqueue("This is the old answer.", stale_epoch)
        await asyncio.sleep(1)
        return rig

    rig = clock.run_virtual(main())
    assert rig.audio() == [] and rig.states() == []


def test_epoch_change_stops_speech_within_150ms_and_records_what_was_heard():
    async def main():
        rig = _Rig()
        rig.speaker.enqueue(REPLY, rig.session.epoch)
        await asyncio.sleep(2.0)                  # ~5.7 words in: mid second sentence
        t_interrupt = clock.monotonic()
        rig.session.bump_epoch("user_correction")   # what a correction / barge-in does
        await asyncio.wait_for(rig.speaker._runner, 5)
        return rig, t_interrupt

    rig, t_int = clock.run_virtual(main())
    stopped = rig.state("stopped")
    assert stopped.reason == "epoch_changed"
    assert stopped.timestamp - t_int <= 0.15, f"speech kept going {stopped.timestamp - t_int:.3f}s after the interrupt"
    assert 1.9 <= stopped.spoken_ms / 1000 <= 2.2
    assert stopped.spoken_text.startswith("I found two flights.") and stopped.spoken_text != REPLY
    assert len(stopped.spoken_text.split()) < 16
    assert rig.truncated == [(REPLY, stopped.spoken_text)]
    assert [a.seq for a in rig.audio()] == [0, 1]    # the third sentence was never even synthesized
    assert len(rig.backend.calls) == 2


def test_user_taking_the_floor_stops_speech_and_clears_the_queue():
    async def main():
        rig = _Rig()
        rig.speaker.enqueue(REPLY, rig.session.epoch)
        rig.speaker.enqueue("Second queued message.", rig.session.epoch)
        await asyncio.sleep(1.0)
        t = clock.monotonic()
        rig.speaker.stop("user_spoke")
        await asyncio.wait_for(rig.speaker._runner, 5)
        await asyncio.sleep(2)
        return rig, t

    rig, t = clock.run_virtual(main())
    assert rig.state("stopped").reason == "user_spoke" and rig.state("stopped").timestamp - t <= 0.15
    assert "Second queued message." not in [a.text for a in rig.audio()]     # queue dropped, not played afterwards


def test_duck_and_resume_are_announced_once_each():
    async def main():
        rig = _Rig()
        rig.speaker.enqueue(REPLY, rig.session.epoch)
        await asyncio.sleep(0.5)
        await rig.speaker.duck()
        await rig.speaker.duck()                   # idempotent
        await rig.speaker.resume()
        await rig.speaker.resume()
        await asyncio.wait_for(rig.speaker._runner, 30)
        return rig

    rig = clock.run_virtual(main())
    assert [s for s, _ in rig.states()] == ["started", "ducked", "resumed", "finished"]


def test_disabled_speaker_is_silent():
    async def main():
        rig = _Rig()
        rig.speaker.enabled = False
        rig.speaker.enqueue(REPLY, rig.session.epoch)
        await asyncio.sleep(1)
        return rig

    assert clock.run_virtual(main()).actions == []


# --------------------------------------------------------------------------------------------- echo
def test_echo_guard_flags_replays_of_recent_speech_but_not_the_user():
    async def main():
        rig = _Rig()
        assert not rig.speaker.is_echo("I found two flights")                  # nothing spoken yet
        rig.speaker.enqueue(REPLY, rig.session.epoch)
        await asyncio.sleep(1.0)
        during = (rig.speaker.is_echo("found two flights the cheapest"),       # our own words, picked up by the mic
                  rig.speaker.is_echo("no wait make it goa"),                  # the user
                  rig.speaker.is_echo("stop"),                                  # one word: never swallowed
                  rig.speaker.is_echo("two flights"))                           # short but made of OUR words
        await asyncio.wait_for(rig.speaker._runner, 30)
        soon = rig.speaker.is_echo("cheapest is IndiGo")
        await asyncio.sleep(5)                                                  # long after: nothing is playing
        later = rig.speaker.is_echo("cheapest is IndiGo")
        return during, soon, later

    during, soon, later = clock.run_virtual(main())
    assert during == (True, False, False, True) and soon is True and later is False


# ------------------------------------------------------------------------------- coordinator wiring
async def _coordinator():
    c = AgentCoordinator(tts_backend=MockTTSBackend())
    c.intent_classifier.classify_text("warm up")    # MiniLM's first call takes seconds; the speech tests run in real time
    s = c.get_or_create_session("c1")
    return c, s


async def test_replies_are_spoken_only_after_the_client_opts_in():
    c, s = await _coordinator()
    await c.emit_action(SpokenResponseAction(session_id="c1", epoch=s.epoch, text="Hello there."))
    await asyncio.sleep(0.2)
    assert "c1" not in c._speakers                                        # nobody asked for speech
    assert c.set_tts("c1", True) is True
    await c.emit_action(FillerAction(session_id="c1", epoch=s.epoch, text="Got it."))
    await c.emit_action(SpokenResponseAction(session_id="c1", epoch=s.epoch, text="All done."))
    await asyncio.sleep(3)
    spoken = [a.text for a in list(c.action_queue._queue) if a.action_type == ActionType.AUDIO_OUT]
    assert spoken == ["Got it.", "All done."]                              # in order, fillers included
    assert c.set_tts("c1", False) is False
    await c.stop()


async def test_no_backend_means_no_speech_and_the_toggle_reports_it():
    c = AgentCoordinator()
    assert c.set_tts("x", True) is False
    await c.stop()


async def test_interrupt_signal_and_user_text_stop_the_voice():
    c, s = await _coordinator()
    c.set_tts("c1", True)
    await c.emit_action(SpokenResponseAction(session_id="c1", epoch=s.epoch, text=REPLY))
    await asyncio.sleep(0.5)
    await c._handle_interrupt(s, reason="ui_barge_in")
    await asyncio.sleep(0.3)
    kinds = [(a.state, a.reason) for a in list(c.action_queue._queue) if a.action_type == ActionType.SPEECH_STATE]
    assert ("stopped", "interrupt") in kinds or ("stopped", "epoch_changed") in kinds
    await c.stop()


async def test_truncated_reply_is_remembered_as_what_the_user_heard():
    c, s = await _coordinator()
    s.ensure_memory()
    s.scratchpad.append_utterance_chunk("find flights")
    await c._commit_turn_and_emit_graph(s, agent_response=REPLY)
    c.set_tts("c1", True)
    await c.emit_action(SpokenResponseAction(session_id="c1", epoch=s.epoch, text=REPLY))
    await asyncio.sleep(1.0)
    s.bump_epoch("user_correction")
    await asyncio.sleep(0.4)
    node = list(s.graph_memory.turns.values())[-1]
    assert node.spoken_text and node.spoken_text != REPLY and REPLY.startswith(node.spoken_text.rstrip(".")[:10])
    ctx = s.graph_memory.get_subgraph_prompt_context()
    assert "[interrupted by the user" in ctx[-1]["content"] and "Want me to book it?" not in ctx[-1]["content"]
    await c.stop()


def test_audio_is_redacted_in_the_trace_not_logged_as_megabytes_of_base64():
    from agent.schemas.actions import AudioOutAction
    from agent.trace_logger import TraceLogger

    t = TraceLogger()
    rec = t.log_action(AudioOutAction(session_id="s", epoch=1, utterance_id="u", seq=0, text="hi", sample_rate=16000,
                                      duration_ms=500, audio_b64="A" * 40000))
    assert rec["payload"]["audio_b64"] == "<30000 bytes>"


async def test_the_agents_own_voice_in_the_mic_is_dropped_not_treated_as_the_user():
    from agent.multimodal.streaming import UtteranceEnd, VoiceStream

    c, s = await _coordinator()

    class Heard:                      # the mic picked up the agent's own sentence
        model_size = "stub"

        def transcribe_audio_bytes(self, pcm, fmt):
            return "the cheapest is IndiGo at 145 dollars"

    c.asr_processor = Heard()
    c.set_tts("c1", True)
    await c.emit_action(SpokenResponseAction(session_id="c1", epoch=s.epoch, text=REPLY))
    await asyncio.sleep(1.2)
    from agent.coordinator import _VoiceRuntime

    class V:
        def reset(self): pass
        def prob(self, f): return 0.0

    rt = _VoiceRuntime(stream=VoiceStream(vad=V()))
    await c._voice_final(s, rt, UtteranceEnd("u9", b"\x00" * 32000, 1000.0, "endpoint"), None)
    queued = [e for e in list(c.event_queue._queue) if getattr(e, "text", None)]
    assert queued == []                                   # no user_text: it never reached the planner
    assert c._speakers["c1"].speaking                      # and the agent kept talking
    await c.stop()


def test_vad_bar_is_raised_while_speaking_and_restored():
    import numpy as np
    from agent.multimodal.streaming import VoiceConfig, VoiceStream

    class P:
        def __init__(self): self.p = 0.6
        def reset(self): pass
        def prob(self, f): return self.p

    vad = P()
    s = VoiceStream(vad=vad, config=VoiceConfig(speech_threshold=0.5, min_speech_ms=64))
    frame = (np.ones(512, dtype=np.int16) * 500).tobytes()
    s.set_speech_threshold(0.75)
    assert s.feed(frame * 6) == []                          # 0.6 < 0.75: faint (echo-like) speech does not open an utterance
    s.set_speech_threshold(None)
    assert any(type(e).__name__ == "SpeechStart" for e in s.feed(frame * 6))


# --------------------------------------------------------------------------------------- real Piper
@pytest.mark.skipif(not piper_available(), reason="piper-tts / voice file not installed")
class TestPiper:
    def test_synthesizes_natural_length_audio_quickly(self):
        import time

        b = PiperTTSBackend()
        b.warmup()
        t = time.perf_counter()
        audio = b.synthesize("Your flight from Delhi to Mumbai is confirmed.")
        took = time.perf_counter() - t
        assert audio.sample_rate == 22050 and 1.5 < audio.duration_s < 5.0
        assert took < audio.duration_s          # faster than real time, or paced streaming could never keep up
        assert max(abs(int.from_bytes(audio.pcm[i:i + 2], "little", signed=True)) for i in range(0, len(audio.pcm), 400)) > 1000

    def test_its_own_voice_is_recognized_as_echo_after_a_real_round_trip(self):
        """Synthesize, resample to the mic format, transcribe with real Whisper, and check the echo guard fires."""
        import subprocess

        from agent.multimodal.asr import ASRProcessor

        asr = ASRProcessor()
        asr._ensure_model_loaded()
        if not asr.is_loaded:
            pytest.skip("Whisper unavailable")
        b = PiperTTSBackend()
        sentence = "Your flight from Delhi to Mumbai is confirmed."
        audio = b.synthesize(sentence)
        pcm16k = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", str(audio.sample_rate), "-ac", "1", "-i", "-",
             "-ar", "16000", "-ac", "1", "-f", "s16le", "-"], input=audio.pcm, capture_output=True, check=True).stdout
        heard = asr.transcribe_audio_bytes(pcm16k, "pcm_16khz")
        assert "mumbai" in heard.lower()                                   # the loop-back really is intelligible speech

        async def main():
            rig = _Rig()
            rig.speaker.enqueue(sentence, rig.session.epoch)
            await asyncio.sleep(0.2)
            return rig.speaker.is_echo(heard), rig.speaker.is_echo("actually make it Goa")

        echo, user = clock.run_virtual(main())
        assert echo is True and user is False


# ----------------------------------------------------------------- echo guard: what Whisper writes vs what we said
def test_skeleton_matches_digits_and_spelled_ids():
    from agent.speech import skeleton

    assert skeleton("The booking ID is FL-98214.") == skeleton("The booking ID is F L nine eight two one four.")
    assert skeleton("It costs $145") == skeleton("It costs one four five dollars").replace("dollars", "")


def test_echo_guard_survives_whisper_writing_numbers_and_ids_differently():
    """The failure that shipped first: we SPOKE 'F L nine eight two one four', Whisper WROTE 'FL-98214', a word-set
    comparison called that the user and the agent cut itself off."""
    async def main():
        rig = _Rig()
        rig.speaker.enqueue("Your flight is confirmed. The booking ID is F L nine eight two one four.", rig.session.epoch)
        await asyncio.sleep(2.2)
        return (rig.speaker.is_echo("The booking ID is FL-98214."),
                rig.speaker.is_echo("Your flight is confirmed, the booking ID is FL 9 8 2 1 4"),
                rig.speaker.is_echo("Your flight is confirmed the booking idea is FL98214"))   # a misheard word too

    assert clock.run_virtual(main()) == (True, True, True)


def test_a_user_repeating_words_we_said_earlier_is_not_echo():
    async def main():
        rig = _Rig()
        rig.speaker.enqueue("I found two flights. The cheapest is IndiGo at 145 dollars. Want me to book it?", rig.session.epoch)
        await asyncio.sleep(1.0)       # only the first sentence has been played so far
        return (rig.speaker.is_echo("book it please"),                       # short: too ambiguous to call, treated as the user
                rig.speaker.is_echo("actually the flight from Pune to Jaipur"))

    assert clock.run_virtual(main()) == (False, False)


async def test_a_mangled_fragment_does_not_stop_the_agent_but_real_words_and_interrupts_do():
    from agent.coordinator import _VoiceRuntime
    from agent.multimodal.streaming import PartialDue, VoiceStream

    class V:
        def reset(self): pass
        def prob(self, f): return 0.0

    c, s = await _coordinator()
    c.set_tts("c1", True)
    await c.emit_action(SpokenResponseAction(session_id="c1", epoch=s.epoch, text=REPLY))
    await asyncio.sleep(0.6)
    speaker = c._speakers["c1"]
    rt = _VoiceRuntime(stream=VoiceStream(vad=V()))

    async def heard(text):
        class A:
            model_size = "stub"
            def transcribe_audio_bytes(self, pcm, fmt): return text
        c.asr_processor = A()
        await c._voice_partial(s, rt, PartialDue("u1", b"\x00" * 100))

    await heard("Fl.")                                  # a scrap of our own voice, mangled
    assert speaker.speaking and speaker._stop_reason is None
    await heard("Delhi Mumbai Delhi Mumbai Delhi")      # a hallucinated loop: substantial, but only ONE partial of evidence
    assert speaker._stop_reason is None
    await heard("I would like to go somewhere else")    # ...the second consecutive one is a user really talking over us
    assert speaker._stop_reason == "user_spoke"
    await asyncio.sleep(0.3)                            # it has stopped; let it speak again for the keyword check
    await c.emit_action(SpokenResponseAction(session_id="c1", epoch=s.epoch, text=REPLY))
    await asyncio.sleep(0.6)
    assert speaker.speaking and speaker._stop_reason is None
    rt2 = _VoiceRuntime(stream=VoiceStream(vad=V()))
    class B:
        model_size = "stub"
        def transcribe_audio_bytes(self, pcm, fmt): return "No wait"
    c.asr_processor = B()
    await c._voice_partial(s, rt2, PartialDue("u3", b"\x00" * 100))   # an interrupt keyword needs no second opinion
    assert speaker._stop_reason == "user_spoke"
    await c.stop()


async def test_sustained_non_echo_speech_stops_the_agent():
    from agent.coordinator import _VoiceRuntime
    from agent.multimodal.streaming import PartialDue, VoiceStream

    class V:
        def reset(self): pass
        def prob(self, f): return 0.0

    c, s = await _coordinator()
    c.set_tts("c1", True)
    await c.emit_action(SpokenResponseAction(session_id="c1", epoch=s.epoch, text=REPLY))
    await asyncio.sleep(0.6)
    rt = _VoiceRuntime(stream=VoiceStream(vad=V()))

    class A:
        model_size = "stub"
        def transcribe_audio_bytes(self, pcm, fmt): return "Please tell me about hotels near the beach today"
    c.asr_processor = A()
    await c._voice_partial(s, rt, PartialDue("u2", b"\x00" * 100))
    assert c._speakers["c1"]._stop_reason is None             # first substantial partial: not yet
    await c._voice_partial(s, rt, PartialDue("u2", b"\x00" * 100))
    assert c._speakers["c1"]._stop_reason == "user_spoke"      # second consecutive one: stop
    await c.stop()
