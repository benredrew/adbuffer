"""Replay a report's transcript through the heuristic and show what it decides.

    .venv/bin/python tests/replay.py <report.json or case.json>
"""

import sys

from common import classify, in_spans, load

data = load(sys.argv[1])
items, spans = classify(data)
origin = data.get("origin", 0.0)
print(f"spans (video time): {[(round(s.start - origin, 1), round(s.end - origin, 1)) for s in spans]}")
for it in items:
    mark = "AD " if in_spans((it.start + it.end) / 2, spans) else "   "
    print(f"{it.start - origin:8.1f} {mark} {it.text or f'<no clear speech {it.end - it.start:.0f}s>'}")
if "stream_time" in data:
    t = data["stream_time"]
    print(f"\nreported {data['kind']} at {t - origin:.1f}s: heuristic now says {'AD' if in_spans(t, spans) else 'clear'}")
for check in data.get("expect", []):
    got = in_spans(check["t"], spans)
    print(f"expect {'AD' if check['ad'] else 'clear'} at {check['t'] - origin:.1f}s: {'ok' if got == check['ad'] else 'FAIL'}")
