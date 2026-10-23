"""Shared state between the ingest, analysis and publish threads.

All times are MPEG-TS presentation timestamps in seconds ("stream time"), so
transcript lines, ad spans and segment boundaries line up exactly with the
timestamps ffmpeg sees when it re-encodes a segment.
"""

import threading
import time
from dataclasses import dataclass, field


@dataclass
class Segment:
    idx: int
    name: str
    path: str
    start: float
    duration: float
    arrived: float = field(default_factory=time.monotonic)
    analyzed: bool = False

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class Line:
    start: float
    end: float
    text: str
    noise: bool = False  # likely a Whisper hallucination over music / effects, not real speech
    words: list[tuple[float, float, str]] = field(default_factory=list)
    ad: bool = False
    kind: str = ""


@dataclass
class Span:
    start: float
    end: float
    kind: str
    source: str  # "heuristic", "claude", "sponsorblock"


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.segments: list[Segment] = []
        self.lines: list[Line] = []
        self.auto_spans: list[Span] = []      # from the transcript classifier, revised every pass
        self.external_spans: list[Span] = []  # from SponsorBlock
        self.published_until = 0.0
        self.analyzed_until = 0.0  # end of the last analysed segment (the detection horizon)
        self.live = False
        self.rate = 1.0            # playout rate; < 1 while a live stream builds up its lookahead
        self.sponsorblock = None   # None (not YouTube), or a short status for the UI
        self.ingest_done = False
        self.stopping = False
        self.censored_segments = 0
        self.published_segments = 0

    def add_segment(self, seg: Segment):
        with self.changed:
            self.segments.append(seg)
            self.changed.notify_all()

    def spans(self) -> list[Span]:
        with self.lock:
            return self._spans()

    def _spans(self) -> list[Span]:
        """SponsorBlock's human-checked boundaries win over the classifier's where both found the ad."""
        ext = self.external_spans
        auto = [a for a in self.auto_spans if not any(a.start < e.end and e.start < a.end for e in ext)]
        return sorted(auto + ext, key=lambda s: s.start)

    def ad_intervals(self, merge_gap: float) -> list[tuple[float, float]]:
        """All ad spans, merged when closer than merge_gap."""
        merged: list[list[float]] = []
        for s in self.spans():
            if merged and s.start - merged[-1][1] <= merge_gap:
                merged[-1][1] = max(merged[-1][1], s.end)
            else:
                merged.append([s.start, s.end])
        return [(a, b) for a, b in merged]

    def relabel_lines(self):
        """Mark transcript lines that fall inside an ad span (for display). Caller holds the lock."""
        spans = self._spans()
        for line in self.lines:
            mid = (line.start + line.end) / 2
            hit = next((s for s in spans if s.start <= mid <= s.end), None)
            line.ad, line.kind = (True, hit.kind) if hit else (False, "")
