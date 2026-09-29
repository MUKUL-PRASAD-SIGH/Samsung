"""The scorer's rules, pinned with hand-built traces (no coordinator involved)."""

import pytest

from agent.coordination.tool_router import ToolRouter
from agent.eval.environment import ExecRecord
from agent.eval.runner import RunRecord, StepTiming
from agent.eval.scenario import ExpectedCall, Scenario, Step
from agent.eval.scorer import (
    CANCEL_FULL_S, CANCEL_ZERO_S, WEIGHTS, _lin, score_interruption_recovery, score_latency,
    score_run, score_safety, score_task_completion, weighted_total,
)

ROUTER = ToolRouter()


def act(kind, ts, epoch=1, sid="s", **payload):
    return {"record_type": "action", "action_type": kind, "session_id": sid, "epoch": epoch, "timestamp": ts, "payload": payload}


def call(ts, call_id, tool="search_flights", args=None, epoch=1, sm=False):
    return act("tool_call", ts, epoch, call_id=call_id, tool_name=tool, arguments=args or {"origin": "DEL", "destination": "BOM"}, is_state_modifying=sm)


def cancel(ts, call_id, epoch=2):
    return act("tool_cancel", ts, epoch, call_id=call_id, tool_name="search_flights", reason="x")


def snap(ts, epoch, slots=None, in_flight=()):
    return act("state_snapshot", ts, epoch, slots=slots or {}, in_flight_calls=[{"call_id": c, "status": "running"} for c in in_flight], intent=None)


def ex(tool="search_flights", args=None, t0=0.0, t1=None, completed=False, cancelled=False, sm=False):
    return ExecRecord(tool=tool, args=args or {"origin": "DEL", "destination": "BOM"}, state_modifying=sm, t_start=t0, t_end=t1, completed=completed, cancelled=cancelled)


def run(steps, trace, execs=(), **scenario_kw):
    sc = Scenario(name="t", description="", tags=(), steps=steps, **scenario_kw)
    timings = [StepTiming(i, s.at_s, s.at_s, s.at_s) for i, s in enumerate(steps)]
    return RunRecord(sc, "mock", trace, list(execs), timings, [], ROUTER, t0=0.0, wall_s=1.0)


INTERRUPT_STEPS = [Step(0.0, text="find flights"), Step(1.0, text="actually goa", interrupts=True)]


# ---------------------------------------------------------------------------- formula
def test_linear_thresholds():
    assert _lin(0.1, 0.25, 2.0) == 1.0 and _lin(2.5, 0.25, 2.0) == 0.0
    assert _lin((0.25 + 2.0) / 2, 0.25, 2.0) == pytest.approx(0.5)


def test_weights_sum_to_one_and_renormalize_when_a_category_is_absent():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)
    assert weighted_total({"task": 1.0, "interrupt": None, "latency": 1.0, "safety": 1.0}) == pytest.approx(1.0)
    full = weighted_total({"task": 0.0, "interrupt": 1.0, "latency": 1.0, "safety": 1.0})
    assert full == pytest.approx(0.60)  # losing the 40% category costs exactly 40 points


# ----------------------------------------------------------------- interruption recovery
def _good_interrupt_trace():
    return [call(0.1, "c1"), cancel(1.1, "c1"), snap(1.15, 2, in_flight=())]


def test_clean_interrupt_scores_full():
    r = run(INTERRUPT_STEPS, _good_interrupt_trace(), [ex(t0=0.1, t1=1.1, cancelled=True)])
    ir = score_interruption_recovery(r)
    assert ir.score == pytest.approx(1.0) and ir.raw["cancel_latency_s"] == [0.1]


def test_missing_cancel_is_penalized_and_reported():
    r = run(INTERRUPT_STEPS, [call(0.1, "c1"), snap(1.5, 2)], [ex(t0=0.1, t1=2.0, completed=True)])
    ir = score_interruption_recovery(r)
    assert ir.parts["cancel_coverage"] == 0.0 and ir.parts["no_stale_completion"] == 0.0
    assert ir.score == 0.0, "a missed interrupt must not collect credit for the things that trivially went 'right'"
    assert any("never cancelled" in n for n in ir.notes)


def test_cancel_speed_is_graded_linearly():
    mid = 1.0 + (CANCEL_FULL_S + CANCEL_ZERO_S) / 2
    r = run(INTERRUPT_STEPS, [call(0.1, "c1"), cancel(mid, "c1"), snap(mid + 0.01, 2)], [ex(t0=0.1, t1=mid, cancelled=True)])
    assert score_interruption_recovery(r).parts["cancel_speed"] == pytest.approx(0.5, abs=0.01)


def test_cancelled_call_that_still_completes_is_a_stale_completion():
    r = run(INTERRUPT_STEPS, _good_interrupt_trace(), [ex(t0=0.1, t1=1.6, completed=True, sm=True)])
    ir = score_interruption_recovery(r)
    assert ir.raw["stale_completions"] == 1 and ir.parts["no_stale_completion"] == 0.0
    assert ir.score == 0.0  # the cancel was emitted but the work was not actually stopped
    assert any("state-changing" in n for n in ir.notes)


def test_redispatching_the_cancelled_call_is_a_stale_rerun():
    trace = _good_interrupt_trace() + [call(1.2, "c2", epoch=2)]  # identical tool+args as the cancelled c1
    ir = score_interruption_recovery(run(INTERRUPT_STEPS, trace, [ex(t0=0.1, t1=1.1, cancelled=True)]))
    assert ir.raw["stale_reruns"] == 1 and ir.parts["no_stale_rerun"] == 0.0


def test_rerun_is_fine_when_the_scenario_expects_that_exact_call():
    trace = _good_interrupt_trace() + [call(1.2, "c2", epoch=2)]
    r = run(INTERRUPT_STEPS, trace, [ex(t0=0.1, t1=1.1, cancelled=True)],
            expected_calls=[ExpectedCall("search_flights", {"destination": "BOM"})])
    assert score_interruption_recovery(r).raw["stale_reruns"] == 0


def test_no_updated_snapshot_after_the_interrupt_is_penalized():
    r = run(INTERRUPT_STEPS, [call(0.1, "c1"), cancel(1.1, "c1")], [ex(t0=0.1, t1=1.1, cancelled=True)])
    ir = score_interruption_recovery(r)
    assert ir.parts["snapshot_updated"] == 0.0


def test_snapshot_still_listing_the_cancelled_call_does_not_count_as_updated():
    r = run(INTERRUPT_STEPS, [call(0.1, "c1"), cancel(1.1, "c1"), snap(1.2, 2, in_flight=("c1",))], [ex(t0=0.1, t1=1.1, cancelled=True)])
    assert score_interruption_recovery(r).parts["snapshot_updated"] == 0.0


def test_interrupt_with_nothing_in_flight_is_vacuously_fine_and_no_interrupts_means_not_applicable():
    r = run(INTERRUPT_STEPS, [call(0.1, "c1")], [ex(t0=0.1, t1=0.6, completed=True)])  # finished before the interrupt
    ir = score_interruption_recovery(r)
    assert ir.score == 1.0 and ir.raw["interrupts_with_nothing_in_flight"] == 1
    assert score_interruption_recovery(run([Step(0, text="hi")], [])) is None


# ------------------------------------------------------------------------------- safety
def test_overlapping_identical_state_changes_are_duplicates():
    execs = [ex("book_flight", t0=0.0, t1=None, sm=True), ex("book_flight", t0=0.5, t1=2.0, completed=True, sm=True)]
    s = score_safety(run([Step(0, text="x")], [], execs))
    assert s.raw["duplicate_state_changes"] == 1 and s.parts["no_duplicate_state_changes"] == 0.0


def test_retry_after_a_genuine_cancel_is_not_a_duplicate():
    execs = [ex("book_flight", t0=0.0, t1=0.4, cancelled=True, sm=True), ex("book_flight", t0=0.5, t1=2.0, completed=True, sm=True)]
    assert score_safety(run([Step(0, text="x")], [], execs)).raw["duplicate_state_changes"] == 0


def test_different_arguments_are_not_duplicates():
    execs = [ex("book_flight", {"origin": "DEL", "destination": "BOM"}, 0.0, 2.0, completed=True, sm=True),
             ex("book_flight", {"origin": "DEL", "destination": "GOI"}, 0.5, 2.5, completed=True, sm=True)]
    assert score_safety(run([Step(0, text="x")], [], execs)).raw["duplicate_state_changes"] == 0


def test_schema_invalid_arguments_and_trace_violations_are_caught():
    bad_call = call(0.1, "c1", args={"origin": "DEL"})  # search_flights requires destination
    s = score_safety(run([Step(0, text="x")], [bad_call, cancel(0.2, "ghost", epoch=1)]))
    assert s.raw["invalid_payloads"] == 1 and s.raw["trace_violations"] >= 1
    assert s.score < 1.0


def test_epoch_regression_is_a_trace_violation():
    s = score_safety(run([Step(0, text="x")], [snap(0.1, 3), snap(0.2, 2)]))
    assert any("epoch regression" in n for n in s.notes)


# ------------------------------------------------------------------------ task completion
def test_expected_call_matches_with_aliases_and_extra_readonly_calls_are_ok():
    r = run([Step(0, text="x")], [], [ex(args={"origin": "delhi", "destination": "BOM"}, t1=1, completed=True),
                                       ex("check_weather", {"city": "x"}, t1=1, completed=True)],
            expected_calls=[ExpectedCall("search_flights", {"origin": ("Delhi", "DEL"), "destination": ("Mumbai", "BOM")})])
    assert score_task_completion(r).parts["tool_recall"] == 1.0


def test_missing_expected_call_and_spurious_booking():
    r = run([Step(0, text="x")], [], [ex("book_flight", {"origin": "DEL", "destination": "NYC"}, t1=1, completed=True, sm=True)],
            expected_calls=[ExpectedCall("book_flight", {"destination": ("Chicago", "ORD")})])
    tc = score_task_completion(r)
    assert tc.parts["tool_recall"] == 0.0 and tc.parts["no_spurious_state_changes"] == 0.0
    assert any("unexpected state-changing" in n for n in tc.notes)


def test_stale_slot_value_in_final_snapshot_zeroes_snapshot_accuracy():
    trace = [snap(1.0, 2, slots={"destination": "Mumbai"})]
    r = run([Step(0, text="x")], trace, expected_slots={"destination": ("Goa", "GOI")}, forbidden_slot_values={"destination": ["Mumbai"]})
    tc = score_task_completion(r)
    assert tc.parts["snapshot_accuracy"] == 0.0 and any("stale value" in n for n in tc.notes)


def test_snapshot_accuracy_uses_the_LAST_emitted_snapshot():
    trace = [snap(1.0, 1, slots={}), snap(2.0, 1, slots={"city": "Goa"})]
    assert score_task_completion(run([Step(0, text="x")], trace, expected_slots={"city": ("Goa", "GOI")})).parts["snapshot_accuracy"] == 1.0
    trace = [snap(1.0, 1, slots={"city": "Goa"}), snap(2.0, 1, slots={})]   # a later, emptier snapshot wins
    assert score_task_completion(run([Step(0, text="x")], trace, expected_slots={"city": ("Goa", "GOI")})).parts["snapshot_accuracy"] == 0.0


def test_forbidden_completion_and_failure_notice_and_unanswered_turn():
    r = run([Step(0, text="x")], [], [ex("search_hotels", {"city": "Goa"}, t1=1, completed=True)],
            forbid_completed=["search_hotels"], expect_failure_notice=True)
    tc = score_task_completion(r)
    assert tc.parts["forbidden_calls_absent"] == 0.0 and tc.parts["failure_reported"] == 0.0
    assert tc.parts["final_turn_answered"] == 0.0

    ok = run([Step(0, text="x")], [act("spoken_response", 1.0, text="Sorry, I couldn't complete search flights.")], expect_failure_notice=True)
    assert score_task_completion(ok).parts["failure_reported"] == 1.0


# --------------------------------------------------------------------------------- latency
def test_latency_is_graded_from_user_done_to_ack_and_reply():
    fast = run([Step(0, text="x")], [act("filler", 0.1, text="ok"), act("spoken_response", 1.0, text="done")])
    slow = run([Step(0, text="x")], [act("filler", 1.9, text="ok"), act("spoken_response", 7.5, text="done")])
    lf, ls = score_latency(fast), score_latency(slow)
    assert lf.score == pytest.approx(1.0) and ls.score < 0.15
    assert lf.raw["first_ack_s"] == [0.1] and lf.raw["first_reply_s"] == [1.0]


def test_earlier_turns_are_not_penalized_for_a_reply_that_lands_in_the_next_window():
    steps = [Step(0.0, text="a"), Step(0.5, text="b")]
    trace = [act("filler", 0.1, text="ok"), act("filler", 0.6, text="ok"), act("spoken_response", 2.0, text="answer")]
    lat = score_latency(run(steps, trace))
    assert not any("turn 1" in n and "no substantive" in n for n in lat.notes)
    assert lat.score > 0.9


def test_final_turn_with_no_reply_is_penalized_unless_something_later_interrupted_it():
    lat = score_latency(run([Step(0, text="x")], [act("filler", 0.1, text="ok")]))
    assert any("no substantive reply" in n for n in lat.notes)
    interrupted = run([Step(0, text="x"), Step(1, kind="interrupt", interrupts=True, is_turn=False)], [act("filler", 0.1, text="ok")])
    assert not any("no substantive reply" in n for n in score_latency(interrupted).notes)


def test_score_run_rolls_everything_up():
    r = run(INTERRUPT_STEPS, _good_interrupt_trace() + [act("filler", 0.1, text="ok"), act("spoken_response", 1.5, text="done")],
            [ex(t0=0.1, t1=1.1, cancelled=True)])
    s = score_run(r)
    assert s.interrupt is not None and 0.0 <= s.total <= 1.0
