"""Tier-1 detector (spec §7.2): measured accuracy on the held-out split, the mid-band clarification flow, and the
raw score landing in the trace. The embedding tests need sentence-transformers + the MiniLM weights."""

import pytest

from agent.coordinator import AgentCoordinator
from agent.fast_path import intent_classifier as ic
from agent.fast_path.intent_dataset import labeled, split
from agent.schemas.actions import ActionType
from agent.schemas.events import UserTextEvent


@pytest.fixture(scope="module")
def clf():
    c = ic.IntentClassifier()
    c._load_model()
    if not c.use_embeddings:
        pytest.skip("sentence-transformers / MiniLM weights unavailable")
    return c


def test_dataset_is_disjoint_from_anchors_and_reasonably_sized():
    anchors = {a.lower() for a in ic.INTERRUPT_ANCHORS + ic.CONTINUATION_ANCHORS}
    texts = {t.lower() for t, _ in labeled()}
    assert not anchors & texts, "anchors must not appear in the calibration set"
    dev, test = split()
    assert len(dev) + len(test) >= 150 and {l for _, l in test} == {True, False}


def test_held_out_accuracy_beats_keyword_baseline_with_no_false_acts(clf):
    _, test = split()
    kw = ic.IntentClassifier(use_embeddings=False)
    results = [(t, lab, clf.classify_text(t)) for t, lab in test]
    false_act = [t for t, lab, r in results if r["decision"] == "act" and not lab]
    handled = [r["decision"] in ("act", "clarify") for _, lab, r in results if lab]
    acc = sum((r["decision"] != "continue") == lab for _, lab, r in results) / len(results)
    kw_acc = sum(kw.classify_text(t)["is_interrupt"] == lab for t, lab in test) / len(test)
    assert not false_act, f"acted (cancelled work) on ordinary speech: {false_act}"
    assert sum(handled) / len(handled) >= 0.95     # every real interrupt is acted on or asked about
    assert acc >= 0.93 and acc > kw_acc


def test_result_carries_raw_scores_and_clear_cases_act(clf):
    r = clf.classify_text("no wait, make it Mumbai")
    assert r["mode"] == "embedding" and 0.0 <= r["confidence_score"] <= 1.0
    assert {"interrupt_similarity", "continuation_similarity", "features"} <= r.keys()
    assert clf.classify_text("stop")["decision"] == "act"
    assert clf.classify_text("abort")["decision"] == "act"            # bare stop rule
    assert clf.classify_text("find me flights to Goa")["decision"] == "continue"
    assert clf.classify_text("no problem")["decision"] != "act"


# --------------------------------------------------------------------------- coordinator flow
class _Scripted:
    def __init__(self, table):
        self.table = table

    def classify_text(self, text):
        d = self.table.get(text, "continue")
        return {"is_interrupt": d == "act", "needs_clarification": d == "clarify", "confidence_score": 0.5,
                "decision": d, "mode": "embedding"}


def _drain(c):
    out = []
    while not c.action_queue.empty():
        out.append(c.action_queue.get_nowait())
    return out


async def _busy_session(table):
    c = AgentCoordinator(intent_classifier=_Scripted(table))
    s = c.get_or_create_session("s1")
    await c.emit_action(s.register_tool_call(call_id="c1", tool_name="search_flights", arguments={"origin": "DEL"}))
    _drain(c)
    return c, s


async def test_mid_band_asks_instead_of_bumping_epoch_then_yes_acts_on_original():
    c, s = await _busy_session({"hmm maybe mumbai": "clarify"})
    epoch = s.epoch
    await c._handle_user_text(s, UserTextEvent(session_id="s1", text="hmm maybe mumbai"))
    acts = _drain(c)
    assert s.epoch == epoch and s.pending_clarification == "hmm maybe mumbai"
    assert [a.action_type for a in acts] == [ActionType.CLARIFICATION]

    await c._handle_user_text(s, UserTextEvent(session_id="s1", text="yes"))
    assert s.epoch == epoch + 1 and s.pending_clarification is None
    assert s.in_flight_calls["c1"].status == "cancelled"


async def test_mid_band_then_no_keeps_work_running():
    c, s = await _busy_session({"hmm maybe mumbai": "clarify"})
    epoch = s.epoch
    await c._handle_user_text(s, UserTextEvent(session_id="s1", text="hmm maybe mumbai"))
    _drain(c)
    await c._handle_user_text(s, UserTextEvent(session_id="s1", text="no"))
    acts = _drain(c)
    assert s.epoch == epoch and s.in_flight_calls["c1"].status == "pending" and s.pending_clarification is None
    assert [a.action_type for a in acts] == [ActionType.SPOKEN_RESPONSE]


async def test_mid_band_with_nothing_running_is_an_ordinary_request():
    c = AgentCoordinator(intent_classifier=_Scripted({"hmm maybe mumbai": "clarify"}))
    s = c.get_or_create_session("s2")
    await c._handle_user_text(s, UserTextEvent(session_id="s2", text="hmm maybe mumbai"))
    assert s.pending_clarification is None
    assert ActionType.CLARIFICATION not in [a.action_type for a in _drain(c)]


async def test_classification_is_written_to_the_trace():
    c, s = await _busy_session({})
    await c._handle_user_text(s, UserTextEvent(session_id="s1", text="and a window seat"))
    recs = [r for r in c.trace_logger.trace_history if r["record_type"] == "classification"]
    assert recs and recs[-1]["payload"]["text"] == "and a window seat" and recs[-1]["payload"]["decision"] == "continue"


def test_short_spoken_corrections_act_immediately_not_clarify(clf):
    """Found by the eval harness: an over-cautious threshold turned 'No sorry, Chicago' into a question, so the
    correction was never applied. Terse corrections with a clear retraction cue must act."""
    for text in ["Actually make that Boston", "No sorry, Chicago", "no wait, make it Goa", "stop", "never mind"]:
        assert clf.classify_text(text)["decision"] == "act", text
    for text in ["Book a flight from Delhi to New York", "and a window seat", "no worries at all"]:
        assert clf.classify_text(text)["decision"] == "continue", text
