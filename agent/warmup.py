"""Warm-up preloading module (§7.7).

Executes during the competition 300s setup/warm-up hook before the
120s-per-scenario evaluation clock starts. Eliminates cold-start latency penalties.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from agent.fast_path.intent_classifier import IntentClassifier
from agent.fast_path.templates import generate_filler
from agent.multimodal.asr import ASRProcessor
from agent.multimodal.vision import VisionProcessor
from agent.llm_client import LLMBackend, LLMConfig, get_backend

logger = logging.getLogger("agent.warmup")


async def run_warmup_hook(
    llm_backend: Optional[LLMBackend] = None,
    warm_asr: bool = True,
    warm_vision: bool = False,
) -> float:
    """Pre-warm models, CUDA contexts, and KV caches (§7.7).
    
    Returns:
        Total duration of warm-up in seconds.
    """
    start_time = time.time()
    logger.info("Initiating 300s warm-up preloading phase...")

    # 1. Warm-up Tier 1 Intent Classifier (embeddings)
    try:
        classifier = IntentClassifier(use_embeddings=True)
        classifier.classify_text("warmup text ping")
        logger.info("Tier 1 Intent Classifier pre-warmed.")
    except Exception as e:
        logger.warning("Tier 1 warm-up notice: %s", e)

    # 2. Warm-up Tier 2 Templates
    generate_filler(intent="search_flights", slots={"origin": "BLR", "destination": "DEL"})

    # 3. Warm-up Tier 3 LLM (local vLLM / Qwen or mock)
    if llm_backend:
        try:
            await llm_backend.generate(
                messages=[{"role": "user", "content": "ping"}],
                tools=None,
            )
            logger.info("Tier 3 LLM client pre-warmed.")
        except Exception as e:
            logger.warning("Tier 3 LLM warm-up notice: %s", e)

    # 4. Warm-up ASR if requested
    if warm_asr:
        asr = ASRProcessor()
        asr.warmup()

    # 5. Warm-up Vision if requested
    if warm_vision:
        vis = VisionProcessor()
        vis.warmup()

    duration = time.time() - start_time
    logger.info("Warm-up sequence completed in %.2f seconds.", duration)
    return duration
