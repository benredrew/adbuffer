"""Release segments for playout, censoring the ones that overlap ads, and maintain a live
HLS playlist that keeps the last `DVR_SECONDS` so the player can scrub back.

Lead-in: playout starts as soon as `MIN_LEAD` seconds past the first segment have been
analysed, then runs at real time. Recorded media is ingested faster than real time, so the
analysed lookahead grows by itself while it plays. A live stream can't be read ahead, so its
playout runs at `LIVE_RAMP_RATE` (the player slows down to match) until the lookahead reaches
the target `delay`.

Each segment carries an EXT-X-PROGRAM-DATE-TIME of 1970-01-01 + its stream time, which lets
the UI map the player's position back to stream time (transcript, ad markers)."""

import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone

from .censor import censor_segment, overlapping
from .state import State

log = logging.getLogger("publish")

DVR_SECONDS = 2 * 3600
BUFFER_KEEP = 30       # raw segments kept behind the playout point (the analyzer only looks ahead of it)
MIN_LEAD = 6.0         # analysed seconds required past a segment before it may play
LIVE_RAMP_RATE = 0.93  # live playout speed while the lookahead grows toward the target


class Publisher(threading.Thread):
    def __init__(self, state: State, buf_dir: str, out_dir: str, delay: float, ramp: float, height: int,
                 grace: float = 15.0):
        super().__init__(name="publisher", daemon=True)
        self.state, self.buf_dir, self.out_dir = state, buf_dir, out_dir
        self.delay, self.ramp, self.height = delay, ramp, height
        self.grace = grace  # how long playout may stall waiting for analysis before going ahead anyway
        self.entries: list[tuple[str, float, float]] = []  # name, duration, stream start
        self.media_seq = 0
        self.ended = False
        self.due: float | None = None  # wall-clock time the next segment is scheduled to play

    def _ready(self, seg) -> bool:
        st = self.state
        lead = st.analyzed_until - seg.end
        all_in = st.ingest_done and all(s.analyzed for s in st.segments)
        return seg.analyzed and (lead >= MIN_LEAD or all_in)

    def _wait_ready(self, i: int):
        """Block until segment i is analysed with enough lookahead and its playout slot has come."""
        st = self.state
        with st.changed:
            while not st.stopping:
                if i >= len(st.segments):
                    if st.ingest_done:
                        return None
                    st.changed.wait(0.5)
                    continue
                seg = st.segments[i]
                now = time.monotonic()
                on_time = self.due is None or now >= self.due
                if on_time and self._ready(seg):
                    return seg
                if self.due is not None and now >= self.due + self.grace:
                    log.warning("%s played with only %.0fs analysed ahead", seg.name, st.analyzed_until - seg.end)
                    return seg
                st.changed.wait(0.05 if not on_time and self.due - now < 0.5 else 0.25)
        return None

    def _schedule_next(self, seg, dur: float):
        st = self.state
        lead = st.analyzed_until - seg.end
        st.rate = LIVE_RAMP_RATE if st.live and lead < self.delay else 1.0
        now = time.monotonic()
        # keep a steady cadence; after a stall, restart the schedule from now
        base = self.due if self.due is not None and now - self.due < 1.0 else now
        self.due = base + dur / st.rate

    def run(self):
        i = 0
        while (seg := self._wait_ready(i)) is not None:
            dst = os.path.join(self.out_dir, seg.name)
            # merge gap > segment length + both ramps guarantees at most one interval per segment
            intervals = self.state.ad_intervals(merge_gap=seg.duration + 2 * self.ramp + 1)
            hits = overlapping(intervals, seg.start, seg.end, self.ramp)
            if hits:
                try:
                    censor_segment(seg.path, dst, hits[0], self.ramp, self.height)
                    self.state.censored_segments += 1
                    log.info("%s censored (ad %.1f-%.1f)", seg.name, *hits[0])
                except Exception:
                    log.exception("censor failed for %s, passing through", seg.name)
                    shutil.copyfile(seg.path, dst)
            else:
                shutil.copyfile(seg.path, dst)
            with self.state.changed:
                self.state.published_until = seg.end
                self.state.published_segments += 1
                self.state.changed.notify_all()
            # list the true spacing to the next segment (the probed duration includes audio
            # overhang) so the player's timeline doesn't drift from stream time
            with self.state.lock:
                nxt = self.state.segments[i + 1] if i + 1 < len(self.state.segments) else None
                dur = nxt.start - seg.start if nxt else seg.duration
                self._schedule_next(seg, dur)
            self._append(seg.name, dur, seg.start)
            if i >= BUFFER_KEEP:
                try:
                    os.remove(self.state.segments[i - BUFFER_KEEP].path)
                except FileNotFoundError:
                    pass
            i += 1
        self.ended = True
        self._write_playlist()
        log.info("stream ended; %d segments published", i)

    def _append(self, name: str, dur: float, start: float):
        self.entries.append((name, dur, start))
        while sum(e[1] for e in self.entries) > DVR_SECONDS:
            old, _, _ = self.entries.pop(0)
            self.media_seq += 1
            try:
                os.remove(os.path.join(self.out_dir, old))
            except FileNotFoundError:
                pass
        self._write_playlist()

    def _write_playlist(self):
        target = max([e[1] for e in self.entries] or [2])
        body = [
            "#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{int(target + 0.999)}",
            f"#EXT-X-MEDIA-SEQUENCE:{self.media_seq}",
        ]
        for name, dur, start in self.entries:
            pdt = datetime.fromtimestamp(start, timezone.utc).isoformat(timespec="milliseconds")
            body += [f"#EXT-X-PROGRAM-DATE-TIME:{pdt}", f"#EXTINF:{dur:.3f},", name]
        if self.ended:
            body.append("#EXT-X-ENDLIST")
        tmp = os.path.join(self.out_dir, "live.m3u8.tmp")
        with open(tmp, "w") as f:
            f.write("\n".join(body) + "\n")
        os.replace(tmp, os.path.join(self.out_dir, "live.m3u8"))
