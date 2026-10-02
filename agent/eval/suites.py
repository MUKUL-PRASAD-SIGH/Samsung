"""Scenario registries: DEV (tuned against) vs HOLDOUT (generalization estimate) -- see holdout.py."""

from __future__ import annotations

from typing import Dict, List

from agent.eval.holdout import HOLDOUT
from agent.eval.scenario import Scenario
from agent.eval.scenarios import SUITE as DEV

ALL: List[Scenario] = DEV + HOLDOUT
BY_NAME: Dict[str, Scenario] = {s.name: s for s in ALL}
assert len(BY_NAME) == len(ALL), "scenario names must be unique across dev and hold-out"


def select(which: str = "dev") -> List[Scenario]:
    return {"dev": DEV, "holdout": HOLDOUT, "all": ALL}[which]
