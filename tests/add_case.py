"""Turn a report into a regression case in tests/cases/.

    .venv/bin/python tests/add_case.py <report.json> [--expect-ad T ...] [--expect-clear T ...]

Times are video time (seconds into the video). Without flags: a "missed" report expects an ad at
the reported moment and a "false" report expects it clear. "boundary" reports need explicit flags.
"""

import argparse
import json
import os

from common import CASES, load

p = argparse.ArgumentParser()
p.add_argument("report")
p.add_argument("--expect-ad", type=float, action="append", default=[])
p.add_argument("--expect-clear", type=float, action="append", default=[])
a = p.parse_args()

r = load(a.report)
origin = r.get("origin", 0.0)
expect = [{"t": origin + t, "ad": True} for t in a.expect_ad] + [{"t": origin + t, "ad": False} for t in a.expect_clear]
if not expect:
    if r["kind"] == "boundary":
        raise SystemExit("boundary reports need --expect-ad / --expect-clear times")
    expect = [{"t": r["stream_time"], "ad": r["kind"] == "missed"}]
case = {k: r[k] for k in ("id", "kind", "note", "source", "origin", "horizon", "lines")}
case["expect"] = expect
os.makedirs(CASES, exist_ok=True)
path = os.path.join(CASES, f"{r['id']}.json")
with open(path, "w") as f:
    json.dump(case, f, indent=1)
print(f"wrote {path} with {len(expect)} expectation(s)")
