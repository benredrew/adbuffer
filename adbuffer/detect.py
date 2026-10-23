"""Speech-to-text plus ad classification over the buffered segments."""

import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass

import numpy as np

from .state import Line, Span, State

log = logging.getLogger("detect")

CHUNK_SEGMENTS = 5          # analyse ~10 s of new audio per pass
FIRST_CHUNK_SEGMENTS = 3    # ...but get the first pass done quickly so playback can start
CONTEXT_AUDIO = 20.0        # ...re-transcribed together with this much of the audio before it
CONTEXT_SECONDS = 150       # how much transcript the classifier sees each pass
MIN_GAP = 3.0               # silence/music stretches at least this long become explicit gap items


def read_pcm(paths: list[str]) -> np.ndarray:
    """Decode the audio of consecutive TS segments to 16 kHz mono float32."""
    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", "concat:" + "|".join(paths),
         "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


# Whisper's stock hallucinations when it hears music or effects instead of speech.
_phantom = re.compile(
    r"^(thanks?( you)? (so much )?for watching|(please )?(like and )?subscribe( to (my|the|our) channel)?( for more videos)?"
    r"|thank you|you|bye|see you (next time|in the next video)|♪+|\[(music|applause|silence)\]|\((music|applause)\))[.!]*$",
    re.I,
)


class Transcriber:
    def __init__(self, model: str, device: str):
        from faster_whisper import WhisperModel

        if device == "auto":
            try:
                self.model = WhisperModel(model, device="cuda", compute_type="float32")
                log.info("whisper %s on cuda", model)
                return
            except Exception as e:  # old GPUs / missing CUDA libs
                log.info("cuda unavailable (%s), using cpu", e)
            device = "cpu"
        self.model = WhisperModel(model, device=device, compute_type="int8" if device == "cpu" else "float32")
        log.info("whisper %s on %s", model, device)

    def transcribe(self, pcm: np.ndarray, offset: float, prompt: str = "") -> list[Line]:
        segments, _ = self.model.transcribe(
            pcm, language="en", beam_size=1, vad_filter=True, word_timestamps=True,
            condition_on_previous_text=False, initial_prompt=prompt or None,
        )
        lines = []
        for s in segments:
            text = s.text.strip()
            if not text:
                continue
            noise = bool(_phantom.match(text)) or s.no_speech_prob > 0.6 or s.avg_logprob < -1.0
            words = [(offset + w.start, offset + w.end, w.word) for w in (s.words or [])]
            lines.append(Line(offset + s.start, offset + s.end, text, noise=noise, words=words))
        return lines


# --------------------------------------------------------------------------- timeline


@dataclass
class Item:
    """A transcript line, or a stretch with no usable speech (music, effects, unintelligible narration)."""
    start: float
    end: float
    text: str = ""

    @property
    def gap(self) -> bool:
        return not self.text


def sentences(line: Line) -> list[Item]:
    """Split a Whisper segment into sentences using word timings. Segments often run several
    sentences long, and an ad cue near the end must not drag the start of the segment with it."""
    if not line.words:
        return [Item(line.start, line.end, line.text)]
    out, cur = [], []
    for w in line.words:
        cur.append(w)
        if w[2].rstrip().endswith((".", "!", "?")):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [Item(ws[0][0], ws[-1][1], "".join(w[2] for w in ws).strip()) for ws in out]


def timeline(lines: list[Line], horizon: float) -> list[Item]:
    items: list[Item] = []
    speech = [it for l in lines if not l.noise for it in sentences(l)]
    t = lines[0].start if lines else horizon
    for it in speech:
        if it.start - t >= MIN_GAP:
            items.append(Item(t, it.start))
        items.append(it)
        t = max(t, it.end)
    if horizon - t >= MIN_GAP:
        items.append(Item(t, horizon))
    return items


# --------------------------------------------------------------------------- classifiers

# Lead-ins announce that an ad starts *after* them.
INTRO_CUES = [
    r"\b(about|to|from|for|on) (a word from |one of )?(our|today'?s|this video'?s|this episode'?s) sponsors?\b",
    r"\bsegue\b", r"\bsponsored by\b", r"\bbrought to you by\b", r"\bthanks to .{1,40} for sponsoring\b",
    r"\bpartnered with\b", r"\bafter (this|these) (short )?(message|messages|break)\b", r"\bwe'?ll be right back\b",
    r"\bbut first,", r"\bthis (video|episode|segment) is (sponsored|supported) by\b",
]
STRONG_CUES = INTRO_CUES + [
    r"\b(today'?s|this video'?s|this episode'?s|our) sponsor", r"\b(promo|discount|coupon) code\b",
    r"\buse (my |our )?code\b", r"\blinks? (is )?(in|down in|below in) the (description|bio)\b", r"\blinks? below\b",
    r"\bfirst \d+ (people|users|customers)\b", r"\bfree trial\b", r"\bask your doctor\b", r"\bcall now\b",
    r"\border (now|today)\b", r"\blimited time (offer|only)\b", r"\b(terms|restrictions) (and conditions )?(may )?apply\b",
    r"\b\d+ ?(%|percent) off\b",
    r"\b(squarespace|nordvpn|expressvpn|surfshark|raid shadow legends|war thunder|world of tanks|betterhelp|audible|"
    r"skillshare|hellofresh|ag1|athletic greens|manscaped|brilliant\.org|ridge wallet|honey|dbrand|ground news|"
    r"incogni|aura|factor meals?|shopify|hostinger|displates?|keeps|rocket money|private internet access)\b",
]
WEAK_CUES = [
    r"\bcheck (them|it) out\b", r"\bhead (over )?to\b", r"\bsign up\b", r"\bsubscription\b", r"\bdownload\b",
    r"\bdot com\b", r"\.com\b", r"\bslash\b", r"\bget started\b", r"\bfor free\b", r"\boffer\b", r"\bdeal\b",
    r"\bfree[- ]to[- ]play\b", r"\bavailable (now|on|at)\b",
]
RETURN_CUES = [
    r"\b(now )?back to (the|our|my) (video|show|stream|episode|build|review)\b", r"^anyway(s)?\b",
    r"\bwith that out of the way\b", r"\blet'?s get (back )?(into|to) it\b", r"\bwe now return\b",
]
_intro = re.compile("|".join(INTRO_CUES), re.I)
_strong = re.compile("|".join(STRONG_CUES), re.I)
_weak = re.compile("|".join(WEAK_CUES), re.I)
_ret = re.compile("|".join(RETURN_CUES), re.I)


class HeuristicClassifier:
    """Keyword-driven ad blocks over the transcript timeline.

    A cue opens a block. A lead-in ("...tell you about our sponsor") opens it *forward*: what
    follows is the ad even if it has no keywords, including a stretch with no usable speech
    (a music-heavy commercial), until the show's own speech resumes. Once ad content has been
    heard, the block runs while cues keep coming (within `linger` s) and closes at a pause,
    a "back to the video" cue, or `max_len`.
    """

    name = "heuristic"
    last = "heuristic"  # what judged the most recent pass (shown in the UI)

    def __init__(self, linger=20.0, intro_linger=60.0, max_len=120.0, min_len=8.0):
        self.linger, self.intro_linger, self.max_len, self.min_len = linger, intro_linger, max_len, min_len

    def classify(self, items: list[Item], horizon: float) -> list[Span]:
        spans = []
        i = 0
        while i < len(items):
            it = items[i]
            intro = not it.gap and _intro.search(it.text)
            if it.gap or not (intro or _strong.search(it.text)):
                i += 1
                continue
            start, end = it.start, None
            cued = not intro      # heard an actual ad cue, beyond the lead-in itself
            last_cue = it.end
            deadline = it.end + (self.intro_linger if intro else self.linger)
            prev_gap = False
            for x in items[i + 1:]:
                if x.start - start > self.max_len:
                    end = start + self.max_len
                    break
                if x.gap:
                    if cued and x.start - start >= self.min_len:
                        end = x.start + 0.5   # the pitch ended in a pause: back to the show
                        break
                    prev_gap = True           # wordless commercial after a lead-in is part of the ad
                    continue
                if _ret.search(x.text):
                    end = x.start
                    break
                if _strong.search(x.text) or _weak.search(x.text):
                    cued, last_cue = True, x.end
                    deadline = max(deadline, x.end + self.linger)
                elif prev_gap and not cued and x.start - start >= self.min_len:
                    end = x.start             # the show's speech resumes after the ad
                    break
                elif x.start > deadline:
                    end = last_cue if cued else x.start
                    break
                prev_gap = False
            if end is None:  # still running at the end of what's been transcribed
                end = min(horizon, start + self.max_len)
            spans.append(Span(start, end, "sponsor/ad", self.name))
            while i < len(items) and items[i].start < end:
                i += 1
        return spans


SYSTEM_PROMPT = """You watch a live transcript of a video stream or broadcast and mark which stretches are advertising.

Advertising means: TV/radio commercial breaks, host-read sponsor segments, pre-recorded sponsor spots \
inserted into a video, paid product placements read aloud, promo-code pitches, and pre/mid-roll ad spots. \
It also covers the lead-in sentence that introduces the sponsor and the call to action that ends it.

Not advertising: the show's own content, the creator discussing products editorially without payment cues, \
brief "like and subscribe" requests, and channel housekeeping.

The transcript is a list of items: [id] (start-end seconds) text. Items marked <no clear speech> are \
stretches where speech recognition heard music, effects or unintelligible narration. A sponsor lead-in \
("...and tell you about our sponsor", "this video is brought to you by") means the ad begins right \
after it: a <no clear speech> stretch directly after a lead-in is usually the commercial itself, and the \
ad ends where the show's own content resumes. If an ad is still running at the last item, end the span \
at the last item.

Return every contiguous span of ad items; return an empty list if there are none. Be precise at the \
boundaries: include the whole ad, but not the normal content around it."""

SCHEMA = {
    "type": "object",
    "properties": {
        "ad_spans": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "first_item": {"type": "integer"},
                    "last_item": {"type": "integer"},
                    "kind": {"type": "string", "enum": ["commercial", "sponsor_read", "product_placement", "self_promo"]},
                },
                "required": ["first_item", "last_item", "kind"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ad_spans"],
    "additionalProperties": False,
}


class ClaudeClassifier:
    name = "claude"

    def __init__(self, model: str):
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.fallback = HeuristicClassifier()
        self.last = "claude"

    def classify(self, items: list[Item], horizon: float) -> list[Span]:
        if not items:
            return []
        transcript = "\n".join(
            f"[{i}] ({it.start:.1f}-{it.end:.1f}) {it.text or f'<no clear speech for {it.end - it.start:.0f}s>'}"
            for i, it in enumerate(items)
        )
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": transcript}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except self.anthropic.APIStatusError as e:
            log.warning("claude classify failed (%s), using heuristic for this pass", e.status_code)
            self.last = "heuristic (fallback)"
            return self.fallback.classify(items, horizon)
        except self.anthropic.APIConnectionError as e:
            log.warning("claude unreachable (%s), using heuristic for this pass", e)
            self.last = "heuristic (fallback)"
            return self.fallback.classify(items, horizon)
        if resp.stop_reason != "end_turn":
            log.warning("claude stop_reason=%s, using heuristic for this pass", resp.stop_reason)
            self.last = "heuristic (fallback)"
            return self.fallback.classify(items, horizon)
        self.last = "claude"
        text = "".join(b.text for b in resp.content if b.type == "text")
        spans = []
        for s in json.loads(text)["ad_spans"]:
            lo, hi = max(0, s["first_item"]), min(len(items) - 1, s["last_item"])
            if lo <= hi:
                end = horizon if hi == len(items) - 1 else items[hi].end
                spans.append(Span(items[lo].start, end, s["kind"], self.name))
        return spans


# --------------------------------------------------------------------------- analyzer thread


class Analyzer(threading.Thread):
    def __init__(self, state: State, transcriber: Transcriber, classifier):
        super().__init__(name="analyzer", daemon=True)
        self.state, self.transcriber, self.classifier = state, transcriber, classifier

    def _next_chunk(self):
        with self.state.changed:
            while not self.state.stopping:
                pending = [s for s in self.state.segments if not s.analyzed]
                size = CHUNK_SEGMENTS if self.state.analyzed_until else FIRST_CHUNK_SEGMENTS
                if len(pending) >= size or (pending and self.state.ingest_done):
                    chunk = pending[:size]
                    # earlier audio is re-transcribed with the new chunk so words and ads aren't cut at chunk edges
                    context = [s for s in self.state.segments
                               if s.idx < chunk[0].idx and s.end > chunk[0].start - CONTEXT_AUDIO]
                    return context, chunk
                self.state.changed.wait(1.0)
        return None

    def run(self):
        while (got := self._next_chunk()) is not None:
            context, chunk = got
            region = context + chunk
            region_start = region[0].start
            t0 = time.monotonic()
            with self.state.lock:
                prompt = next((l.text for l in reversed(self.state.lines) if l.start < region_start and not l.noise), "")
            try:
                new_lines = self.transcriber.transcribe(read_pcm([s.path for s in region]), region_start, prompt)
            except Exception:
                log.exception("transcription failed for %s..%s", region[0].name, region[-1].name)
                new_lines = None
            t1 = time.monotonic()

            with self.state.lock:
                aired = self.state.published_until
                if new_lines is not None:
                    # replace the re-transcribed region, except what's already on air
                    kept = [l for l in self.state.lines if l.start < region_start or l.end <= aired]
                    kept_end = max((l.end for l in kept), default=0.0)
                    fresh = [l for l in new_lines if l.end > aired and l.start >= kept_end - 0.5]
                    self.state.lines = kept + fresh
                horizon = chunk[-1].end
                window = [l for l in self.state.lines if l.end >= horizon - CONTEXT_SECONDS]
            items = timeline(window, horizon)
            spans = self.classifier.classify(items, horizon)
            window_start = items[0].start if items else horizon
            with self.state.changed:
                self.state.auto_spans = [s for s in self.state.auto_spans if s.start < window_start] + spans
                self.state.relabel_lines()
                for s in chunk:
                    s.analyzed = True
                self.state.analyzed_until = chunk[-1].end
                self.state.changed.notify_all()
                shown = [l for l in self.state.lines if l.start >= chunk[0].start]
            for l in shown:
                log.info("%8.1f %s %s", l.start, "AD " if l.ad else ("~  " if l.noise else "   "), l.text)
            log.debug("chunk %s: stt %.1fs, classify %.1fs, spans %s", chunk[0].name, t1 - t0,
                      time.monotonic() - t1, [(round(s.start, 1), round(s.end, 1)) for s in spans])
