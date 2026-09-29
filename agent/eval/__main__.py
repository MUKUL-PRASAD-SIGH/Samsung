"""CLI:  python -m agent.eval [--llm mock|live] [--only NAME ...] [--tag TAG] [--out report.json]

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

from agent.eval.report import format_report, to_json
from agent.eval.runner import Pacer, run_scenario
from agent.eval.scenarios import SUITE
from agent.eval.scorer import score_run
from agent.multimodal.asr import ASRProcessor


async def _main(args: argparse.Namespace) -> int:
    scenarios = [s for s in SUITE
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
    scores = []
    for i, sc in enumerate(scenarios, 1):
        print(f"[{i}/{len(scenarios)}] {sc.name} ...", end=" ", flush=True)
        run = await run_scenario(sc, mode=args.llm, asr=asr, pacer=pacer)
        score = score_run(run)
        scores.append(score)
        print(f"{score.total * 100:5.1f}  ({run.wall_s:.1f}s)")

    print(format_report(scores, args.llm, show_notes=not args.quiet))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(to_json(scores, args.llm), f, indent=2, default=str)
        print(f"\nwrote {args.out}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m agent.eval", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--llm", choices=["mock", "live"], default="mock")
    p.add_argument("--only", nargs="*", help="scenario names")
    p.add_argument("--tag", help="only scenarios with this tag (task/interrupt/safety/fault/voice/context)")
    p.add_argument("--skip-voice", action="store_true")
    p.add_argument("--tpm", type=int, default=6000, help="live mode: tokens-per-minute budget (default 6000)")
    p.add_argument("--out", help="write a JSON report here")
    p.add_argument("--quiet", action="store_true", help="omit the per-scenario failure notes")
    args = p.parse_args()
    logging.disable(logging.CRITICAL)
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
