"""Scenario evaluation harness: runs scripted scenarios against the real coordinator and scores the
resulting trace on the spec's four weighted categories (task completion, interruption recovery,
response latency, safety & protocol). See `python -m agent.eval --help`."""
