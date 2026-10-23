"""Shared helpers: rebuild a transcript from a report or case file and run the heuristic on it."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adbuffer.detect import HeuristicClassifier, timeline  # noqa: E402
from adbuffer.state import Line  # noqa: E402

CASES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases")


def load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def lines_of(data: dict) -> list[Line]:
    return [Line(l["start"], l["end"], l["text"], noise=l.get("noise", False),
                 words=[tuple(w) for w in l.get("words", [])]) for l in data["lines"]]


def classify(data: dict):
    lines = lines_of(data)
    horizon = data.get("horizon") or (lines[-1].end if lines else 0.0)
    items = timeline(lines, horizon)
    return items, HeuristicClassifier().classify(items, horizon)


def in_spans(t: float, spans) -> bool:
    return any(s.start <= t <= s.end for s in spans)
