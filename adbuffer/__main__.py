"""adbuffer: buffer a live stream, soften ads and sponsor reads, re-serve it as HLS.

    python -m adbuffer [url-or-file] [--delay 45] [--port 8090] [--classifier auto]

Runs the app server; with a source, starts watching it right away.
"""

import argparse
import logging
import os
import shutil

from aiohttp import web

from .detect import ClaudeClassifier, HeuristicClassifier, Transcriber
from .library import Library
from .reports import Reporter
from .server import make_app
from .session import WORK_ROOT, Sessions


def main():
    p = argparse.ArgumentParser(prog="adbuffer", description=__doc__.split("\n")[0])
    p.add_argument("source", nargs="?", help="stream URL (HLS/TS/RTMP, or a YouTube/Twitch/etc. page) or a local file")
    p.add_argument("--delay", type=float, default=45, help="target lookahead in seconds; playback starts sooner and the lookahead grows toward this")
    p.add_argument("--ramp", type=float, default=1.0, help="seconds to fade into / out of the censored look")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--height", type=int, default=720, help="output video height")
    p.add_argument("--classifier", choices=["auto", "heuristic", "claude"], default="auto",
                   help="auto = Claude when Anthropic credentials are set, else keyword heuristic")
    p.add_argument("--claude-model", default="claude-opus-5-5")
    p.add_argument("--whisper-model", default="small.en")
    p.add_argument("--device", choices=["auto", "cpu", "cuda"], default="cpu")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)-8s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("faster_whisper", "httpx", "httpx2", "httpcore", "httpcore2", "huggingface_hub", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("adbuffer")

    use_claude = args.classifier == "claude" or (
        args.classifier == "auto" and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    )
    classifier = ClaudeClassifier(args.claude_model) if use_claude else HeuristicClassifier()
    log.info("classifier: %s", f"claude ({args.claude_model})" if use_claude else "keyword heuristic")

    shutil.rmtree(WORK_ROOT, ignore_errors=True)  # segments left behind by a crashed run
    sessions = Sessions(lambda: Transcriber(args.whisper_model, args.device), classifier,
                        args.delay, args.ramp, args.height)
    sessions.preload()  # load Whisper now so the first video doesn't wait for it
    if args.source:
        sessions.start(args.source)

    log.info("app at http://%s:%d/", args.host, args.port)
    try:
        reporter = Reporter()
        reporter.start()
        web.run_app(make_app(sessions, Library(), reporter), host=args.host, port=args.port, print=None)
    finally:
        sessions.stop()


if __name__ == "__main__":
    main()
