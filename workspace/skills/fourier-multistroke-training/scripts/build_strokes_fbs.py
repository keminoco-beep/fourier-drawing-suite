#!/usr/bin/env python
"""Build a per-stroke .fbs directly from a strokes JSON (no render)."""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.dirname(os.path.abspath(__file__))
def _find_ws():
    env = os.environ.get("FOURIER_WS")
    if env:
        return os.path.abspath(env)
    cur = HERE
    for _ in range(8):
        if os.path.exists(os.path.join(cur, "fourier-brushstroke", "scripts", "fbs.py")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.abspath(os.path.join(HERE, "..", ".."))

WS = _find_ws()
SKILL = os.path.join(WS, "fourier-brushstroke", "scripts")
for p in (HERE, SKILL):
    if p not in sys.path:
        sys.path.insert(0, p)

from fbs_core import io_format, preprocess, structdoc  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.util import WarningLog  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strokes", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--resolution", type=int, default=1536)
    ap.add_argument("--epsilon", type=float, default=0.08)
    ap.add_argument("--nmax", type=int, default=4096)
    ap.add_argument("--max-curves", type=int, default=40000)
    args = ap.parse_args()
    cfg = FBSConfig(target_resolution=int(args.resolution), epsilon_px=float(args.epsilon),
                    N_max=int(args.nmax), resample_points=4096, curve_tol=0.002,
                    max_curves=int(args.max_curves), supersample=2)
    warn = WarningLog()
    img = preprocess.load_and_prepare(args.input, cfg, warn)
    L = int(img["long_side"])
    data = json.load(open(args.strokes, encoding="utf-8"))
    strokes = []
    for d in data:
        pts = np.asarray(d["points"], dtype=np.float64)
        widths = np.asarray(d["widths"], dtype=np.float64)
        if len(pts) < 2:
            continue
        if d.get("closed"):
            pts = np.vstack([pts, pts[:1]])
            widths = np.concatenate([widths, widths[:1]])
        strokes.append({
            "points_px": pts * L,
            "widths_px": np.maximum(widths * L, 0.45),
            "color": np.asarray(d["color"], dtype=np.float64),
            "alpha": float(d.get("alpha", 1.0)),
            "layer": int(d.get("layer", 0)),
            "preset": "basic_round_hard", "shape": "round",
            "class": str(d.get("kind", "stroke")),
        })
    strokes.sort(key=lambda s: int(s["layer"]))
    doc = structdoc.build_doc_from_strokes(strokes, img, args.input, cfg, warn,
                                           method="single_fft_training_perstroke")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    io_format.write_fbs(doc, args.out)
    print(f"[build] {args.out} strokes={len(strokes)} bytes={os.path.getsize(args.out)} "
          f"fills={doc.get('stats', {}).get('fill_count')} contract={cfg.contract()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
