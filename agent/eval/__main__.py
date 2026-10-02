"""CLI:  python -m agent.eval [--llm mock|live] [--set dev|holdout|all] [--runs N] [--only NAME ...] [--tag TAG] [--out report.json]

mock : scripted LLM per scenario -> deterministic; scores the COORDINATION layer (cancel, idempotency,
       snapshots, latency of the fast path, trace validity).
live : the configured real LLM (Groq/OpenRouter) -> scores the WHOLE system, incl. argument extraction
       and correction handling. Runs are paced to a tokens-per-minute budget so rate limits (429s)
       aren't mistaken for agent failures; affected scenarios are flagged INFRA.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from agent.clock import run_virtual
from agent.eval.report import (aggregate_runs, format_report, format_variance_report, generalization_gap, summarize,
                               to_json)
from agent.eval.runner import Pacer, run_scenario
from agent.eval.suites import select
from agent.eval.scorer import score_run
from agent.multimodal.asr import ASRProcessor


async def _main(args: argparse.Namespace) -> int:
    which = args.set or ("all" if args.only else "dev")
    scenarios = [s for s in select(which)
                 if (not args.only or s.name in args.only)
                 and (not args.tag or args.tag in s.tags)
                 and not (args.skip_voice and s.uses_voice)]
    if not scenarios:
        print("no scenarios selected", file=sys.stderr)
        return 2

    asr = ASRProcessor()
    if any(s.uses_voice for s in scenarios):
        asr._ensure_model_loaded()
        if not asr.is_loaded:
            print("Whisper unavailable: skipping voice scenarios", file=sys.stderr)
            scenarios = [s for s in scenarios if not s.uses_voice]

    pacer = Pacer(args.tpm) if args.llm == "live" else None
    runs = []
    for rep in range(1, args.runs + 1):
        scores = []
        for i, sc in enumerate(scenarios, 1):
            tag = f"[run {rep}/{args.runs}] " if args.runs > 1 else ""
            print(f"{tag}[{i}/{len(scenarios)}] {sc.name} ...", end=" ", flush=True)
            run = await run_scenario(sc, mode=args.llm, asr=asr, pacer=pacer)
            score = score_run(run)
            scores.append(score)
            print(f"{score.total * 100:5.1f}  ({run.wall_s:.1f}s)")
        runs.append(scores)

    if args.runs == 1:
        print(format_report(runs[0], args.llm, show_notes=not args.quiet))
        gap = generalization_gap(runs[0])
        if gap:
            print(f"\ngeneralization: dev {gap['dev']:.1f}  hold-out {gap['holdout']:.1f}  gap {gap['gap']:+.1f}  (target: gap <= 5; {gap['holdout_scenarios']} untuned hold-out scenarios)")
    else:
        print(format_variance_report(aggregate_runs(runs), generalization_gap([s for r in runs for s in r])))
        print("\n(last run, in detail)")
        print(format_report(runs[-1], args.llm, show_notes=not args.quiet))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(to_json(runs[-1], args.llm, runs), f, indent=2, default=str)
        print(f"\nwrote {args.out}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m agent.eval", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--llm", choices=["mock", "live"], default="mock")
    p.add_argument("--set", choices=["dev", "holdout", "all"], help="which scenarios: dev (default; the ones tuned against), "
                   "holdout (generalization estimate), or all. Defaults to 'all' when --only names scenarios.")
    p.add_argument("--runs", type=int, default=1, help="repeat the selection N times and report mean/std/worst + flaky scenarios")
    p.add_argument("--only", nargs="*", help="scenario names")
    p.add_argument("--tag", help="only scenarios with this tag (task/interrupt/safety/fault/voice/context)")
    p.add_argument("--virtual", action="store_true",
                   help="mock mode only: run on a virtual-time event loop (whole suite in seconds); voice scenarios are skipped")
    p.add_argument("--skip-voice", action="store_true")
    p.add_argument("--tpm", type=int, default=6000, help="live mode: tokens-per-minute budget (default 6000)")
    p.add_argument("--out", help="write a JSON report here")
    p.add_argument("--quiet", action="store_true", help="omit the per-scenario failure notes")
    args = p.parse_args()
    logging.disable(logging.CRITICAL)
    if args.virtual:
        if args.llm != "mock":
            p.error("--virtual requires --llm mock (real network/ASR latency cannot be simulated)")
        args.skip_voice = True
        sys.exit(run_virtual(_main(args)))
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
