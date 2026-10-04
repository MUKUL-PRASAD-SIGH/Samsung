"""End-to-end: the mock-mode suite is a regression gate, and MUTATION tests prove the harness can
actually tell a broken agent from a working one (a scorer that always says 100 is worthless)."""

import asyncio

import pytest

from agent import clock
from agent.coordination.idempotency import IdempotencyStore
from agent.coordination.state_machine import SessionState
from agent.coordinator import AgentCoordinator
from agent.eval.runner import run_scenario
from agent.eval.scenarios import BY_NAME, SUITE
from agent.eval.scorer import score_run
from agent.fast_path.intent_classifier import IntentClassifier
from agent.schemas.actions import ActionType


def _score_virtual(name):
    async def go():
        return score_run(await run_scenario(BY_NAME[name], mode="mock"))
    return clock.run_virtual(go())


async def score(name):
    """Run a scenario on a VIRTUAL clock (in a worker thread, because run_virtual owns its own event loop).

    These tests used to run 18 scenarios concurrently in real time on one loop. That made them depend on how fast the
    machine was: the MiniLM classification of ~20 simultaneous first messages (~5 ms each) delayed the 150 ms-spaced
    events of rapid_fire_corrections enough to coalesce two of them in the debounce window, which misaligned the scripted
    LLM and failed the gate. Virtual time makes the gate deterministic and independent of CPU speed."""
    return await asyncio.to_thread(_score_virtual, name)


NON_VOICE = [s.name for s in SUITE if not s.uses_voice]


# ------------------------------------------------------------------------- regression gate
async def test_healthy_agent_scores_high_on_every_scenario():
    # Independent coordinators, so run them concurrently (mock LLM: timing is unaffected).
    scores = [await score(name) for name in NON_VOICE]
    for s in scores:
        assert s.total >= 0.95, f"{s.name}: {s.total:.3f}\n" + "\n".join(s.notes)
        assert s.infra_errors == 0 and not s.timed_out, s.name
        assert s.safety.raw["duplicate_state_changes"] == 0 and s.safety.raw["trace_violations"] == 0, s.name


async def test_healthy_agent_has_no_stale_completions_and_fast_cancels():
    names = ("correction_mid_search", "correction_mid_booking", "stop_command", "rapid_fire_corrections", "explicit_interrupt_signal")
    for name, s in zip(names, [await score(n) for n in names]):
        assert s.interrupt.raw["stale_completions"] == 0 and s.interrupt.raw["stale_reruns"] == 0, name
        assert all(x < 0.5 for x in s.interrupt.raw["cancel_latency_s"]), (name, s.interrupt.raw["cancel_latency_s"])


# ---------------------------------------------------------------------------- mutations
async def test_MUTATION_cancellation_disabled_is_caught(monkeypatch):
    def bump_without_cancelling(self, reason="x"):
        self.epoch += 1
        return []
    monkeypatch.setattr(SessionState, "bump_epoch", bump_without_cancelling)

    s = await score("correction_mid_booking")
    assert s.interrupt.score == 0.0
    assert s.interrupt.raw["stale_completions"] >= 1
    assert s.task.parts["no_spurious_state_changes"] == 0.0   # the stale Mumbai booking really happened
    assert s.total < 0.6


async def test_MUTATION_idempotency_disabled_is_caught(monkeypatch):
    monkeypatch.setattr(IdempotencyStore, "exists", lambda self, key: False)

    s = await score("duplicate_booking_race")
    assert s.safety.raw["duplicate_state_changes"] >= 1
    assert s.safety.parts["no_duplicate_state_changes"] == 0.0
    assert s.task.parts["no_spurious_state_changes"] == 0.0


async def test_MUTATION_rephrased_duplicate_slips_past_raw_string_idempotency_and_is_caught(monkeypatch):
    # The real bug found by the live run: "Mumbai/Delhi" vs "BOM/DEL" hashed differently -> booked twice.
    import agent.coordination.idempotency as idem
    monkeypatch.setattr(idem, "canonical_mapping", lambda values: dict(values))

    s = await score("duplicate_booking_paraphrase")
    assert s.safety.raw["duplicate_state_changes"] >= 1
    assert s.task.parts["no_spurious_state_changes"] == 0.0


async def test_healthy_agent_blocks_the_rephrased_duplicate():
    s = await score("duplicate_booking_paraphrase")
    assert s.safety.raw["duplicate_state_changes"] == 0 and s.total >= 0.95, "\n".join(s.notes)


async def test_MUTATION_vision_result_never_re_plans_is_caught(monkeypatch):
    async def no_continuation(self, session, request_text, result):
        return None
    monkeypatch.setattr(AgentCoordinator, "_continue_after_observation", no_continuation)

    s = await score("vision_extract_arg")
    assert s.task.parts["tool_recall"] < 1.0          # search_flights never happens: the observation went nowhere
    assert any("search_flights" in n for n in s.task.notes)


async def test_MUTATION_uncancellable_vision_call_is_caught(monkeypatch):
    def bump_without_cancelling(self, reason="x"):
        self.epoch += 1
        return []
    monkeypatch.setattr(SessionState, "bump_epoch", bump_without_cancelling)

    s = await score("vision_interrupt")
    assert s.interrupt.score == 0.0
    assert s.task.parts["forbidden_calls_absent"] == 0.0   # analyze_frame ran to completion after "stop"


async def test_healthy_vision_scenarios_including_irrelevant_frame_score_high():
    names = ("vision_extract_arg", "vision_no_frame", "vision_interrupt", "vision_irrelevant_frame")
    for name, s in zip(names, [await score(n) for n in names]):
        assert s.total >= 0.95, f"{name}: {s.total:.3f}\n" + "\n".join(s.notes)


async def test_MUTATION_slots_not_applied_to_snapshot_is_caught(monkeypatch):
    monkeypatch.setattr(SessionState, "stage_call_slots", lambda self, call_id, values: {})

    s = await score("flight_search")
    assert s.task.parts["snapshot_accuracy"] < 1.0
    assert any("final snapshot slot" in n for n in s.task.notes)


async def test_MUTATION_interrupt_detector_blind_is_caught(monkeypatch):
    blind = {"is_interrupt": False, "needs_clarification": False, "confidence_score": 0.0, "decision": "continue"}
    monkeypatch.setattr(IntentClassifier, "classify_text", lambda self, text, **kw: blind)

    s = await score("correction_mid_search")
    assert s.interrupt.parts["cancel_coverage"] == 0.0
    assert s.interrupt.score == 0.0


async def test_MUTATION_snapshots_never_emitted_is_caught(monkeypatch):
    original = AgentCoordinator.emit_action

    async def drop_snapshots(self, action):
        if action.action_type == ActionType.STATE_SNAPSHOT:
            return
        await original(self, action)
    monkeypatch.setattr(AgentCoordinator, "emit_action", drop_snapshots)

    s = await score("correction_mid_search")
    assert s.task.parts["snapshot_accuracy"] == 0.0
    assert s.interrupt.parts["snapshot_updated"] == 0.0


async def test_MUTATION_no_acknowledgement_hurts_latency(monkeypatch):
    original = AgentCoordinator.emit_action

    async def drop_fillers(self, action):
        if action.action_type == ActionType.FILLER:
            return
        await original(self, action)
    monkeypatch.setattr(AgentCoordinator, "emit_action", drop_fillers)

    healthy_ack = 1.0
    s = await score("flight_search")
    assert s.latency.score < healthy_ack
    assert any("no quick acknowledgement" in n for n in s.latency.notes)


async def test_MUTATION_failures_swallowed_silently_is_caught(monkeypatch):
    original = AgentCoordinator.emit_action
    from agent.schemas.actions import SpokenResponseAction

    async def drop_replies(self, action):
        if isinstance(action, SpokenResponseAction):
            return
        await original(self, action)
    monkeypatch.setattr(AgentCoordinator, "emit_action", drop_replies)

    s = await score("tool_failure_reported")
    assert s.task.parts["failure_reported"] == 0.0


# --------------------------------------------------------------------------- voice (real audio)
@pytest.fixture(scope="module")
def whisper():
    from agent.multimodal.asr import ASRProcessor
    asr = ASRProcessor()
    asr._ensure_model_loaded()
    if not asr.is_loaded:
        pytest.skip("faster-whisper model unavailable")
    return asr


async def test_voice_scenarios_score_and_barge_in_beats_the_utterance_end(whisper):
    task = score_run(await run_scenario(BY_NAME["voice_task"], mode="mock", asr=whisper))
    assert task.task.score == 1.0 and task.total >= 0.85, "\n".join(task.notes)

    barge = score_run(await run_scenario(BY_NAME["voice_barge_in"], mode="mock", asr=whisper))
    assert barge.interrupt.raw["stale_completions"] == 0
    assert barge.interrupt.parts["cancel_coverage"] == 1.0
    lat = barge.interrupt.raw["cancel_latency_s"]
    assert lat and max(lat) < 3.0, lat        # cancelled while the sentence was still being spoken
    assert barge.total >= 0.8, "\n".join(barge.notes)
