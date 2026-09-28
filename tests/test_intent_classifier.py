"""Tests for Tier 1 IntentClassifier and confidence-gated interrupt detection (§7.2)."""

import pytest
from agent.fast_path.intent_classifier import IntentClassifier


def test_intent_classifier_heuristics():
    classifier = IntentClassifier(use_embeddings=False)

    # Obvious interruptions
    res1 = classifier.classify_text("stop please")
    assert res1["is_interrupt"] is True
    assert res1["decision"] == "act"
    assert res1["confidence_score"] >= 0.8

    res2 = classifier.classify_text("no actually wait, change date to next week")
    assert res2["is_interrupt"] is True
    assert res2["decision"] == "act"

    # Continuation
    res3 = classifier.classify_text("and also book a window seat")
    assert res3["is_interrupt"] is False
    assert res3["decision"] == "continue"
    assert res3["confidence_score"] < 0.4


def test_intent_classifier_empty_input():
    classifier = IntentClassifier(use_embeddings=False)
    res = classifier.classify_text("")
    assert res["is_interrupt"] is False
    assert res["decision"] == "continue"
