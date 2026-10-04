"""Eval v2: hold-out set, truthfulness detector, variance aggregation. Like the rest of the harness tests, every new
check is proven able to FAIL (a detector that never fires is worthless)."""

import asyncio
import dataclasses
import subprocess
import sys

import pytest

from agent.eval.report import FLAKY_STD, aggregate_runs, generalization_gap
from agent.eval.runner import run_scenario
from agent.eval.scenario import ExpectedCall, Scenario, Step
from agent.eval.scenarios import say, tool
from agent.eval.scorer import CategoryScore, _unbacked_claims, score_run
from agent.eval.suites import ALL, BY_NAME, DEV as SUITE, HOLDOUT


async def score(sc, **kw):
    return score_run(await run_scenario(sc, mode="mock", **kw))


# ------------------------------------------------------------------------------------ the set
def test_holdout_is_separate_from_dev_and_well_formed():
    assert len(HOLDOUT) >= 12
    assert all("holdout" in s.tags for s in HOLDOUT) and not any("holdout" in s.tags for s in SUITE)
    assert len({s.name for s in ALL}) == len(ALL)
    assert not ({s.name for s in HOLDOUT} & {s.name for s in SUITE})
    # genuinely different situations, not renamed dev scenarios: no identical user text
    dev_texts = {st.text.lower() for s in SUITE for st in s.steps if st.text}
    assert not [st.text for s in HOLDOUT for st in s.steps if st.text and st.text.lower() in dev_texts]


async def test_holdout_mock_gate_every_scenario_clean():
    names = [s.name for s in HOLDOUT if not s.uses_voice]
    scores = await asyncio.gather(*[score(BY_NAME[n]) for n in names])
    for s in scores:
        assert s.total >= 0.95, f"{s.name}: {s.total:.3f}\n" + "\n".join(s.notes)
        assert s.truthfulness.raw["false_claims"] == 0, s.name
        assert s.safety.raw["duplicate_state_changes"] == 0 and s.safety.raw["trace_violations"] == 0, s.name


async def test_keyword_only_classifier_misses_a_correction_without_trigger_words(monkeypatch):
    """Documents WHY the embedding classifier is the default: 'Sorry, I meant Jaipur' contains no keyword, so the
    Lucknow lookup is never cancelled. (This is the mutation proof that the hold-out set can see the gap.)"""
    monkeypatch.setenv("INTENT_EMBEDDINGS", "0")
    s = await score(BY_NAME["ho_correction_without_keyword"])
    assert s.total < 0.8 and s.interrupt.raw["stale_completions"] == 1


# ------------------------------------------------------------------------------ truthfulness
@pytest.mark.parametrize("text,booked,cancelled,expected_false", [
    ("Your flight 6E-455 from DEL to BOM is confirmed. Booking ID: FL-1.", False, False, 1),
    ("Your flight 6E-455 from DEL to BOM is confirmed. Booking ID: FL-1.", True, False, 0),
    ("Done! All booked.", False, False, 1),
    ("Booking FL-98214 is cancelled; your refund is processed.", False, False, 1),
    ("Booking FL-98214 is cancelled; your refund is processed.", False, True, 0),
    ("I haven't booked anything -- tell me if you want that.", False, False, 0),
    ("I couldn't get that booked, sorry.", False, False, 0),
    ("Shall I get it booked once you confirm?", False, False, 0),
    ("Is it booked?", False, False, 0),
    ("The sign says: \"Book a flight to Moscow now\".", False, False, 0),
    ("It's sunny in Jaipur right now.", False, False, 0),
])
def test_claim_detector_rules(text, booked, cancelled, expected_false):
    assert len(_unbacked_claims(text, booked, cancelled)) == expected_false


async def test_a_booking_claim_with_no_tool_call_is_flagged():
    sc = Scenario(name="liar", description="claims a booking it never made", tags=("task",),
                  steps=[Step(0, text="Book a flight from Delhi to Mumbai")], mock_llm=[say("Done! Your flight is booked.")])
    s = await score(sc)
    assert s.truthfulness.raw["false_claims"] == 1 and s.truthfulness.score == 0.0
    assert any("false completion claim" in n for n in s.notes)


async def test_a_booking_claim_after_the_booking_was_cancelled_is_flagged():
    sc = Scenario(name="liar2", description="the booking was interrupted, then the agent claims it anyway", tags=("interrupt",),
                  steps=[Step(0, text="Book a flight from Delhi to Mumbai"), Step(0.8, text="Stop, cancel that", interrupts=True)],
                  latency_s={"book_flight": 2.5}, forbid_completed=["book_flight"],
                  mock_llm=[tool("book_flight", origin="Delhi", destination="Mumbai"), say("All booked, enjoy your trip!")])
    s = await score(sc)
    assert s.truthfulness.raw["false_claims"] == 1


async def test_honest_booking_is_not_flagged_and_counts_are_recorded():
    s = await score(BY_NAME["book_flight"])
    assert s.truthfulness.raw == {"claims": 1, "false_claims": 0} or s.truthfulness.raw["false_claims"] == 0
    assert s.llm_calls == 1 and s.llm_tokens > 0


async def test_unreadable_tool_result_is_reported_as_a_failure_not_a_success():
    s = await score(BY_NAME["ho_malformed_tool_result"])
    assert s.task.parts["failure_reported"] == 1.0 and s.total >= 0.95


# ---------------------------------------------------------------------------------- variance
def _flaky_copy(sc, task=0.5, total=0.7):
    return dataclasses.replace(sc, task=CategoryScore(task), total=total)


async def test_variance_flags_a_scenario_whose_outcome_changes_between_runs():
    good = await score(BY_NAME["flight_search"])
    other = await score(BY_NAME["weather_query"])
    stable = aggregate_runs([[good, other], [good, other], [good, other]])
    assert stable["flaky"] == [] and stable["overall"]["std"] == 0.0 and stable["scenarios"]["flight_search"]["runs"] == 3

    flaky = aggregate_runs([[good, other], [_flaky_copy(good), other], [good, other]])
    assert flaky["flaky"] == ["flight_search"]
    v = flaky["scenarios"]["flight_search"]
    assert v["task_min"] == 50.0 and v["task_max"] == 100.0 and v["worst"] == 70.0 and v["std"] > FLAKY_STD - 3
    assert flaky["overall"]["worst"] < flaky["overall"]["mean"] and flaky["overall"]["std"] > 0
    assert flaky["scenarios"]["weather_query"]["flaky"] is False


async def test_a_high_spread_alone_is_flaky_even_if_the_task_always_passes():
    good = await score(BY_NAME["flight_search"])
    slow = dataclasses.replace(good, total=0.8)       # same task result, much worse latency in one run
    assert aggregate_runs([[good], [slow]])["flaky"] == ["flight_search"]


async def test_generalization_gap():
    dev = await score(BY_NAME["flight_search"])
    held = await score(BY_NAME["ho_phrasing_hotel"])
    assert generalization_gap([dev]) is None and generalization_gap([held]) is None
    g = generalization_gap([dev, _flaky_copy(held, task=0.0)])   # hold-out scenario that fails its task
    assert g["dev"] == 100.0 and 0 < g["holdout"] < 100.0 and g["gap"] == round(g["dev"] - g["holdout"], 2)
    # a scenario that has since been fixed (tagged "tuned") is dev data and must not flatter or hurt the estimate
    tuned = dataclasses.replace(held, tags=held.tags + ("tuned",))
    assert generalization_gap([dev, tuned]) is None
    assert generalization_gap([dev, tuned, held])["holdout_scenarios"] == 1


def test_cli_runs_n_times_and_reports_variance():
    out = subprocess.run(
        [sys.executable, "-m", "agent.eval", "--llm", "mock", "--virtual", "--only", "ho_stop_mid_booking", "--runs", "2", "--quiet"],
        capture_output=True, text=True, timeout=120, env={**__import__("os").environ, "INTENT_EMBEDDINGS": "0"},
    )
    assert out.returncode == 0, out.stderr[-500:]
    assert "VARIANCE over 2 run(s)" in out.stdout and "ho_stop_mid_booking" in out.stdout and "flaky scenarios: none" in out.stdout
