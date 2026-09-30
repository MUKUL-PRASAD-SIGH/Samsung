"""Import smoke test: every agent module must at least import (a syntax error in a module that no other test
touches once shipped a server that could not start)."""

import importlib
import pkgutil

import agent


def test_every_agent_module_imports():
    failures = []
    for mod in pkgutil.walk_packages(agent.__path__, "agent."):
        if mod.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(mod.name)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{mod.name}: {type(e).__name__}: {e}")
    assert not failures, failures
