"""One buffered, ad-softened playback of a single source."""

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from .detect import Analyzer, sentences
from .ingest import VOD_READRATE, start_ingest, watch_segments
from .publish import MIN_LEAD, Publisher
from .sponsorblock import SponsorBlockWatcher, youtube_id
from .state import State

log = logging.getLogger("session")

# on disk, not /tmp: with a 2 h scrub-back window a session holds a few GB of video
WORK_ROOT = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "adbuffer")
TRANSCRIPT_WINDOW = 150  # seconds of transcript either side of the player position sent to the UI


class Session:
    def __init__(self, source: str, title: str, transcriber, classifier, delay: float, ramp: float, height: int):
        self.id = uuid.uuid4().hex[:8]
        self.source, self.title, self.delay, self.classifier = source, title, delay, classifier
        self.started = time.monotonic()
        self.state = State()
        os.makedirs(WORK_ROOT, exist_ok=True)
        self.work = tempfile.mkdtemp(prefix=f"{self.id}-", dir=WORK_ROOT)
        self.buf_dir, self.out_dir = os.path.join(self.work, "buffer"), os.path.join(self.work, "out")
        os.makedirs(self.buf_dir)
        os.makedirs(self.out_dir)
        self.procs: list[subprocess.Popen] = []
        self.threads = [
            threading.Thread(target=self._ingest, args=(source, height), name="ingest", daemon=True),
            Analyzer(self.state, transcriber, classifier),
            Publisher(self.state, self.buf_dir, self.out_dir, delay, ramp, height),
        ]
        if vid := youtube_id(source):
            self.threads.append(SponsorBlockWatcher(self.state, vid))
        for t in self.threads:
            t.start()
        log.info("session %s started: %s", self.id, source)

    def _ingest(self, source: str, height: int):
        """Resolve the source (yt-dlp can take a few seconds) and run ffmpeg, off the request path."""
        try:
            self.procs, self.state.live = start_ingest(source, self.buf_dir, height)
        except Exception as e:
            log.warning("can't open %s: %s", source, e)
            self.procs = [subprocess.Popen(["false"])]  # an ingest that exits at once marks the session failed
        if self.state.stopping:
            for p in self.procs:
                p.terminate()
        watch_segments(self.state, self.buf_dir, self.procs[-1])

    def stop(self):
        with self.state.changed:
            self.state.stopping = True
            self.state.changed.notify_all()
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
        for t in self.threads:
            t.join(timeout=15)
        shutil.rmtree(self.work, ignore_errors=True)
        log.info("session %s stopped", self.id)

    def status(self, around: float | None = None) -> dict:
        st = self.state
        with st.lock:
            first = st.segments[0].arrived if st.segments else None
            failed = st.ingest_done and not st.segments
            body = {
                "id": self.id,
                "source": self.source,
                "title": self.title,
                "delay": self.delay,
                "starts_in": self._starts_in() if not st.published_segments else 0.0,
                "lead": max(0.0, st.analyzed_until - st.published_until) if st.published_segments else 0.0,
                "target_lead": self.delay,
                "rate": st.rate,
                "live": st.live,
                "sponsorblock": st.sponsorblock,
                "waiting_for_source": first is None and not failed,
                "failed": failed,
                "published_segments": st.published_segments,
                "censored_segments": st.censored_segments,
                "buffered_seconds": (st.segments[-1].end - st.published_until) if st.published_segments else
                (time.monotonic() - first if first else 0.0),
                "ingest_done": st.ingest_done,
                "ended": st.ingest_done and st.published_segments >= len(st.segments) > 0,
                "published_until": st.published_until,
                "origin": st.segments[0].start if st.segments else 0.0,  # stream time of the source's 0:00
                "spans": [{"start": x.start, "end": x.end, "kind": x.kind, "source": x.source} for x in st._spans()],
                "lines": self._display_lines(around),
            }
        return body

    def _starts_in(self) -> float:
        """Rough wait until playout starts: the analysis still needed for the first segment's
        lookahead, at the ingest speed, plus a few seconds of transcription. Caller holds the lock."""
        st = self.state
        if not st.segments:
            return MIN_LEAD + 10.0  # resolving the source and the first analysis pass
        need = st.segments[0].end + MIN_LEAD - st.analyzed_until
        return max(1.0, max(0.0, need) / (1.0 if st.live else VOD_READRATE) + 3.0)

    def _display_lines(self, around: float | None) -> list[dict]:
        """Transcript sentences near `around` (the player's stream time; default: the newest), with
        a placeholder where an ad has no speech. Caller holds the lock."""
        st = self.state
        if around is None:
            around = st.published_until
        lo, hi = around - TRANSCRIPT_WINDOW, around + TRANSCRIPT_WINDOW
        spans = st._spans()
        rows = []
        for line in st.lines:
            if line.noise or line.end < lo or line.start > hi:
                continue
            for it in sentences(line):
                mid = (it.start + it.end) / 2
                hit = next((x for x in spans if x.start <= mid <= x.end), None)
                rows.append({"start": it.start, "end": it.end, "text": it.text, "ad": bool(hit), "kind": hit.kind if hit else ""})
        heard = st.lines[-1].end if st.lines else 0.0
        for x in spans:
            if x.end > lo and x.start < min(hi, heard) and not any(r["ad"] and r["start"] < x.end and r["end"] > x.start for r in rows):
                rows.append({"start": x.start, "end": x.end, "ad": True, "kind": x.kind,
                             "text": f"\u2014 {x.end - x.start:.0f}s ad without clear speech \u2014"})
        rows.sort(key=lambda r: r["start"])
        return rows


class Sessions:
    """Holds the single active session; starting a new one replaces it."""

    def __init__(self, make_transcriber, classifier, delay, ramp, height):
        self._make_transcriber, self.classifier = make_transcriber, classifier
        self.delay, self.ramp, self.height = delay, ramp, height
        self._transcriber = None
        self._load_lock = threading.Lock()
        self.current: Session | None = None
        self.lock = threading.Lock()

    def preload(self):
        threading.Thread(target=self._load, name="whisper-load", daemon=True).start()

    def _load(self):
        with self._load_lock:
            if self._transcriber is None:
                self._transcriber = self._make_transcriber()  # kept for the daemon's life
        return self._transcriber

    def start(self, source: str, title: str = "") -> Session:
        with self.lock:
            self._load()
            old, self.current = self.current, None
            if old:
                old.stop()
            self.current = Session(source, title or source, self._transcriber, self.classifier,
                                   self.delay, self.ramp, self.height)
            return self.current

    def stop(self):
        with self.lock:
            old, self.current = self.current, None
            if old:
                old.stop()
