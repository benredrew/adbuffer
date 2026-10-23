"""Re-encode one segment with a soft blur + muffled audio over the ad intervals.

The censored look fades in over the first `ramp` seconds of an ad and back out
over `ramp` seconds after it ends. Timestamps are preserved (-copyts) so the
segment drops back into the HLS sequence without a discontinuity.
"""

import subprocess

from .ingest import encode_args

FONT = "/usr/share/fonts/liberation/LiberationSans-Regular.ttf"


def overlapping(intervals, seg_start, seg_end, ramp):
    return [(a, b) for a, b in intervals if a < seg_end and b + ramp > seg_start]


def censor_segment(src: str, dst: str, interval: tuple[float, float], ramp: float, height: int,
                   label: str = "Ad muted — back shortly"):
    a, b = interval
    fade_in, fade_out = a, b
    # K(t): 0 = clear broadcast, 1 = fully censored.
    k = f"clip((t-{a:.3f})/{ramp},0,1)*clip(({b + ramp:.3f}-t)/{ramp},0,1)"
    label = label.replace(":", r"\:").replace("'", "")
    graph = (
        "[0:v]split=2[clear][soft];"
        "[soft]boxblur=luma_radius=22:luma_power=3:chroma_radius=10:chroma_power=3,"
        "eq=saturation=0.35:brightness=-0.07,"
        f"drawtext=fontfile={FONT}:text='{label}':fontcolor=white@0.9:fontsize=h/24:"
        "x=(w-tw)/2:y=h-th-h/10:box=1:boxcolor=black@0.4:boxborderw=16,"
        "format=yuva420p,"
        f"fade=t=in:st={fade_in:.3f}:d={ramp}:alpha=1,"
        f"fade=t=out:st={fade_out:.3f}:d={ramp}:alpha=1[veil];"
        "[clear][veil]overlay=format=auto:eof_action=pass,format=yuv420p[v];"
        "[0:a]asplit=2[dry][wet];"
        f"[dry]volume=eval=frame:volume='1-{k}'[dry2];"
        f"[wet]lowpass=f=650,lowpass=f=650,volume=eval=frame:volume='0.3*{k}'[wet2];"
        "[dry2][wet2]amix=inputs=2:normalize=0:duration=first[a]"
    )
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-copyts", "-i", src,
         "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
         *encode_args(height), "-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", dst],
        check=True,
    )
