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
            "false_completion_claims": sum(s.truthfulness.raw["false_claims"] for s in scores if s.truthfulness),
            "llm_calls": sum(s.llm_calls for s in scores),
            "llm_tokens": sum(s.llm_tokens for s in scores),
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
              f"  invalid payloads / trace violations : {r['invalid_payloads']} / {r['trace_violations']}",
              f"  false completion claims (truthfulness) : {r['false_completion_claims']}",
              f"  LLM calls / tokens : {r['llm_calls']} / {r['llm_tokens']}"]
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


def to_json(scores: List[ScenarioScore], mode: str, runs: Optional[List[List[ScenarioScore]]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"llm": mode, "summary": summarize(scores), "scenarios": [asdict(s) | {"notes": s.notes} for s in scores]}
    gap = generalization_gap(scores)
    if gap:
        out["generalization"] = gap
    if runs and len(runs) > 1:
        out["variance"] = aggregate_runs(runs)
        out["runs"] = [[asdict(s) | {"notes": s.notes} for s in run] for run in runs]
    return out


# ------------------------------------------------------------------------------ variance runs
FLAKY_STD = 3.0   # points: a scenario whose total moves more than this between identical runs is flagged


def _std(xs: List[float]) -> float:
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


def aggregate_runs(runs: List[List[ScenarioScore]]) -> Dict[str, Any]:
    """`runs[i]` is the full list of scenario scores from the i-th repetition. Reports mean/std/worst per scenario
    and per category, and which scenarios are flaky (task outcome differs between runs, or a high spread)."""
    by_name: Dict[str, List[ScenarioScore]] = {}
    for run in runs:
        for sc in run:
            by_name.setdefault(sc.name, []).append(sc)
    scenarios = {}
    for name, scs in by_name.items():
        totals = [x.total * 100 for x in scs]
        tasks = [x.task.score * 100 for x in scs]
        scenarios[name] = {
            "runs": len(scs), "mean": round(statistics.mean(totals), 2), "std": round(_std(totals), 2),
            "worst": round(min(totals), 2), "task_min": round(min(tasks), 2), "task_max": round(max(tasks), 2),
            "flaky": (min(tasks) != max(tasks)) or _std(totals) > FLAKY_STD,
            "infra_runs": sum(1 for x in scs if x.infra_errors),
        }
    per_run = [summarize(run) for run in runs]
    cats = {}
    for c in ("task", "interrupt", "latency", "safety"):
        vals = [r["categories"][c] * 100 for r in per_run if r["categories"][c] is not None]
        cats[c] = {"mean": round(statistics.mean(vals), 2), "std": round(_std(vals), 2), "worst": round(min(vals), 2)} if vals else None
    overall = [r["overall"] * 100 for r in per_run]
    return {
        "runs": len(runs), "scenarios": scenarios, "categories": cats,
        "overall": {"mean": round(statistics.mean(overall), 2), "std": round(_std(overall), 2), "worst": round(min(overall), 2)},
        "flaky": sorted(n for n, v in scenarios.items() if v["flaky"]),
        "false_completion_claims": sum(r["raw"]["false_completion_claims"] for r in per_run),
    }


def generalization_gap(scores: List[ScenarioScore]) -> Optional[Dict[str, float]]:
    """Dev-vs-hold-out difference in overall score (positive = hold-out is worse). None unless both are present."""
    held = [s for s in scores if "holdout" in s.tags and "tuned" not in s.tags]   # tuned ones are dev data now
    dev = [s for s in scores if "holdout" not in s.tags]
    if not held or not dev:
        return None
    d, h = summarize(dev)["overall"] * 100, summarize(held)["overall"] * 100
    return {"dev": round(d, 2), "holdout": round(h, 2), "gap": round(d - h, 2), "holdout_scenarios": len(held)}


def format_variance_report(agg: Dict[str, Any], gap: Optional[Dict[str, float]] = None) -> str:
    n = agg["runs"]
    lines = [f"\nVARIANCE over {n} run(s)   (scores 0-100; std is the spread between identical runs)",
             f"{'scenario':30s} {'mean':>6s} {'std':>5s} {'worst':>6s}  {'task min..max':>14s}  flags", "-" * 78]
    for name, v in agg["scenarios"].items():
        flags = ("FLAKY " if v["flaky"] else "") + (f"INFRA({v['infra_runs']})" if v["infra_runs"] else "")
        lines.append(f"{name:30s} {v['mean']:6.1f} {v['std']:5.1f} {v['worst']:6.1f}  {v['task_min']:6.1f}..{v['task_max']:<6.1f}  {flags}")
    lines.append("-" * 78)
    for c, v in agg["categories"].items():
        if v:
            lines.append(f"{c:30s} {v['mean']:6.1f} {v['std']:5.1f} {v['worst']:6.1f}")
    o = agg["overall"]
    lines.append(f"{'OVERALL':30s} {o['mean']:6.1f} {o['std']:5.1f} {o['worst']:6.1f}")
    lines.append(f"\nflaky scenarios: {', '.join(agg['flaky']) or 'none'}      false completion claims: {agg['false_completion_claims']}")
    if gap:
        lines.append(f"generalization: dev {gap['dev']:.1f}  hold-out {gap['holdout']:.1f}  gap {gap['gap']:+.1f}  (target: gap <= 5; "
                     f"{gap['holdout_scenarios']} untuned hold-out scenarios)")
    return "\n".join(lines)
