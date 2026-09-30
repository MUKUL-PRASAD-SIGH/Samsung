"""Labeled utterances for calibrating/measuring the Tier-1 interrupt detector (spec §7.2).

Label meaning is deliberately narrow: INTERRUPT = the user is stopping, cancelling or correcting work that is
already in flight ("no wait, Mumbai", "stop", "scratch that"). CONTINUE = anything else: a fresh request, an
additive detail, a question, small talk, or a confirmation. A new request is NOT an interrupt by itself; the
planner decides whether it supersedes anything.

Utterances are written the way speech arrives, including truncated voice partials ("no wa"). The anchors the
classifier matches against live in intent_classifier.py and are kept DISJOINT from this set, so accuracy here
is a measurement and not a memorisation check. Thresholds are fit on the `dev` split and reported on `test`.
"""

from __future__ import annotations

import random
from typing import List, Tuple

INTERRUPT: List[str] = [
    # explicit stops / cancels
    "stop", "stop stop stop", "please stop", "stop that right now", "stop searching", "cancel it", "cancel the search",
    "cancel everything", "abort", "forget it", "never mind", "nevermind about that", "drop it", "enough", "hold it",
    "hold on", "hold on hold on", "wait", "wait wait", "wait a second", "wait no", "hang on", "one moment stop",
    # corrections of a slot
    "no make it Mumbai", "no, from Bangalore", "no not Delhi, Chennai", "actually make that Goa", "actually I meant tomorrow",
    "no I meant next Friday", "sorry, I meant Hyderabad", "wait, change the date to Monday", "no change it to two passengers",
    "actually let's do business class", "no the return is on the twentieth", "instead search for hotels in Pune",
    "switch that to Kolkata", "make it three nights not two", "not that one, the other one", "that's not what I asked",
    "that's wrong, I said Mumbai", "no no no that's the wrong city", "wrong date, it should be December", "oh wait, use my other card",
    "scratch that", "scratch that, book Delhi instead", "ignore that last request", "disregard what I said",
    "correction, the destination is Goa", "let me change that", "I changed my mind", "on second thought make it Jaipur",
    "actually never mind the flight", "no, leave out the hotel", "undo that", "go back, that's wrong",
    "hey stop, wrong airport", "no wait that should be Bengaluru", "sorry wait, different city",
    # truncated voice partials
    "no wa", "sto", "actual", "wait no from", "no i mea", "hold o", "cancel th",
]

CONTINUE: List[str] = [
    # fresh requests
    "find me flights from Delhi to Mumbai", "book a flight to Goa tomorrow", "search hotels in Pune for two nights",
    "what's the weather in Chennai", "check the weather in Delhi this weekend", "I need a flight next Tuesday",
    "show me cheap flights to Kolkata", "look for a hotel near the airport", "book the cheapest one",
    "cancel my booking for Friday's flight after it's confirmed", "reserve a table for two tonight",
    "build me a database schema for a bookstore", "write a security audit checklist", "design an svg logo of a mountain",
    # additive details
    "and also book a window seat", "add a return flight on Sunday", "for two passengers please", "economy class is fine",
    "morning flights only", "direct flights if possible", "with breakfast included", "under ten thousand rupees",
    "and a hotel for those dates", "also check the weather there", "make sure it has free cancellation",
    "I prefer the aisle", "one checked bag", "leaving in the evening", "around noon would be good",
    # confirmations / small talk / questions
    "yes please", "yes that works", "sounds good", "perfect thanks", "okay go ahead", "great", "thank you",
    "that looks right", "how long will it take", "what does that cost", "which airline is that", "is it refundable",
    "how many stops does it have", "can you repeat that", "what did you find", "tell me more about the second one",
    "hello", "hi there", "good morning", "how are you", "who are you", "what can you do", "hmm let me think",
    "no problem", "no worries at all", "nothing else for now", "I know", "nowhere in particular", "now show me hotels",
    "north goa please", "notify me when it's done", "anything cheaper than that", "what about the weather",
    # vision-flavoured requests
    "book a flight to the city on this poster", "what does this sign say", "read the text on my screen",
    "what city is shown in the picture", "look at this and find hotels there",
    # truncated voice partials of ordinary speech
    "find me fli", "book a flight to", "and also", "what's the wea", "yes ple", "for two",
]

# Composed so the set has enough breadth to calibrate on, without pretending these are independent samples.
_SLOT_CORRECTIONS = ["no, {x}", "actually, {x}", "wait, {x}", "sorry, {x}", "no wait, {x}"]
_SLOT_VALUES = ["make it {c}", "I meant {c}", "change it to {c}", "it should be {c}", "go to {c} instead", "use {c} not the other one"]
_CITIES = ["Mumbai", "Delhi", "Goa", "Chennai", "Pune", "Jaipur", "Kochi", "Lucknow", "Indore", "Nagpur"]
_FRESH = ["find flights to {c}", "weather in {c} tomorrow", "hotels in {c} for the weekend", "book me a flight from {c}",
          "show me the options for {c}", "is it raining in {c}"]
_ADD = ["and add a {d}", "also I want a {d}", "please include a {d}", "with a {d} if you can"]
_DETAILS = ["window seat", "vegetarian meal", "second bag", "late checkout", "sea view room", "flexible ticket"]


def _composed() -> Tuple[List[str], List[str]]:
    rng = random.Random(7)
    inter, cont = [], []
    for tpl in _SLOT_CORRECTIONS:
        for val in rng.sample(_SLOT_VALUES, 3):
            inter.append(tpl.format(x=val.format(c=rng.choice(_CITIES))))
    for tpl in _FRESH:
        for c in rng.sample(_CITIES, 4):
            cont.append(tpl.format(c=c))
    for tpl in _ADD:
        for d in rng.sample(_DETAILS, 3):
            cont.append(tpl.format(d=d))
    return inter, cont


def labeled() -> List[Tuple[str, bool]]:
    """(utterance, is_interrupt), de-duplicated, deterministic order."""
    from agent.fast_path.intent_classifier import CONTINUATION_ANCHORS, INTERRUPT_ANCHORS

    anchors = {a.lower() for a in INTERRUPT_ANCHORS + CONTINUATION_ANCHORS}
    ci, cc = _composed()
    seen, out = set(anchors), []
    for text, label in [(t, True) for t in INTERRUPT + ci] + [(t, False) for t in CONTINUE + cc]:
        if text not in seen:
            seen.add(text)
            out.append((text, label))
    return out


def split(seed: int = 13, dev_fraction: float = 0.5) -> Tuple[List[Tuple[str, bool]], List[Tuple[str, bool]]]:
    """Stratified, deterministic dev/test split."""
    rng = random.Random(seed)
    data = labeled()
    dev, test = [], []
    for label in (True, False):
        items = [d for d in data if d[1] is label]
        rng.shuffle(items)
        cut = int(len(items) * dev_fraction)
        dev += items[:cut]
        test += items[cut:]
    return dev, test
