"""The agent must behave the same under a virtual-clock harness (spec): all time flows through agent.clock
and asyncio timers, so a virtual-time loop reproduces real-time results in a fraction of the wall time."""

import asyncio
import time

from agent import clock
from agent.eval.runner import run_scenario
from agent.eval.scenarios import BY_NAME, SUITE
from agent.eval.scorer import score_run

NON_VOICE = [s.name for s in SUITE if not s.uses_voice]


def test_virtual_loop_jumps_over_sleeps_and_keeps_now_consistent():
    async def main():
        t0, m0 = clock.now(), clock.monotonic()
        await asyncio.sleep(3600)
        with_timeout = None
        try:
            await asyncio.wait_for(asyncio.sleep(10), timeout=2)
        except asyncio.TimeoutError:
            with_timeout = True
        return clock.now() - t0, clock.monotonic() - m0, with_timeout

    real_start = time.time()
    dt_now, dt_mono, timed_out = clock.run_virtual(main())
    assert time.time() - real_start < 1.0          # an hour of simulated time, instantly
    assert abs(dt_now - 3602) < 0.01 and abs(dt_mono - 3602) < 0.01 and timed_out
    assert clock.now() > 1e9                        # back on the real clock afterwards


def test_suite_scores_match_between_virtual_and_real_clock():
    names = ["correction_mid_search", "rapid_fire_corrections", "duplicate_booking_race", "vision_interrupt"]

    async def run_all():
        return await asyncio.gather(*[run_scenario(BY_NAME[n], mode="mock") for n in names])

    virtual = [score_run(r) for r in clock.run_virtual(run_all())]
    real = [score_run(r) for r in asyncio.run(run_all())]
    for n, v, r in zip(names, virtual, real):
        assert abs(v.total - r.total) < 0.02, (n, v.total, r.total)
        assert v.safety.raw["duplicate_state_changes"] == r.safety.raw["duplicate_state_changes"] == 0
