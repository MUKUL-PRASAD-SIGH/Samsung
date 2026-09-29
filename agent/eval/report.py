"""Suite roll-up, console report, and JSON output."""

from __future__ import annotations

import statistics
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from agent.eval.scorer import WEIGHTS, ScenarioScore, weighted_total


def _mean(xs: List[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def summarize(scores: List[ScenarioScore]) -> Dict[str, Any]:
    cats = {
        "task": _mean([s.task.score for s in scores]),
        "interrupt": _mean([s.interrupt.score for s in scores if s.interrupt]),
        "latency": _mean([s.latency.score for s in scores]),
        "safety": _mean([s.safety.score for s in scores]),
    }
    cancel = [x for s in scores if s.interrupt for x in s.interrupt.raw.get("cancel_latency_s", [])]
    ack = [x for s in scores for x in s.latency.raw.get("first_ack_s", [])]
    reply = [x for s in scores for x in s.latency.raw.get("first_reply_s", [])]
    med = lambda xs: round(statistics.median(xs), 3) if xs else None
    return {
        "overall": weighted_total(cats),
        "categories": cats,
        "scenarios": len(scores),
        "infra_degraded": [s.name for s in scores if s.infra_errors],
        "raw": {
            "median_cancel_latency_s": med(cancel), "worst_cancel_latency_s": round(max(cancel), 3) if cancel else None,
            "median_first_ack_s": med(ack), "median_first_reply_s": med(reply),
            "stale_completions": sum(s.interrupt.raw.get("stale_completions", 0) for s in scores if s.interrupt),
            "stale_reruns": sum(s.interrupt.raw.get("stale_reruns", 0) for s in scores if s.interrupt),
            "duplicate_state_changes": sum(s.safety.raw["duplicate_state_changes"] for s in scores),
            "invalid_payloads": sum(s.safety.raw["invalid_payloads"] for s in scores),
            "trace_violations": sum(s.safety.raw["trace_violations"] for s in scores),
        },
    }


def _pct(x: Optional[float]) -> str:
    return "  -- " if x is None else f"{x * 100:5.1f}"


def format_report(scores: List[ScenarioScore], mode: str, show_notes: bool = True) -> str:
    lines = [f"\nEVAL RESULTS  (llm={mode}; scores are 0-100; interruption shown only where a scenario interrupts)",
             f"{'scenario':28s} {'task':>6s} {'intr':>6s} {'lat':>6s} {'safe':>6s} {'TOTAL':>6s}  flags",
             "-" * 78]
    for s in scores:
        flags = []
        if s.infra_errors:
            flags.append(f"INFRA({s.infra_errors} LLM errors)")
        if s.timed_out:
            flags.append("TIMED-OUT")
        lines.append(f"{s.name:28s} {_pct(s.task.score)} {_pct(s.interrupt.score if s.interrupt else None)} "
                     f"{_pct(s.latency.score)} {_pct(s.safety.score)} {_pct(s.total)}  {' '.join(flags)}")
    lines.append("-" * 78)

    sm = summarize(scores)
    c = sm["categories"]
    lines.append(f"{'SUITE (category means)':28s} {_pct(c['task'])} {_pct(c['interrupt'])} {_pct(c['latency'])} {_pct(c['safety'])} {_pct(sm['overall'])}")
    lines.append(f"{'weights':28s} {WEIGHTS['task'] * 100:5.0f}  {WEIGHTS['interrupt'] * 100:5.0f}  {WEIGHTS['latency'] * 100:5.0f}  {WEIGHTS['safety'] * 100:5.0f}")
    r = sm["raw"]
    lines += ["", "Raw measurements (independent of the proxy formulas):",
              f"  interrupt->cancel latency : median {r['median_cancel_latency_s']}s, worst {r['worst_cancel_latency_s']}s",
              f"  user done -> first ack    : median {r['median_first_ack_s']}s",
              f"  user done -> first reply  : median {r['median_first_reply_s']}s",
              f"  stale completions / re-runs / duplicate state changes : {r['stale_completions']} / {r['stale_reruns']} / {r['duplicate_state_changes']}",
              f"  invalid payloads / trace violations : {r['invalid_payloads']} / {r['trace_violations']}"]
    if sm["infra_degraded"]:
        lines.append(f"  NOTE: provider errors affected: {', '.join(sm['infra_degraded'])} -- treat those scores as infrastructure noise")
        for s in scores:
            for err in s.llm_errors:
                lines.append(f"    {s.name}: {err}")

    if show_notes:
        failing = [s for s in scores if s.notes]
        if failing:
            lines += ["", "Where points were lost:"]
            for s in failing:
                lines.append(f"  {s.name}:")
                lines += [f"    - {n}" for n in s.notes]
    return "\n".join(lines)


def to_json(scores: List[ScenarioScore], mode: str) -> Dict[str, Any]:
    return {"llm": mode, "summary": summarize(scores), "scenarios": [asdict(s) | {"notes": s.notes} for s in scores]}
