#!/usr/bin/env python
"""Pack a rendered .fbs into the portable .fdraw container and validate it."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# Resolve junctions/symlinks first: registered skills are reached through
# .dsh/skills/<skill> -> skills/<skill>, and walking up the junction path lands
# in .dsh/skills (which has no real fourier_prompt/ package) instead of the
# workspace root.  FOURIER_WS still overrides the auto-detection.
_REAL_HERE = os.path.dirname(os.path.realpath(__file__))


def _find_ws():
    env = os.environ.get("FOURIER_WS")
    if env:
        return os.path.abspath(env)
    cur = _REAL_HERE
    for _ in range(8):
        if os.path.exists(os.path.join(cur, "fourier-brushstroke", "scripts", "fbs.py")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.abspath(os.path.join(_REAL_HERE, "..", ".."))

WS = _find_ws()
FP = os.path.join(WS, "fourier-prompt")
if FP not in sys.path:
    sys.path.insert(0, FP)

from fourier_prompt import fdraw  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fbs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--meta", default=None, help="optional JSON file for meta")
    args = ap.parse_args()
    started = time.time()
    with open(args.fbs, encoding="utf-8") as fh:
        doc = json.load(fh)
    meta = {}
    if args.meta:
        meta = json.load(open(args.meta, encoding="utf-8"))
    data = fdraw.pack(doc, meta=meta or None, out=args.out)
    errors = fdraw.validate(args.out)
    info = fdraw.inspect(args.out)
    report = {
        "fbs": os.path.abspath(args.fbs),
        "fdraw": os.path.abspath(args.out),
        "packed_bytes": len(data),
        "validate_errors": errors,
        "zip_bytes": info.get("zip_bytes"),
        "uncompressed_bytes": info.get("uncompressed_bytes"),
        "entry_count": info.get("entry_count"),
        "root_sha256_ok": info.get("root_sha256_ok"),
        "seconds": round(time.time() - started, 1),
    }
    with open(os.path.splitext(args.out)[0] + ".report.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
