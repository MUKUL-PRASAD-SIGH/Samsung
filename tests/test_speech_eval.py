"""The eval harness must be able to tell an agent that goes quiet when interrupted from one that talks over the user."""

from agent import clock
from agent.eval.runner import run_scenario
from agent.eval.scorer import score_run
from agent.eval.suites import BY_NAME
from agent.speech import SessionSpeaker


def _score(name):
    async def go():
        return score_run(await run_scenario(BY_NAME[name], mode="mock"))
    return clock.run_virtual(go())


def test_healthy_agent_stops_talking_within_150ms_and_records_the_truncation():
    s = _score("speak_barge_in")
    assert s.total >= 0.97 and s.task.score == 1.0
    assert s.interrupt.raw["speech_stop_latency_s"] and max(s.interrupt.raw["speech_stop_latency_s"]) <= 0.15
    assert s.interrupt.parts["speech_stopped"] == 1.0 and s.interrupt.parts["truncation_recorded"] == 1.0
    assert s.interrupt.parts["no_audio_after_stop"] == 1.0


def test_spoken_replies_do_not_disturb_an_uninterrupted_turn():
    s = _score("speak_plain")
    assert s.total >= 0.97 and s.interrupt is None and not s.notes


def test_mutation_an_agent_that_keeps_talking_is_caught(monkeypatch):
    """Break the speaker so neither an epoch change nor an explicit stop silences it."""
    monkeypatch.setattr(SessionSpeaker, "_check", lambda self, epoch: None)
    s = _score("speak_barge_in")
    assert s.interrupt.parts["speech_stopped"] == 0.0
    assert any("kept talking" in n for n in s.notes)
    assert s.interrupt.score < 0.7 and s.total < 0.9


def test_mutation_forgetting_what_was_actually_heard_is_caught(monkeypatch):
    import agent.speech as speech

    monkeypatch.setattr(speech, "spoken_prefix", lambda sentences, durations, played: " ".join(sentences))
    s = _score("speak_barge_in")
    assert s.interrupt.parts["truncation_recorded"] == 0.0
    assert any("not recorded as truncated" in n for n in s.notes)
