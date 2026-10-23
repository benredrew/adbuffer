"""Viewer reports of bad ad detection, investigated by a headless Claude Code session.

Pressing `r` in the player files a report at the current position. A bundle of evidence
(transcript around that point with word timings, detector and SponsorBlock spans, what was
softened, stills from the published video) goes to ~/.local/share/adbuffer/reports/<id>/,
then `claude -p` investigates it inside the adbuffer repository. It writes REPORT.md and, for
small low-risk fixes, edits the detector, adds the case to the regression suite and commits
only if every case passes. It can only use file tools and a few commands, limited to the
repository and the bundle.
"""

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime

from .library import DATA_DIR

log = logging.getLogger("reports")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
CONTEXT = 150      # seconds of transcript either side of the reported moment
FRAME_SPAN = 30    # stills from this many seconds either side...
FRAME_STEP = 5     # ...every this many seconds

KINDS = {
    "missed": "An ad played without being softened.",
    "false": "Normal content was softened as if it were an ad.",
    "boundary": "An ad was caught, but softening started or ended at the wrong time.",
}

PROMPT = """A viewer reported a problem with adbuffer's ad detection. Investigate it.

Report bundle: {dir}
- report.json: what the viewer flagged ("kind", "note", "stream_time"), the transcript around it
  with word timings, the detector's spans, SponsorBlock's spans, and the intervals that were softened.
- frames/: stills from the published video around the report (frame_-30.jpg ... frame_+30.jpg,
  seconds relative to the reported moment), already softened wherever adbuffer softened them.

This directory is the adbuffer repository. Detection lives in adbuffer/detect.py (transcription,
sentence timeline, HeuristicClassifier cue lists and block logic, Claude classifier prompt);
also state.py, sponsorblock.py, publish.py, censor.py. Times in report.json are stream time;
video time = stream time - origin.

1. Work out what went wrong and why: transcription, cue lists, block start/end logic, SponsorBlock,
   timing, or something else. Replay the transcript through the heuristic with
   `.venv/bin/python tests/replay.py {dir}/report.json`.
2. Write {dir}/REPORT.md. Its first line is a one-sentence summary (shown as a desktop
   notification). Then: what the viewer saw, the root cause with evidence, and the fix, made or proposed.
3. If the fix is small and low-risk (a cue phrase, a regex, a threshold, a narrow logic fix),
   make it, add this report to the regression suite with
   `.venv/bin/python tests/add_case.py {dir}/report.json` (pass --expect-ad/--expect-clear times
   if the defaults don't describe the right outcome), run `.venv/bin/python tests/run_cases.py`,
   and commit with `git add -A && git commit -m "auto-fix: ..."` only if every case passes.
   Otherwise change no code and describe the proposed change in REPORT.md.

Don't restart services, and don't touch anything outside this repository and the report bundle."""

ALLOWED_TOOLS = [
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(.venv/bin/python tests/*)", "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)",
    "Bash(git add -A*)", "Bash(git commit*)",
]


def find_claude() -> str | None:
    extra = [os.path.expanduser(p) for p in ("~/.local/bin", "~/.local/share/mise/shims",
                                              "~/.local/share/mise/installs/claude/latest")]
    return shutil.which("claude", path=os.pathsep.join([os.environ.get("PATH", ""), *extra]))


def notify(title: str, body: str):
    if shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", "adbuffer", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Reporter(threading.Thread):
    def __init__(self):
        super().__init__(name="reporter", daemon=True)
        self.jobs: list[dict] = []
        self.queue: queue.Queue = queue.Queue()
        self.lock = threading.Lock()

    # ------------------------------------------------------------ filing

    def file(self, session, pts: float, kind: str, note: str) -> dict:
        rid = datetime.now().strftime("%Y%m%d-%H%M%S")
        d = os.path.join(REPORTS_DIR, rid)
        os.makedirs(os.path.join(d, "frames"), exist_ok=True)
        st = session.state
        with st.lock:
            origin = st.segments[0].start if st.segments else 0.0
            lines = [
                {"start": l.start, "end": l.end, "text": l.text, "noise": l.noise, "words": l.words}
                for l in st.lines if pts - CONTEXT <= l.end and l.start <= pts + CONTEXT
            ]
            report = {
                "id": rid,
                "kind": kind,
                "kind_description": KINDS.get(kind, kind),
                "note": note,
                "source": session.source,
                "title": session.title,
                "stream_time": pts,
                "video_time": pts - origin,
                "origin": origin,
                "horizon": st.analyzed_until,
                "published_until": st.published_until,
                "classifier": session.classifier.name,
                "detector_spans": [vars(x) for x in st.auto_spans],
                "sponsorblock_spans": [vars(x) for x in st.external_spans],
                "lines": lines,
                "git_rev": git_rev(),
            }
            segs = [(s.start, s.end, os.path.join(session.out_dir, s.name)) for s in st.segments
                    if s.end <= st.published_until]
        report["softened_intervals"] = st.ad_intervals(merge_gap=5)  # takes the lock itself
        with open(os.path.join(d, "report.json"), "w") as f:
            json.dump(report, f, indent=1)
        grab_frames(segs, pts, os.path.join(d, "frames"))

        job = {"id": rid, "dir": d, "kind": kind, "video_time": pts - origin, "title": session.title,
               "status": "queued", "summary": "", "created": time.time()}
        with self.lock:
            self.jobs.append(job)
        self.queue.put(job)
        log.info("report %s filed (%s at %.1f)", rid, kind, pts - origin)
        return job

    # ------------------------------------------------------------ investigating

    def run(self):
        while True:
            job = self.queue.get()
            claude = find_claude()
            if not claude:
                self._finish(job, "failed", "Claude Code (`claude`) isn't installed or on PATH.")
                continue
            job["status"] = "running"
            rev_before = git_rev()
            notify("adbuffer", f"Claude is looking into your report at {fmt_t(job['video_time'])}")
            out_path = os.path.join(job["dir"], "claude-output.txt")
            try:
                with open(out_path, "w") as out:
                    proc = subprocess.run(
                        [claude, "-p", PROMPT.format(dir=job["dir"]),
                         "--permission-mode", "dontAsk", "--allowedTools", *ALLOWED_TOOLS,
                         "--add-dir", job["dir"], "--name", f"adbuffer report {job['id']}"],
                        cwd=REPO, stdout=out, stderr=subprocess.STDOUT, timeout=1800,
                    )
                ok = proc.returncode == 0
            except subprocess.TimeoutExpired:
                ok = False
            summary = first_line(os.path.join(job["dir"], "REPORT.md"))
            if ok and summary:
                if git_rev() != rev_before:
                    job["fixed"] = True
                    summary += " Fix committed; restart adbuffer to use it."
                self._finish(job, "done", summary)
            else:
                self._finish(job, "failed", summary or f"The investigation didn't finish; see {out_path}")

    def _finish(self, job: dict, status: str, summary: str):
        job["status"], job["summary"] = status, summary
        notify("adbuffer report " + ("ready" if status == "done" else "failed"), summary)
        log.info("report %s %s: %s", job["id"], status, summary)

    def summary(self) -> dict:
        with self.lock:
            recent = [{k: j.get(k) for k in ("id", "kind", "status", "summary", "video_time", "title", "dir", "fixed")}
                      for j in self.jobs[-5:]]
        active = next((j for j in reversed(recent) if j["status"] in ("queued", "running")), None)
        return {"active": active, "recent": recent}


def grab_frames(segs: list[tuple[float, float, str]], pts: float, out_dir: str):
    for rel in range(-FRAME_SPAN, FRAME_SPAN + 1, FRAME_STEP):
        t = pts + rel
        seg = next((s for s in segs if s[0] <= t < s[1]), None)
        if not seg or not os.path.exists(seg[2]):
            continue
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{t - seg[0]:.3f}", "-i", seg[2],
             "-frames:v", "1", "-vf", "scale=480:-2", os.path.join(out_dir, f"frame_{rel:+03d}.jpg")],
            check=False,
        )


def git_rev() -> str:
    out = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    return out.stdout.strip()


def first_line(path: str) -> str:
    try:
        with open(path) as f:
            return next((l.strip().lstrip("# ").strip() for l in f if l.strip()), "")
    except OSError:
        return ""


def fmt_t(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
