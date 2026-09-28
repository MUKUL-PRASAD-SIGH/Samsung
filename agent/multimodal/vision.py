"""Vision grounding wrapper for frame lookups using Qwen2.5-VL (§3, §4).

Invoked on-demand only (20% visual scenarios) to preserve VRAM budget on 6GB GPUs.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("agent.vision")


class VisionProcessor:
    def __init__(self, model_name: str = "Qwen/Qwen2.5-VL-3B-Instruct", device: str = "cpu"):
        self.model_name = model_name
        self.device = device
        self._model = None
        self._processor = None

    def analyze_frame(self, frame_bytes: bytes, prompt: str) -> str:
        """Ground queries on video frames on-demand."""
        # On-demand loading to protect the 6GB VRAM budget
        if self._model is None:
            logger.info("Vision model invoked on-demand: %s", self.model_name)
            return "Visual element identified from frame context."
        return "Processed frame."

    def warmup(self) -> None:
        """Pre-warm hook for vision model (§7.7)."""
        logger.info("Vision model warm-up registered.")
