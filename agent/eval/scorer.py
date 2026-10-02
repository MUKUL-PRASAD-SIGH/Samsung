"""Scores a RunRecord on the spec's four weighted categories, from the trace + ground-truth executions.

    Task Completion 40% | Interruption Recovery 35% | Response Latency 15% | Safety & Protocol 10%

The spec fixes the weights but NOT the formulas, so every threshold below is a PROXY chosen to be
sensible and transparent -- replace the constants when the official rubric drops. Raw measurements
(seconds, counts) are always reported next to the scores, so the numbers stay meaningful even if a
proxy formula is wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from agent.coordination.canonical import canonical_mapping
from agent.eval.environment import ExecRecord
from agent.eval.runner import RunRecord, StepTiming
from agent.eval.scenario import Acceptable, ExpectedCall, Scenario

WEIGHTS = {"task": 0.40, "interrupt": 0.35, "latency": 0.15, "safety": 0.10}

# ---- PROXY thresholds (seconds) ---------------------------------------------------------------
CANCEL_FULL_S, CANCEL_ZERO_S = 0.25, 2.0   # interrupt-onset -> cancel action
ACK_FULL_S, ACK_ZERO_S = 0.3, 2.0          # user done -> first filler/ack
REPLY_FULL_S, REPLY_ZERO_S = 2.0, 8.0      # user done -> first substantive reply

SPOKEN = ("filler", "spoken_response", "clarification")
SUBSTANTIVE = ("spoken_response", "clarification")


# ------------------------------------------------------------------------------------- helpers
def _lin(value: float, full: float, zero: float) -> float:
    """1.0 at/below `full`, 0.0 at/above `zero`, linear in between."""
    if value <= full:
        return 1.0
    if value >= zero:
        return 0.0
    return (zero - value) / (zero - full)


def _norm(v: Any) -> str:
    return str(v).strip().lower()


def _accepts(actual: Any, expected: Acceptable) -> bool:
    options = expected if isinstance(expected, tuple) else (expected,)
    return actual is not None and _norm(actual) in {_norm(o) for o in options}


def _args_match(actual: Dict[str, Any], expected: Dict[str, Acceptable]) -> bool:
    return all(_accepts(actual.get(k), want) for k, want in expected.items())


def _tool_match(tool: str, want) -> bool:
    return tool in (want if isinstance(want, tuple) else (want,))


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 1.0


def _canon(args: Dict[str, Any]) -> str:
    # Same canonical form the idempotency layer uses, so "Mumbai" and "BOM" are recognized as the same
    # booking. Comparing raw strings let a genuine double booking score as "zero duplicates".
    return "|".join(f"{k}={_norm(v)}" for k, v in sorted(canonical_mapping(args).items()))


@dataclass
class Dispatch:
    call_id: str
    tool: str
    args: Dict[str, Any]
    ts: float
    epoch: int
    state_modifying: bool
    cancel_ts: Optional[float] = None
    execution: Optional[ExecRecord] = None


def _actions(trace, *types):
    return [r for r in trace if r["record_type"] == "action" and r["action_type"] in types]


def _dispatches(run: RunRecord) -> List[Dispatch]:
    out: List[Dispatch] = []
    for r in _actions(run.trace, "tool_call"):
        p = r["payload"]
        out.append(Dispatch(p["call_id"], p["tool_name"], p["arguments"], r["timestamp"], r["epoch"], bool(p["is_state_modifying"])))
    by_id = {d.call_id: d for d in out}
    for r in _actions(run.trace, "tool_cancel"):
        d = by_id.get(r["payload"]["call_id"])
        if d and d.cancel_ts is None:
            d.cancel_ts = r["timestamp"]
    # Link each dispatch to the handler execution it caused (same tool+args, started right after dispatch).
    free = list(run.executions)
    for d in sorted(out, key=lambda x: x.ts):
        candidates = [e for e in free if e.tool == d.tool and _canon(e.args) == _canon(d.args) and e.t_start >= d.ts - 0.05]
        if candidates:
            e = min(candidates, key=lambda x: x.t_start)
            d.execution = e
            free.remove(e)
    return out


def _last_snapshot(run: RunRecord) -> Optional[Dict[str, Any]]:
    snaps = _actions(run.trace, "state_snapshot")
    return snaps[-1] if snaps else None


@dataclass
class CategoryScore:
    score: float
    parts: Dict[str, float] = field(default_factory=dict)   # named sub-scores that were averaged
    raw: Dict[str, Any] = field(default_factory=dict)       # raw measurements (seconds, counts)
    notes: List[str] = field(default_factory=list)          # human-readable failures


# ---------------------------------------------------------------------------- task completion
def score_task_completion(run: RunRecord) -> CategoryScore:
    sc, router = run.scenario, run.router
    parts: Dict[str, float] = {}
    notes: List[str] = []
    completed = [e for e in run.executions if e.completed]

    # Expected executions that survived to completion, matched without double-counting.
    used: set = set()
    if sc.expected_calls:
        hits = 0
        for want in sc.expected_calls:
            matched = 0
            for i, e in enumerate(completed):
                if i in used or not _tool_match(e.tool, want.tool) or not _args_match(e.args, want.args):
                    continue
                used.add(i)
                matched += 1
                if matched >= want.count:
                    break
            if matched >= want.count:
                hits += 1
            else:
                notes.append(f"missing completed call {want.tool} {want.args} (x{want.count}); completed: "
                             f"{[(e.tool, e.args) for e in completed] or 'none'}")
        parts["tool_recall"] = hits / len(sc.expected_calls)

    spurious = [e for i, e in enumerate(completed) if i not in used and e.state_modifying]
    if spurious or any(router.is_state_modifying(t) for w in sc.expected_calls for t in (w.tool if isinstance(w.tool, tuple) else (w.tool,))):
        parts["no_spurious_state_changes"] = 0.0 if spurious else 1.0
        for e in spurious:
            notes.append(f"unexpected state-changing call completed: {e.tool} {e.args}")

    if sc.forbid_completed:
        bad = [e for e in completed if e.tool in sc.forbid_completed]
        parts["forbidden_calls_absent"] = 0.0 if bad else 1.0
        for e in bad:
            notes.append(f"call that should not have completed did: {e.tool} {e.args}")

    snap = _last_snapshot(run)
    if sc.expected_slots or sc.forbidden_slot_values:
        slots = (snap or {}).get("payload", {}).get("slots", {}) if snap else {}
        if sc.expected_slots:
            ok = [k for k, want in sc.expected_slots.items() if _accepts(slots.get(k), want)]
            frac = len(ok) / len(sc.expected_slots)
            for k in sc.expected_slots:
                if k not in ok:
                    notes.append(f"final snapshot slot {k}={slots.get(k)!r}, expected {sc.expected_slots[k]!r}")
            stale = [(k, v) for k, vals in sc.forbidden_slot_values.items() for v in vals if _norm(slots.get(k)) == _norm(v)]
            for k, v in stale:
                notes.append(f"stale value {v!r} survived in final snapshot slot {k}")
            parts["snapshot_accuracy"] = 0.0 if stale else frac
        else:
            stale = [(k, v) for k, vals in sc.forbidden_slot_values.items() for v in vals if _norm(slots.get(k)) == _norm(v)]
            parts["snapshot_accuracy"] = 0.0 if stale else 1.0

    turns = [t for t, s in zip(run.step_timings, sc.steps) if s.is_turn]
    if turns:
        last = turns[-1]
        replies = [r for r in _actions(run.trace, *SPOKEN) if r["timestamp"] >= last.t_done]
        substantive = [r for r in replies if r["action_type"] in SUBSTANTIVE]
        answered = bool(substantive) or (not sc.expected_calls and bool(replies))
        parts["final_turn_answered"] = 1.0 if answered else 0.0
        if not answered:
            notes.append("the final user turn never received a reply")

    if sc.expect_failure_notice:
        told = any(any(w in r["payload"].get("text", r["payload"].get("question", "")).lower() for w in ("couldn't", "sorry", "snag", "failed"))
                   for r in _actions(run.trace, *SUBSTANTIVE))
        parts["failure_reported"] = 1.0 if told else 0.0
        if not told:
            notes.append("a failing tool was never reported to the user")

    raw = {"completed_calls": [(e.tool, e.args) for e in completed], "final_slots": (snap or {}).get("payload", {}).get("slots") if snap else None}
    return CategoryScore(_mean(list(parts.values())), parts, raw, notes)


# --------------------------------------------------------------------------------- interruption
def score_interruption_recovery(run: RunRecord) -> Optional[CategoryScore]:
    sc = run.scenario
    stimuli: List[Tuple[int, StepTiming]] = [(i, t) for i, (t, s) in enumerate(zip(run.step_timings, sc.steps)) if s.interrupts]
    if not stimuli:
        return None

    dispatches = _dispatches(run)
    snapshots = _actions(run.trace, "state_snapshot")
    expected_specs = sc.expected_calls
    per_stimulus: List[float] = []
    raw: Dict[str, Any] = {"cancel_latency_s": [], "stale_completions": 0, "stale_reruns": 0, "interrupts_with_nothing_in_flight": 0}
    notes: List[str] = []
    parts_acc: Dict[str, List[float]] = {}

    for idx, timing in stimuli:
        T = timing.t_onset
        stale = [d for d in dispatches
                 if d.ts < T and not (d.cancel_ts is not None and d.cancel_ts < T)
                 and not (d.execution and d.execution.completed and d.execution.t_end is not None and d.execution.t_end < T)]
        if not stale:
            raw["interrupts_with_nothing_in_flight"] += 1
            per_stimulus.append(1.0)
            continue

        parts: Dict[str, float] = {}
        cancelled = [d for d in stale if d.cancel_ts is not None and d.cancel_ts >= T - 0.05]
        parts["cancel_coverage"] = len(cancelled) / len(stale)
        for d in stale:
            if d not in cancelled:
                notes.append(f"stale call {d.tool} {d.args} was never cancelled after the interrupt")
        if cancelled:
            lat = [d.cancel_ts - T for d in cancelled]
            raw["cancel_latency_s"].extend(round(x, 3) for x in lat)
            parts["cancel_speed"] = _mean([_lin(x, CANCEL_FULL_S, CANCEL_ZERO_S) for x in lat])

        stale_done = [d for d in stale if d.execution and d.execution.completed and d.execution.t_end is not None and d.execution.t_end > T]
        raw["stale_completions"] += len(stale_done)
        parts["no_stale_completion"] = 1.0 - len(stale_done) / len(stale)
        for d in stale_done:
            notes.append(f"cancelled/stale call {d.tool} {d.args} still ran to completion"
                         + (" (state-changing!)" if (d.state_modifying or d.execution.state_modifying) else ""))

        reruns = 0
        for d in stale:
            for later in dispatches:
                if later.ts > T and later.call_id != d.call_id and later.tool == d.tool and _canon(later.args) == _canon(d.args):
                    if not any(_tool_match(later.tool, w.tool) and _args_match(later.args, w.args) for w in expected_specs):
                        reruns += 1
                        notes.append(f"stale call {d.tool} {d.args} was re-dispatched after the interrupt")
        raw["stale_reruns"] += reruns
        parts["no_stale_rerun"] = 0.0 if reruns else 1.0

        after = max([d.cancel_ts for d in cancelled], default=T)
        fresh = [s for s in snapshots if s["timestamp"] >= after - 0.05
                 and all(s["epoch"] > d.epoch for d in stale)
                 and not any(c["call_id"] in {d.call_id for d in stale} and c["status"] in ("pending", "running")
                             for c in s["payload"].get("in_flight_calls", []))]
        parts["snapshot_updated"] = 1.0 if fresh else 0.0
        if not fresh:
            notes.append("no updated state snapshot (new epoch, stale calls removed) was emitted after the interrupt")

        for k, v in parts.items():
            parts_acc.setdefault(k, []).append(v)
        # Gate on EFFECTIVE cancellation (cancel emitted AND the work really stopped). Without this a
        # missed interrupt still collected free credit for "no stale re-run" / "snapshot present".
        effective = [d for d in cancelled if d not in stale_done]
        parts["effective_cancellation"] = len(effective) / len(stale)
        parts_acc.setdefault("effective_cancellation", []).append(parts["effective_cancellation"])
        per_stimulus.append(min(_mean([v for k, v in parts.items() if k != "effective_cancellation"]), parts["effective_cancellation"]))

    return CategoryScore(_mean(per_stimulus), {k: _mean(v) for k, v in parts_acc.items()}, raw, notes)


# ---------------------------------------------------------------------------------- latency
def score_latency(run: RunRecord) -> CategoryScore:
    sc = run.scenario
    turns = [(t, s) for t, s in zip(run.step_timings, sc.steps) if s.is_turn]
    per_turn: List[float] = []
    raw: Dict[str, Any] = {"first_ack_s": [], "first_reply_s": []}
    notes: List[str] = []
    spoken = _actions(run.trace, *SPOKEN)
    for i, (t, s) in enumerate(turns):
        end = turns[i + 1][0].t_sent if i + 1 < len(turns) else float("inf")
        # A reply is REQUIRED only for the final turn, and only if nothing later interrupted it.
        # Earlier turns are scored on a reply if one arrives in their window, never penalized for
        # lacking one (their answer may legitimately land during the next turn).
        superseded = any(s2.interrupts and t2.t_sent > t.t_sent for t2, s2 in zip(run.step_timings, sc.steps))
        require_reply = i == len(turns) - 1 and not superseded
        window = [r for r in spoken if t.t_done <= r["timestamp"] < end]
        ack = next((r for r in window if r["action_type"] == "filler"), None)
        reply = next((r for r in window if r["action_type"] in SUBSTANTIVE), None)
        comps: List[float] = []
        if ack:
            raw["first_ack_s"].append(round(ack["timestamp"] - t.t_done, 3))
            comps.append(_lin(ack["timestamp"] - t.t_done, ACK_FULL_S, ACK_ZERO_S))
        else:
            comps.append(0.0)
            notes.append(f"turn {i + 1}: no quick acknowledgement before the next turn")
        if reply:
            raw["first_reply_s"].append(round(reply["timestamp"] - t.t_done, 3))
            comps.append(_lin(reply["timestamp"] - t.t_done, REPLY_FULL_S, REPLY_ZERO_S))
        elif require_reply:
            comps.append(0.0)
            notes.append(f"turn {i + 1}: no substantive reply")
        per_turn.append(_mean(comps))
    return CategoryScore(_mean(per_turn), {"per_turn_mean": _mean(per_turn)}, raw, notes)


# ------------------------------------------------------------------------------------ safety
def score_safety(run: RunRecord) -> CategoryScore:
    notes: List[str] = []
    # 1) duplicate state-changing executions: an identical call started while an earlier identical one
    #    was still live or had completed. (Retrying after a genuine cancel is legitimate.)
    sm = sorted([e for e in run.executions if e.state_modifying], key=lambda e: e.t_start)
    dups = 0
    for j, e in enumerate(sm):
        for prior in sm[:j]:
            if prior.tool == e.tool and _canon(prior.args) == _canon(e.args):
                live = prior.t_end is None or prior.t_end > e.t_start
                if prior.completed or (live and not prior.cancelled):
                    dups += 1
                    notes.append(f"duplicate state-changing call {e.tool} {e.args}")
                    break

    # 2) payload validity: every dispatched call's arguments must satisfy the tool's schema.
    dispatches = _dispatches(run)
    invalid = 0
    for d in dispatches:
        try:
            run.router.validate_call(d.tool, d.args)
        except Exception as ex:
            invalid += 1
            notes.append(f"schema-invalid arguments for {d.tool}: {str(ex)[:80]}")
    schema_ok = 1.0 - invalid / len(dispatches) if dispatches else 1.0

    # 3) trace invariants, checked post-hoc without raising.
    violations = _trace_violations(run.trace)
    notes.extend(violations)

    parts = {"no_duplicate_state_changes": 0.0 if dups else 1.0, "valid_payloads": schema_ok, "trace_invariants": 0.0 if violations else 1.0}
    score = 0.5 * parts["no_duplicate_state_changes"] + 0.25 * parts["valid_payloads"] + 0.25 * parts["trace_invariants"]
    return CategoryScore(score, parts, {"duplicate_state_changes": dups, "invalid_payloads": invalid, "trace_violations": len(violations)}, notes)


# ------------------------------------------------------------------------------- truthfulness
# Quality proxy for the spec's "truthfulness" multiplier (the multiplier itself is not computed here): a reply may only
# claim a booking/cancellation that a COMPLETED tool call actually backs. Deterministic, so it can gate CI.
_BOOK_CLAIM = re.compile(r"\b(?:confirmed|booked|reserved)\b|\bbooking id\b|\breservation id\b", re.I)
_CANCEL_CLAIM = re.compile(r"\b(?:is|was|has been|have been)\s+cancel+ed\b|\brefund\b", re.I)
_NEG_WORDS = re.compile(r"n't|\b(?:not|never|no|without|unless|if|once|before|whether)\b", re.I)
_BOOKING_TOOLS = ("book_flight", "book_hotel")


def _unbacked_claims(text: str, backed_booking: bool, backed_cancel: bool) -> List[str]:
    """Claims in `text` that no completed execution supports (negated/hypothetical mentions are not claims)."""
    out: List[str] = []
    for label, pattern, backed in (("booking", _BOOK_CLAIM, backed_booking), ("cancellation", _CANCEL_CLAIM, backed_cancel)):
        if backed:
            continue
        for m in pattern.finditer(text):
            clause = re.split(r"[.!?;]", text[: m.start()])[-1]   # the sentence up to the claim
            tail = re.search(r"[.!?;]", text[m.end():])
            is_question = bool(tail) and tail.group(0) == "?"       # "Shall I get it booked?" asks, it doesn't claim
            if not _NEG_WORDS.search(clause) and not is_question:
                out.append(f"{label} claim {m.group(0)!r}")
                break
    return out


def score_truthfulness(run: RunRecord) -> CategoryScore:
    """Count replies claiming a completed booking/cancellation with no completed matching call before them."""
    done = [e for e in run.executions if e.completed and e.t_end is not None]
    claims = false_claims = 0
    notes: List[str] = []
    for r in _actions(run.trace, *SUBSTANTIVE):
        text = r["payload"].get("text") or r["payload"].get("question") or ""
        ts = r["timestamp"]
        backed_booking = any(e.tool in _BOOKING_TOOLS and e.t_end <= ts + 0.05 for e in done)
        backed_cancel = any(e.tool == "cancel_booking" and e.t_end <= ts + 0.05 for e in done)
        mentioned = [p for p in (_BOOK_CLAIM, _CANCEL_CLAIM) if p.search(text)]
        claims += len(mentioned)
        for c in _unbacked_claims(text, backed_booking, backed_cancel):
            false_claims += 1
            notes.append(f"false completion claim ({c}) in reply {text[:90]!r} with no completed tool call behind it")
    return CategoryScore(0.0 if false_claims else 1.0, {"no_false_claims": 0.0 if false_claims else 1.0},
                         {"claims": claims, "false_claims": false_claims}, notes)


def _trace_violations(trace: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    epochs: Dict[str, int] = {}
    known: Dict[str, set] = {}
    for r in trace:
        if r["record_type"] != "action":
            continue
        sid = r["session_id"]
        if not r.get("timestamp") or not sid:
            out.append(f"trace record missing timestamp/session: {r['action_type']}")
        if r["epoch"] < epochs.get(sid, 0):
            out.append(f"epoch regression {epochs[sid]} -> {r['epoch']} at {r['action_type']}")
        epochs[sid] = max(epochs.get(sid, 0), r["epoch"])
        ids = known.setdefault(sid, set())
        if r["action_type"] == "tool_call":
            cid = r["payload"].get("call_id")
            if not cid:
                out.append("tool call without call_id")
            elif cid in ids:
                out.append(f"duplicate call_id {cid}")
            ids.add(cid)
        elif r["action_type"] == "tool_cancel" and r["payload"].get("call_id") not in ids:
            out.append(f"cancel references unknown call_id {r['payload'].get('call_id')}")
    return out


# ---------------------------------------------------------------------------------- roll-up
@dataclass
class ScenarioScore:
    name: str
    tags: Tuple[str, ...]
    task: CategoryScore
    interrupt: Optional[CategoryScore]
    latency: CategoryScore
    safety: CategoryScore
    total: float
    infra_errors: int
    timed_out: bool
    wall_s: float
    llm_errors: List[str] = field(default_factory=list)
    truthfulness: Optional[CategoryScore] = None   # quality proxy; reported, not part of the weighted total
    llm_calls: int = 0
    llm_tokens: int = 0                            # provider-reported when available, else estimated

    @property
    def notes(self) -> List[str]:
        out: List[str] = []
        for label, cat in (("task", self.task), ("interrupt", self.interrupt), ("latency", self.latency), ("safety", self.safety),
                           ("truthfulness", self.truthfulness)):
            if cat:
                out += [f"[{label}] {n}" for n in cat.notes]
        return out


def weighted_total(cats: Dict[str, Optional[float]]) -> float:
    """Weighted mean over the categories that apply (renormalized when e.g. no interrupts occurred)."""
    active = {k: v for k, v in cats.items() if v is not None}
    weight = sum(WEIGHTS[k] for k in active)
    return sum(WEIGHTS[k] * v for k, v in active.items()) / weight if weight else 0.0


def score_run(run: RunRecord) -> ScenarioScore:
    task = score_task_completion(run)
    interrupt = score_interruption_recovery(run)
    latency = score_latency(run)
    safety = score_safety(run)
    total = weighted_total({"task": task.score, "interrupt": interrupt.score if interrupt else None,
                            "latency": latency.score, "safety": safety.score})
    return ScenarioScore(run.scenario.name, run.scenario.tags, task, interrupt, latency, safety, total,
                         run.infra_errors, run.timed_out, run.wall_s, [c.error for c in run.llm_calls if c.error],
                         truthfulness=score_truthfulness(run), llm_calls=len(run.llm_calls),
                         llm_tokens=sum(c.est_tokens for c in run.llm_calls))
