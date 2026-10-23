"""Crowd-sourced sponsor timestamps for YouTube videos (https://sponsor.ajay.app).

Uses the hash-prefix endpoint, so the server never learns which video is being watched.
"""

import hashlib
import json
import logging
import re
import threading
import urllib.request
from urllib.parse import parse_qs, urlparse

from .state import Span, State

log = logging.getLogger("sponsorblock")

API = "https://sponsor.ajay.app/api/skipSegments/{prefix}?categories={cats}"
CATEGORIES = ["sponsor", "selfpromo"]
REFRESH = 300  # new submissions show up while people watch, so look again now and then


def youtube_id(url: str) -> str | None:
    u = urlparse(url)
    host = (u.hostname or "").removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        vid = u.path.strip("/").split("/")[0]
    elif host.endswith("youtube.com"):
        vid = parse_qs(u.query).get("v", [""])[0]
        if not vid and (m := re.match(r"/(shorts|live|embed)/([\w-]+)", u.path)):
            vid = m.group(2)
    else:
        return None
    return vid if re.fullmatch(r"[\w-]{11}", vid or "") else None


def fetch(video_id: str) -> list[dict]:
    prefix = hashlib.sha256(video_id.encode()).hexdigest()[:4]
    url = API.format(prefix=prefix, cats=json.dumps(CATEGORIES, separators=(",", ":")))
    req = urllib.request.Request(url, headers={"User-Agent": "adbuffer"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            videos = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # no segments for any video with this prefix
            return []
        raise
    return next((v["segments"] for v in videos if v.get("videoID") == video_id), [])


class SponsorBlockWatcher(threading.Thread):
    def __init__(self, state: State, video_id: str):
        super().__init__(name="sponsorblock", daemon=True)
        self.state, self.video_id = state, video_id

    def run(self):
        self.state.sponsorblock = "looking up"
        with self.state.changed:
            while not self.state.segments and not self.state.stopping and not self.state.ingest_done:
                self.state.changed.wait(1.0)
            if not self.state.segments:
                return
            offset = self.state.segments[0].start  # video time 0 in stream (PTS) time
        while not self.state.stopping:
            try:
                segs = fetch(self.video_id)
                spans = [Span(offset + s["segment"][0], offset + s["segment"][1],
                              "sponsor" if s["category"] == "sponsor" else "self-promo", "sponsorblock")
                         for s in segs if s.get("actionType", "skip") == "skip"]
                with self.state.changed:
                    self.state.external_spans = spans
                    self.state.sponsorblock = f"{len(spans)} segment{'s' * (len(spans) != 1)}" if spans else "nothing submitted"
                    self.state.relabel_lines()
                log.info("%s: %d segment(s) %s", self.video_id, len(spans),
                         [(round(s.start - offset, 1), round(s.end - offset, 1)) for s in spans])
            except Exception as e:
                log.warning("lookup failed for %s: %s", self.video_id, e)
                if self.state.sponsorblock == "looking up":
                    self.state.sponsorblock = "unreachable"
            with self.state.changed:
                self.state.changed.wait_for(lambda: self.state.stopping or self.state.ingest_done, timeout=REFRESH)
                if self.state.ingest_done:
                    return
