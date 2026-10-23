"""Run every regression case in tests/cases/ through the heuristic. Exit code 1 on any failure.

    .venv/bin/python tests/run_cases.py
"""

import glob
import os
import sys

from common import CASES, classify, in_spans, load

failed = 0
for path in sorted(glob.glob(os.path.join(CASES, "*.json"))):
    case = load(path)
    _, spans = classify(case)
    origin = case.get("origin", 0.0)
    for check in case["expect"]:
        ok = in_spans(check["t"], spans) == check["ad"]
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {os.path.basename(path)}: {'AD' if check['ad'] else 'clear'} at "
              f"{check['t'] - origin:.1f}s  {case.get('note', '')}")
print(f"\n{'all passed' if not failed else f'{failed} failed'}")
sys.exit(1 if failed else 0)
