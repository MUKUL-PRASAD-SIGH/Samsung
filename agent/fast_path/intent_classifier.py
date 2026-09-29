"""Tier 1: Intent & Interrupt / Barge-In Detector (§3, §7.2).

Uses embedding similarity (all-MiniLM-L6-v2) for CPU-speed (< 5ms) classification:
- Continuation vs. Correction vs. Brand-new Intent
- Implements §7.2 Confidence-Gated Interrupt Detection:
    - High confidence (score >= high_threshold): Auto-act, bump epoch, cancel stale calls.
    - Mid-band (mid_threshold <= score < high_threshold): Needs clarification, do not thrash epoch.
    - Low confidence (score < mid_threshold): Treat as normal continuation or noise.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("agent.intent_classifier")

# Anchor phrases representing clear interruption / correction signals
INTERRUPT_ANCHORS = [
    "stop",
    "wait",
    "cancel that",
    "no wait stop",
    "actually no",
    "change that to",
    "scratch that",
    "don't do that",
    "hold on a second",
    "instead of that",
    "no I said",
    "that's wrong",
]

# Anchor phrases representing standard continuations
CONTINUATION_ANCHORS = [
    "yes please",
    "and also add",
    "for tomorrow",
    "morning flight",
    "direct flight only",
    "two passengers",
    "economy class",
]


_INTERRUPT_KEYWORDS = re.compile(
    r"\b(?:no|stop|wait|cancel|actually|scratch that|instead|hold on)\b"
)


class IntentClassifier:
    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        high_threshold: float = 0.60,
        mid_threshold: float = 0.40,
        use_embeddings: bool = True,
    ):
        self.model_name = model_name
        self.high_threshold = high_threshold
        self.mid_threshold = mid_threshold
        self.use_embeddings = use_embeddings
        self._model = None
        self._interrupt_embeddings = None
        self._continuation_embeddings = None

    def _load_model(self) -> None:
        """Lazy loader for sentence-transformers model."""
        if self._model is None and self.use_embeddings:
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self.model_name)
                self._interrupt_embeddings = self._model.encode(INTERRUPT_ANCHORS, normalize_embeddings=True)
                self._continuation_embeddings = self._model.encode(CONTINUATION_ANCHORS, normalize_embeddings=True)
                logger.info("Loaded embedding model '%s' for Tier 1 detection", self.model_name)
            except Exception as e:
                logger.warning("Could not load sentence-transformers (%s). Falling back to keyword heuristics.", e)
                self.use_embeddings = False

    def classify_text(self, text: str) -> Dict[str, Any]:
        """Classify user text into interrupt vs. continuation with confidence score (§7.2).
        
        Returns:
            Dict containing:
                - is_interrupt: bool
                - needs_clarification: bool
                - confidence_score: float
                - decision: "act" | "clarify" | "continue"
        """
        text_clean = text.strip().lower()
        if not text_clean:
            return {"is_interrupt": False, "needs_clarification": False, "confidence_score": 0.0, "decision": "continue"}

        self._load_model()

        if self.use_embeddings and self._model is not None:
            import numpy as np

            query_emb = self._model.encode([text_clean], normalize_embeddings=True)[0]
            # Max cosine similarity against interrupt anchors
            interrupt_sims = np.dot(self._interrupt_embeddings, query_emb)
            max_interrupt_score = float(np.max(interrupt_sims))

            continuation_sims = np.dot(self._continuation_embeddings, query_emb)
            max_cont_score = float(np.max(continuation_sims))

            # Relative confidence margin
            net_score = max_interrupt_score

            if net_score >= self.high_threshold:
                decision = "act"
                is_interrupt = True
                needs_clarification = False
            elif net_score >= self.mid_threshold and net_score > max_cont_score:
                decision = "clarify"
                is_interrupt = False
                needs_clarification = True
            else:
                decision = "continue"
                is_interrupt = False
                needs_clarification = False

            return {
                "is_interrupt": is_interrupt,
                "needs_clarification": needs_clarification,
                "confidence_score": net_score,
                "decision": decision,
            }

        # Fallback keyword-based heuristic
        # Whole-word match: the old substring check (`" no" in text`) fired on "now", "north",
        # "nothing"... which would cancel in-flight work on ordinary sentences like "flights now".
        matched = _INTERRUPT_KEYWORDS.search(text_clean) is not None
        
        score = 0.85 if matched else 0.10
        return {
            "is_interrupt": matched,
            "needs_clarification": False,
            "confidence_score": score,
            "decision": "act" if matched else "continue",
        }
