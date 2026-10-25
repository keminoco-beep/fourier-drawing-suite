#!/usr/bin/env python
"""Flow-tiled base generator: coverage + local direction, from the ground up.

The old base layer was scanline hatching at one angle per colour region.  It
left holes and produced long straight strokes across curved flow.  This module
replaces it with a small set of overlapping oriented cells:

  for each grid point inside the foreground:
      direction = local structure-tensor tangent
      stroke length ~ 2 x spacing, width ~ 1.3-1.5 x spacing
      colour    = source colour at the stroke

Multiple staggered passes guarantee coverage, while each stroke follows the
local flow.  Detail/drawn-line layers are merged from a supplied stroke file.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

import numpy as np
from PIL import Image

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
for p in (HERE, os.path.join(WS, "fourier-brushstroke", "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import hatch_gen as hgr  # noqa: E402
import hybrid_gen as hg  # noqa: E402


def load_strokes(path, skip=()):
    out = []
    for s in json.load(open(path, encoding="utf-8")):
        if str(s.get("kind", "")) in skip:
            continue
        d = dict(s)
        d["points"] = np.asarray(d["points"], dtype=np.float64)
        d["widths"] = np.asarray(d["widths"], dtype=np.float64)
        d["color"] = np.asarray(d.get("color", [0.5, 0.5, 0.5]), dtype=np.float64)
        out.append(d)
    return out


def save_strokes(path, strokes):
    payload = []
    for s in strokes:
        d = dict(s)
        d["points"] = np.round(np.asarray(d["points"]), 6).tolist()
        d["widths"] = np.round(np.asarray(d["widths"]), 7).tolist()
        d["color"] = np.round(np.asarray(d["color"]), 5).tolist()
        if d.get("gradient"):
            d["gradient"] = {
                "start": np.round(np.asarray(d["gradient"]["start"], float), 5).tolist(),
                "end": np.round(np.asarray(d["gradient"]["end"], float), 5).tolist(),
            }
        d["alpha"] = float(d.get("alpha", 1.0))
        d["layer"] = int(d.get("layer", 0))
        d["kind"] = str(d.get("kind", ""))
        d["closed"] = False
        payload.append(d)
    json.dump(payload, open(path, "w", encoding="utf-8"), ensure_ascii=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--detail", default=None, help="detail/line strokes to merge (base kinds removed)")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--work-res", type=int, default=1280)
    ap.add_argument("--spacings", default="16,12,10",
                    help="comma-separated grid spacings (px); later passes fill gaps")
    ap.add_argument("--width-factor", type=float, default=1.35)
    ap.add_argument("--length-factor", type=float, default=1.9)
    ap.add_argument("--seed", type=int, default=20261101)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    t0 = time.time()
    rgb = hg.load_image(args.input, args.work_res)
    H, W = rgb.shape[:2]
    L = float(max(H, W))
    fg = ~np.all(rgb > 0.945, axis=2)
    jxx, jyy, jxy = hg.orientation_field(rgb)
    theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy) + math.pi / 2.0
    rng = np.random.default_rng(args.seed)
    flow = []
    spacings = [float(x) for x in str(args.spacings).split(",") if x.strip()]
    for pi, spacing in enumerate(spacings):
        off = (spacing * 0.5 + (pi % 2) * spacing * 0.37) % spacing
        for y in np.arange(off, H, spacing):
            for x in np.arange(off, W, spacing):
                ix, iy = int(x), int(y)
                if ix < 0 or iy < 0 or ix >= W or iy >= H or not fg[iy, ix]:
                    continue
                th = float(theta[iy, ix] + rng.normal(0.0, 0.10))
                ln = float(rng.uniform(args.length_factor * 0.8, args.length_factor * 1.2) * spacing)
                wpx = float(rng.uniform(args.width_factor * 0.85, args.width_factor * 1.15) * spacing)
                ca, sa = math.cos(th), math.sin(th)
                pts = np.array([
                    [x - ca * ln / 2, y - sa * ln / 2],
                    [x + rng.normal(0, 1.0), y + rng.normal(0, 1.0)],
                    [x + ca * ln / 2, y + sa * ln / 2],
                ])
                pts[:, 0] = np.clip(pts[:, 0], 0, W - 1)
                pts[:, 1] = np.clip(pts[:, 1], 0, H - 1)
                xi = np.clip(pts[:, 0].astype(int), 0, W - 1)
                yi = np.clip(pts[:, 1].astype(int), 0, H - 1)
                col = np.median(rgb[yi, xi], axis=0)
                flow.append({
                    "points": pts / L,
                    "widths": np.full(3, wpx / L),
                    "color": np.clip(col, 0, 1).tolist(),
                    "alpha": 1.0,
                    "layer": 0,
                    "kind": "flow_base",
                    "closed": False,
                })
    detail = load_strokes(args.detail, skip={"base"}) if args.detail else []
    out = []
    for s in flow + detail:
        d = dict(s)
        d["points"] = np.asarray(d["points"], dtype=np.float64)
        d["widths"] = np.asarray(d["widths"], dtype=np.float64)
        d["color"] = np.asarray(d["color"], dtype=np.float64)
        out.append(d)
    out.sort(key=lambda s: int(s.get("layer", 0)))
    ren = hgr._render_strokes(out, H, W, ss=1)
    psnr = hgr._psnr(rgb, ren)
    ssim = hgr._ssim(rgb, ren)
    white = np.all(ren > 0.99, axis=2)
    white_in_fg = int((white & fg).sum())
    png = os.path.join(args.outdir, f"flow_tile_R{args.work_res}.png")
    Image.fromarray((np.clip(ren, 0, 1) * 255).astype(np.uint8)).save(png)
    js = os.path.join(args.outdir, "flow_tile_strokes.json")
    save_strokes(js, out)
    report = {
        "input": os.path.abspath(args.input), "work_res": args.work_res,
        "flow_base": len(flow), "detail": len(detail), "strokes_out": len(out),
        "spacings": spacings, "width_factor": args.width_factor,
        "length_factor": args.length_factor,
        "white_px_in_fg": white_in_fg, "fg_px": int(fg.sum()),
        "psnr": round(psnr, 4), "ssim": round(ssim, 5),
        "png": os.path.abspath(png), "strokes": os.path.abspath(js),
        "seconds": round(time.time() - t0, 1),
    }
    json.dump(report, open(os.path.join(args.outdir, "flow_tile_report.json"), "w",
                           encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
