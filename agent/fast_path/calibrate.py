"""Fit and report the Tier-1 interrupt detector (spec §7.2):  python -m agent.fast_path.calibrate [--write]

Combines MiniLM anchor similarities with a few lexical cues in a small logistic model, fit on the `dev` split of
the labeled set only and reported on the held-out `test` split next to the keyword-heuristic baseline. Thresholds
are then chosen on dev: a false `act` cancels real work while a false `continue` only misses a correction, so
`high` is the lowest probability whose dev precision is >= 0.98, and `mid` is the lowest probability whose
[mid, high) band is still >= 50% real interrupts. `--write` stores weights + thresholds in intent_weights.json.
"""

from __future__ import annotations

import argparse
import json
from typing import Dict, List, Tuple

import numpy as np

from agent.fast_path import intent_classifier as ic
from agent.fast_path.intent_dataset import split

Data = List[Tuple[str, bool]]


def featurize(data: Data, clf: "ic.IntentClassifier") -> Tuple[np.ndarray, np.ndarray]:
    X, y = [], []
    for text, label in data:
        r = clf.classify_text(text)
        X.append(ic.feature_vector(r["interrupt_similarity"], r["continuation_similarity"], text.strip().lower()))
        y.append(float(label))
    return np.array(X), np.array(y)


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 0.1, iters: int = 5000, lr: float = 0.5) -> np.ndarray:
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-X @ w))
        w -= lr * (X.T @ (p - y) / len(y) + l2 * np.r_[w[:-1], 0.0] / len(y) * 10)
    return w


def probabilities(X: np.ndarray, w: np.ndarray, texts: List[str]) -> np.ndarray:
    """Model probability after the same linguistic rules the runtime applies (so thresholds fit what runs)."""
    raw = 1.0 / (1.0 + np.exp(-X @ w))
    return np.array([ic.apply_rules(float(p), t.strip().lower())[0] for p, t in zip(raw, texts)])


def metrics(p: np.ndarray, y: np.ndarray, high: float, mid: float) -> Dict[str, float]:
    act, clar = p >= high, (p >= mid) & (p < high)
    pos = y == 1
    return {
        "act_precision": float(y[act].mean()) if act.any() else 1.0,
        "act_recall": float(act[pos].mean()),
        "clarify_n": int(clar.sum()),
        "clarify_precision": float(y[clar].mean()) if clar.any() else 1.0,
        "handled_recall": float((act | clar)[pos].mean()),   # interrupts acted on or asked about
        "false_act": int((act & ~pos).sum()),
        "false_clarify": int((clar & ~pos).sum()),
        "accuracy_at_high": float(((p >= high) == pos).mean()),
    }


def choose_thresholds(p: np.ndarray, y: np.ndarray) -> Tuple[float, float]:
    grid = [round(0.05 * i, 2) for i in range(4, 20)]
    high = next((g for g in grid if metrics(p, y, g, g)["act_precision"] >= 0.98), 0.9)
    mid = next((g for g in grid if g < high and metrics(p, y, high, g)["clarify_precision"] >= 0.5), high)
    return high, mid


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="store the fitted weights/thresholds in intent_weights.json")
    ap.add_argument("--errors", action="store_true", help="list misclassified test utterances")
    args = ap.parse_args()

    dev, test = split()
    # Similarities don't depend on the weights, so any calibration works for featurizing.
    clf = ic.IntentClassifier()
    Xd, yd = featurize(dev, clf)
    Xt, yt = featurize(test, clf)
    w = fit_logistic(Xd, yd)
    pd_, pt = probabilities(Xd, w, [t for t, _ in dev]), probabilities(Xt, w, [t for t, _ in test])
    high, mid = choose_thresholds(pd_, yd)
    print(f"dev={len(dev)} test={len(test)}  features={ic.FEATURES}\nweights={np.round(w, 2).tolist()}  high={high} mid={mid}")
    report = {"dev": metrics(pd_, yd, high, mid), "test": metrics(pt, yt, high, mid)}
    for name, m in report.items():
        print(f"{name:5}", {k: round(v, 3) for k, v in m.items()})

    kw = ic.IntentClassifier(use_embeddings=False)
    kw_acc = float(np.mean([kw.classify_text(t)["is_interrupt"] == lab for t, lab in test]))
    print(f"TEST accuracy (act-or-clarify vs not): keyword baseline {kw_acc:.3f}   "
          f"embedding+lexical {float(((pt >= mid) == (yt == 1)).mean()):.3f}")
    if args.errors:
        for (text, lab), p in zip(test, pt):
            d = ic.decide(p, high, mid)
            if (d == "act") != lab:
                print(f"  {'MISSED   ' if lab else 'FALSE-ACT'} {d:8} p={p:.2f}  {text!r}")
    if args.write:
        with open(ic._WEIGHTS_PATH, "w", encoding="utf-8") as f:
            json.dump({"features": list(ic.FEATURES), "weights": [round(float(x), 4) for x in w], "high": high, "mid": mid,
                       "fit": {"dev_n": len(dev), "test_n": len(test), "test_metrics": {k: round(v, 3) for k, v in report["test"].items()}}},
                      f, indent=2)
        print(f"wrote {ic._WEIGHTS_PATH}")


if __name__ == "__main__":
    main()
