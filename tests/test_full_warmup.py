"""Spec §7.7: every model is warmed through the coordinator's own instances, stages are isolated and timed."""

import time

from agent.coordinator import AgentCoordinator
from agent.warmup import run_full_warmup


async def test_full_warmup_reports_every_stage_and_warms_the_used_instances():
    c = AgentCoordinator()
    report = await run_full_warmup(c)
    assert {"classifier", "vad", "asr", "llm", "vision", "total_seconds", "all_ok"} <= report.keys()
    assert report["classifier"]["ok"] and report["vad"]["ok"] and report["llm"]["ok"] and report["vision"]["ok"]
    assert c.warmup_report is report
    if c.asr_processor.is_loaded:     # the coordinator's own ASR instance, not a throwaway one
        assert report["asr"]["ok"]


async def test_one_failing_stage_does_not_stop_the_others():
    c = AgentCoordinator()

    def boom():
        raise RuntimeError("no model file")

    c.asr_processor.warmup = boom
    report = await run_full_warmup(c)
    assert report["asr"]["ok"] is False and "no model file" in report["asr"]["error"]
    assert report["vad"]["ok"] and report["llm"]["ok"] and report["all_ok"] is False


async def test_first_call_after_warmup_is_steady_state():
    c = AgentCoordinator()
    await run_full_warmup(c, include_llm=False)
    durations = []
    for _ in range(3):
        t0 = time.perf_counter()
        c.intent_classifier.classify_text("no wait, make it Goa")
        durations.append(time.perf_counter() - t0)
    assert durations[0] < max(0.05, 5 * min(durations[1:]))     # no cold-start spike on the first real call
