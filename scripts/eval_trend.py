"""Print the trend of recorded live evals: python scripts/eval_trend.py [eval_results/live_*.json ...]

Reads the JSON reports the nightly job (or `python -m agent.eval --out`) writes and prints one row per report:
overall, the four category scores, median reaction times, and the scenarios flagged flaky (variance runs only).
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path


def row(path: str) -> str:
    d = json.loads(Path(path).read_text())
    sm, cats, raw = d["summary"], d["summary"]["categories"], d["summary"]["raw"]
    f = lambda x: "  -- " if x is None else f"{x * 100:5.1f}"
    var = d.get("variance") or {}
    flaky = ",".join(var.get("flaky", [])) or "-"
    return (f"{Path(path).stem:24s} {f(sm['overall'])} | task {f(cats['task'])} intr {f(cats['interrupt'])} lat {f(cats['latency'])} "
            f"safe {f(cats['safety'])} | cancel {raw['median_cancel_latency_s']}s ack {raw['median_first_ack_s']}s "
            f"reply {raw['median_first_reply_s']}s | flaky: {flaky}")


def main() -> int:
    paths = sorted(sys.argv[1:] or glob.glob("eval_results/*.json"))
    if not paths:
        print("no reports found", file=sys.stderr)
        return 1
    for p in paths:
        try:
            print(row(p))
        except Exception as e:  # noqa: BLE001
            print(f"{Path(p).stem:24s} unreadable ({type(e).__name__}: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
