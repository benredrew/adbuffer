"""Pull the source stream, normalise it, and cut it into fixed-length TS segments."""

import json
import logging
import os
import re
import subprocess
import threading
import time

from .state import Segment, State

log = logging.getLogger("ingest")

SEGMENT_SECONDS = 2
FPS = 30
MEDIA_EXT = re.compile(r"\.(m3u8|ts|mp4|mkv|webm|mov|flv|mpd)(\?|$)", re.I)


def encode_args(height: int) -> list[str]:
    """Encoder settings shared by ingest and the censor pass so that censored
    segments splice seamlessly between untouched ones."""
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
        "-pix_fmt", "yuv420p", "-g", str(FPS * SEGMENT_SECONDS),
        "-keyint_min", str(FPS * SEGMENT_SECONDS), "-sc_threshold", "0",
        "-b:v", "3500k" if height >= 720 else "1800k", "-maxrate", "4500k", "-bufsize", "7000k",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
    ]


def resolve(page_url: str, height: int) -> tuple[list[dict], bool]:
    """Ask yt-dlp for the media URLs behind a page (YouTube, Twitch, ...). Returns the
    format(s) to read (video+audio, or one muxed stream) and whether it's a live stream."""
    out = subprocess.run(
        ["yt-dlp", "-q", "--no-warnings", "-J", "--no-playlist",
         "-S", f"res:{height},vcodec:h264,acodec:aac", "-f", "bv*+ba/b", page_url],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "yt-dlp failed")
    info = json.loads(out.stdout)
    return info.get("requested_formats") or [info], bool(info.get("is_live"))


# Recorded media is read a bit faster than real time, so the analysed lookahead keeps growing
# while it plays: playback starts after a short lead-in instead of the full delay.
VOD_READRATE = 1.5
VOD_BURST = 8  # seconds read at full speed up front


def _pace(vod: bool) -> list[str]:
    return ["-readrate", str(VOD_READRATE), "-readrate_initial_burst", str(VOD_BURST)] if vod else []


def _input_args(fmt: dict, vod: bool) -> list[str]:
    args = _pace(vod)
    headers = fmt.get("http_headers") or {}
    if ua := headers.pop("User-Agent", None):
        args += ["-user_agent", ua]
    if headers:
        args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    if fmt.get("protocol", "").startswith(("http", "https")) and "m3u8" not in fmt.get("protocol", ""):
        args += ["-reconnect", "1", "-reconnect_streamed", "1"]
    return args + ["-i", fmt["url"]]


def start_ingest(source: str, buf_dir: str, height: int) -> tuple[list[subprocess.Popen], bool]:
    """Start ffmpeg; returns its process and whether the source is live (can't be read ahead)."""
    ff = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    audio = "0:a:0"
    live = False
    if os.path.exists(source):
        ff += [*_pace(True), "-i", source]
    elif MEDIA_EXT.search(source):
        live = bool(re.search(r"\.(m3u8|ts|flv|mpd)(\?|$)", source, re.I))
        ff += [*_pace(not live), "-reconnect", "1", "-reconnect_streamed", "1", "-i", source]
    else:
        # Page URLs are resolved to their media URLs, which ffmpeg reads directly. (Piping
        # `yt-dlp -o -` breaks whenever a site only has separate video and audio streams.)
        fmts, live = resolve(source, height)
        for f in fmts:
            ff += _input_args(f, vod=not live)  # live streams pace themselves
        if len(fmts) > 1:
            audio = "1:a:0"
    ff += [
        "-map", "0:v:0", "-map", audio,
        "-vf", f"scale=-2:{height},fps={FPS}",
        *encode_args(height),
        "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SECONDS})",
        "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-segment_format", "mpegts",
        "-segment_list", os.path.join(buf_dir, "list.csv"), "-segment_list_type", "csv",
        "-segment_list_flags", "+live",
        os.path.join(buf_dir, "seg_%06d.ts"),
    ]
    return [subprocess.Popen(ff)], live


def probe(path: str) -> tuple[float, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=start_time,duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    start, dur = out.split(",")[:2]
    return float(start), float(dur)


def watch_segments(state: State, buf_dir: str, ffmpeg: subprocess.Popen):
    """Tail the segment list ffmpeg appends to after each segment is closed."""
    list_path = os.path.join(buf_dir, "list.csv")
    seen = 0
    while not state.stopping:
        done = ffmpeg.poll() is not None
        rows = []
        if os.path.exists(list_path):
            with open(list_path) as f:
                rows = [r for r in f.read().splitlines() if r.strip()]
        for row in rows[seen:]:
            name = row.split(",")[0]
            path = os.path.join(buf_dir, name)
            start, dur = probe(path)
            state.add_segment(Segment(idx=seen, name=name, path=path, start=start, duration=dur))
            log.debug("segment %s start=%.2f dur=%.2f", name, start, dur)
            seen += 1
        if done:
            log.info("ingest finished (exit %s) after %d segments", ffmpeg.returncode, seen)
            with state.changed:
                state.ingest_done = True
                state.changed.notify_all()
            return
        time.sleep(0.25)


def run_watcher(state: State, buf_dir: str, ffmpeg: subprocess.Popen) -> threading.Thread:
    t = threading.Thread(target=watch_segments, args=(state, buf_dir, ffmpeg), name="watcher", daemon=True)
    t.start()
    return t
