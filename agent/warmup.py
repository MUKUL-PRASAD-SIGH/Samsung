"""Warm-up preloading module (§7.7).

Executes during the competition 300s setup/warm-up hook before the
120s-per-scenario evaluation clock starts. Eliminates cold-start latency penalties.
"""

from __future__ import annotations

import asyncio
import logging
import time

from agent.multimodal.vision import get_vision_backend

logger = logging.getLogger("agent.warmup")


async def run_full_warmup(coordinator, include_llm: bool = True) -> dict:
    """Warm every runtime model through the coordinator's OWN instances (§7.7) and report per-stage timings.

    Each stage is isolated (one failing never stops the rest) and timed, so `first call after warm-up ~= steady
    state` can be asserted instead of assumed. Stages: classifier (MiniLM), vad (Silero ONNX), asr (Whisper),
    llm (one tiny request: opens the TLS connection and primes the provider), vision (config check only -- the
    hosted backend has nothing to preload).
    """
    report: dict = {}

    async def stage(name, fn, *, threaded=True):
        t0 = time.perf_counter()
        try:
            await (asyncio.to_thread(fn) if threaded else fn())
            report[name] = {"ok": True, "seconds": round(time.perf_counter() - t0, 3)}
        except Exception as e:  # noqa: BLE001 - a failed stage must not abort the others
            report[name] = {"ok": False, "seconds": round(time.perf_counter() - t0, 3), "error": f"{type(e).__name__}: {e}"[:160]}
            logger.warning("Warm-up stage %s failed: %s", name, e)

    def _vad():
        import numpy as np
        from agent.multimodal.streaming import SileroStreamingVAD

        SileroStreamingVAD().prob(np.zeros(512, dtype="float32"))

    async def _llm():
        backend = coordinator.planner.client.backend
        await backend.generate([{"role": "user", "content": "ping"}], tools=None)

    await stage("classifier", lambda: coordinator.intent_classifier.classify_text("warmup text ping"))
    await stage("vad", _vad)
    await stage("asr", coordinator.asr_processor.warmup)
    if include_llm:
        await stage("llm", _llm, threaded=False)
    await stage("vision", get_vision_backend)
    if coordinator.tts_backend is not None:
        await stage("tts", lambda: coordinator.tts_backend.synthesize("Ready."))
    report["total_seconds"] = round(sum(v["seconds"] for v in report.values() if isinstance(v, dict)), 3)
    report["all_ok"] = all(v["ok"] for v in report.values() if isinstance(v, dict))
    coordinator.warmup_report = report
    return report
