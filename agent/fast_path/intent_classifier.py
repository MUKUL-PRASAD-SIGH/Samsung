"""Tier 1: Intent & Interrupt / Barge-In Detector (§3, §7.2).

Uses embedding similarity (all-MiniLM-L6-v2) for CPU-speed (< 5ms) classification:
- Continuation vs. Correction vs. Brand-new Intent
- Implements §7.2 Confidence-Gated Interrupt Detection:
    - High confidence (score >= high_threshold): Auto-act, bump epoch, cancel stale calls.
    - Mid-band (mid_threshold <= score < high_threshold): Needs clarification, do not thrash epoch.
    - Low confidence (score < mid_threshold): Treat as normal continuation or noise.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import math
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("agent.intent_classifier")

# Anchor phrases representing clear interruption / correction signals. Deliberately DISJOINT from the labeled
# calibration set (intent_dataset.py) so measured accuracy is not memorisation.
INTERRUPT_ANCHORS = [
    "stop", "wait", "cancel that", "no wait stop", "actually no", "change that to", "scratch that",
    "don't do that", "hold on a second", "instead of that", "no I said", "that's wrong",
    "no that's not right", "hang on, I want something else", "sorry, I got that wrong", "stop what you're doing",
    "please cancel the request", "I take that back", "no, use a different one", "that's the wrong one",
    "wait, let me fix that", "belay that", "oops, not that", "no, change the city", "actually, switch it to",
    "forget what I just said", "halt", "pause, that's incorrect", "hold your horses", "that isn't what I wanted",
    "let me correct myself", "no I want the other date",
]

# Anchor phrases representing standard continuations / fresh requests
CONTINUATION_ANCHORS = [
    "yes please", "and also add", "for tomorrow", "morning flight", "direct flight only", "two passengers",
    "economy class", "find me a flight", "what's the weather like", "book a hotel room", "thanks a lot",
    "tell me more", "how much does it cost", "show me the options", "sounds good to me", "hello there",
    "search for restaurants nearby", "add a window seat", "is it refundable", "what can you do",
    "with free breakfast", "under a certain budget", "okay go ahead", "please continue",
]


_INTERRUPT_KEYWORDS = re.compile(
    r"\b(?:no|stop|wait|cancel|actually|scratch that|instead|hold on)\b"
)

# Lexical features for the embedding mode. MiniLM alone scores "no wait, make it Mumbai" only ~0.35 against the
# anchors (it is about the new value, not the retraction), so cheap lexical cues are combined with the two
# similarities in a small logistic model fit on the labeled set (see calibrate.py; weights in intent_weights.json).
# STRONG words retract work; WEAK ones only count when they open the utterance; REPAIR words mark a correction.
_STRONG = re.compile(r"\b(?:stop|cancel|abort|scratch that|hold on|never ?mind|forget it|hang on)\b")
_WEAK_OPENING = re.compile(r"^(?:no|nope|actually|wait|sorry|correction|oh wait|hey)\b")
_REPAIR = re.compile(r"\b(?:not|instead|meant|change|switch|make it|should be|wrong|other|different|correction|undo|back)\b")

# "no problem" / "no worries" / "no thanks" open with "no" but are pleasantries, not retractions.
_BENIGN_NO = re.compile(r"^no\s+(?:problem|worries|worry|thanks|thank you|rush|need|hurry)\b")

FEATURES = ("interrupt_similarity", "continuation_similarity", "strong_keyword", "opening_keyword", "repair_word",
            "benign_no", "strong_in_long", "bias")
_WEIGHTS_PATH = Path(__file__).with_name("intent_weights.json")


_LONG_UTTERANCE_WORDS = 6


def lexical_features(text: str) -> Tuple[float, float, float, float, float]:
    strong = bool(_STRONG.search(text))
    return (float(strong), float(bool(_WEAK_OPENING.match(text))), float(bool(_REPAIR.search(text))),
            float(bool(_BENIGN_NO.match(text))),
            # Retractions are short; "cancel" buried in a long sentence is usually an instruction ("cancel my
            # booking for Friday..."), i.e. a tool request rather than an interruption of the work in flight.
            float(strong and len(text.split()) >= _LONG_UTTERANCE_WORDS))


def feature_vector(interrupt_sim: float, continuation_sim: float, text: str) -> List[float]:
    return [interrupt_sim, continuation_sim, *lexical_features(text), 1.0]


def interrupt_probability(vector: List[float], weights: List[float]) -> float:
    z = sum(w * x for w, x in zip(weights, vector))
    return 1.0 / (1.0 + math.exp(-z))


def load_calibration() -> Dict[str, Any]:
    with open(_WEIGHTS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# A short utterance that is essentially just a stop word is a stop, whatever MiniLM thinks of "abort"/"forget it".
_BARE_STOP_MAX_WORDS = 3


def is_bare_stop(text: str) -> bool:
    return bool(_STRONG.search(text)) and len(text.split()) <= _BARE_STOP_MAX_WORDS


def apply_rules(probability: float, text: str) -> Tuple[float, Optional[str]]:
    """Linguistic overrides that no amount of calibration data should be needed to get right."""
    if _BENIGN_NO.match(text) and not _STRONG.search(text):
        return min(probability, 0.2), "benign_no"        # "no worries" is a pleasantry, never a retraction
    if is_bare_stop(text):
        return max(probability, 0.99), "bare_stop"       # "abort" / "forget it" is a stop whatever MiniLM thinks
    return probability, None


def decide(score: float, high: float, mid: float) -> str:
    """Confidence gate (§7.2): act / clarify / continue."""
    if score >= high:
        return "act"
    if score >= mid:
        return "clarify"
    return "continue"


_MODEL_CACHE: Dict[str, Tuple[Any, Any, Any]] = {}
_MODEL_LOCK = threading.Lock()


def embeddings_enabled() -> bool:
    """INTENT_EMBEDDINGS=1 forces the MiniLM classifier, =0 forces the keyword heuristic; unset/"auto" uses MiniLM
    whenever sentence-transformers is installed (it falls back to keywords by itself if the model can't load)."""
    mode = os.getenv("INTENT_EMBEDDINGS", "auto").strip().lower()
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    return importlib.util.find_spec("sentence_transformers") is not None


class IntentClassifier:
    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        high_threshold: Optional[float] = None,
        mid_threshold: Optional[float] = None,
        use_embeddings: bool = True,
    ):
        self.model_name = model_name
        # Defaults come from the calibration file; explicit values override (tests, calibrate.py itself).
        self.calibration = load_calibration()
        self.high_threshold = self.calibration["high"] if high_threshold is None else high_threshold
        self.mid_threshold = self.calibration["mid"] if mid_threshold is None else mid_threshold
        self.use_embeddings = use_embeddings
        self._model = None
        self._interrupt_embeddings = None
        self._continuation_embeddings = None

    @property
    def ready(self) -> bool:
        """True when classify_text will not block on a model load (keyword mode, or MiniLM already loaded)."""
        return not self.use_embeddings or self._model is not None

    async def aload(self) -> None:
        """Load the model without blocking the event loop (the first load takes seconds, and a stalled loop delays
        exactly the interrupts and tool results this system exists to react to)."""
        if not self.ready:
            import asyncio

            await asyncio.to_thread(self._load_model)

    def _load_model(self) -> None:
        """Lazy loader for the sentence-transformers model, shared by every classifier in the process
        (loading takes ~10 s; a coordinator per session/test must not each pay it)."""
        if self._model is not None or not self.use_embeddings:
            return
        with _MODEL_LOCK:
            cached = _MODEL_CACHE.get(self.model_name)
            if cached is None:
                try:
                    from sentence_transformers import SentenceTransformer

                    # CPU on purpose: 22M parameters is ~3 ms per sentence there, and CUDA init costs seconds of startup
                    # plus VRAM the LLM/ASR/VLM budget needs.
                    import torch

                    # A 22M-parameter model gains nothing from 8 intra-op threads but fights Whisper/VAD/the event loop
                    # for cores (measured: classification latency jitter and delayed debounce timers under load).
                    torch.set_num_threads(int(os.getenv("INTENT_THREADS", "2")))
                    model = SentenceTransformer(self.model_name, device=os.getenv("INTENT_DEVICE", "cpu"))
                    cached = (
                        model,
                        model.encode(INTERRUPT_ANCHORS, normalize_embeddings=True),
                        model.encode(CONTINUATION_ANCHORS, normalize_embeddings=True),
                    )
                    _MODEL_CACHE[self.model_name] = cached
                    logger.info("Loaded embedding model '%s' for Tier 1 detection", self.model_name)
                except Exception as e:
                    logger.warning("Could not load sentence-transformers (%s). Falling back to keyword heuristics.", e)
                    self.use_embeddings = False
                    return
        self._model, self._interrupt_embeddings, self._continuation_embeddings = cached

    def classify_text(self, text: str, force_keywords: bool = False) -> Dict[str, Any]:
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

        if not force_keywords:
            self._load_model()

        if not force_keywords and self.use_embeddings and self._model is not None:
            import numpy as np

            query_emb = self._model.encode([text_clean], normalize_embeddings=True)[0]
            # Max cosine similarity against interrupt anchors
            interrupt_sims = np.dot(self._interrupt_embeddings, query_emb)
            max_interrupt_score = float(np.max(interrupt_sims))

            continuation_sims = np.dot(self._continuation_embeddings, query_emb)
            max_cont_score = float(np.max(continuation_sims))

            vector = feature_vector(max_interrupt_score, max_cont_score, text_clean)
            probability = interrupt_probability(vector, self.calibration["weights"])
            probability, rule = apply_rules(probability, text_clean)
            decision = decide(probability, self.high_threshold, self.mid_threshold)

            return {
                "is_interrupt": decision == "act",
                "needs_clarification": decision == "clarify",
                "confidence_score": probability,
                "decision": decision,
                "mode": "embedding",
                "rule": rule,
                # Raw components, logged to the trace (§7.2) so a threshold can be audited after the fact.
                "features": dict(zip(FEATURES, (round(v, 4) for v in vector))),
                "interrupt_similarity": round(max_interrupt_score, 4),
                "continuation_similarity": round(max_cont_score, 4),
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
            "mode": "keyword",
        }
