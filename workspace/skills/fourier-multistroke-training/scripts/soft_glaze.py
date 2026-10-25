#!/usr/bin/env python
"""Top-layer soft glaze: broad translucent ribbons over the whole painting.

Hazy impressionist images (Monet) need a final atmospheric unification.  The
glaze is deliberately wide (30-65 px) and low alpha (0.35), so it smooths
segmentation/hatch boundaries without erasing detail.  Additive, content-agnostic.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

import numpy as np

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strokes", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--work-res", type=int, default=1408)
    ap.add_argument("--count", type=int, default=0, help="0 = auto 1000 * megapixels")
    ap.add_argument("--alpha", type=float, default=0.35)
    ap.add_argument("--width-min", type=float, default=30.0)
    ap.add_argument("--width-max", type=float, default=65.0)
    ap.add_argument("--layer", type=int, default=16)
    ap.add_argument("--seed", type=int, default=20261024)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    t0 = time.time()
    rng = np.random.default_rng(args.seed)
    rgb = hg.load_image(args.input, args.work_res)
    H, W = rgb.shape[:2]
    L = float(max(H, W))
    fg = ~np.all(rgb > 0.945, axis=2)
    scale = (H * W) / (1280.0 * 1280.0)
    count = args.count or int(round(1000 * scale))
    blur = hg.gaussian_blur(rgb.astype(np.float32), 8.0).astype(np.float64)
    jxx, jyy, jxy = hg.orientation_field(blur, sigma_t=2.0, sigma_g=1.2)
    theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy) + math.pi / 2.0
    base = json.load(open(args.strokes, encoding="utf-8"))
    for s in base:
        s["points"] = np.asarray(s["points"], dtype=np.float64)
        s["widths"] = np.asarray(s["widths"], dtype=np.float64)
        s["color"] = np.asarray(s["color"], dtype=np.float64)
    ys = np.arange(10, H - 10, 16)
    xs = np.arange(10, W - 10, 16)
    seeds = [(int(x), int(y)) for y in ys for x in xs if fg[y, x]]
    rng.shuffle(seeds)
    add = []
    for x0, y0 in seeds:
        if len(add) >= count:
            break
        th = float(theta[y0, x0] + rng.normal(0, 0.20))
        ln = float(rng.uniform(50.0, 110.0)); wd = float(rng.uniform(args.width_min, args.width_max))
        ca, sa = math.cos(th), math.sin(th)
        pts = np.array([
            [x0 - ca * ln / 2, y0 - sa * ln / 2],
            [x0 + rng.normal(0, 2.0), y0 + rng.normal(0, 2.0)],
            [x0 + ca * ln / 2, y0 + sa * ln / 2],
        ])
        xi = np.clip(pts[:, 0].astype(int), 0, W - 1)
        yi = np.clip(pts[:, 1].astype(int), 0, H - 1)
        col = np.median(blur[yi, xi], axis=0)
        add.append({
            "points": (pts / L).tolist(),
            "widths": np.full(3, wd / L).tolist(),
            "color": np.clip(col, 0, 1).tolist(),
            "alpha": float(args.alpha),
            "layer": int(args.layer),
            "kind": "soft_glaze",
            "closed": False,
        })
    strokes = base + add
    strokes.sort(key=lambda s: int(s.get("layer", 0)))
    ren = hgr._render_strokes(strokes, H, W, ss=1)
    psnr = hgr._psnr(rgb, ren); ssim = hgr._ssim(rgb, ren)
    from PIL import Image
    png = os.path.join(args.outdir, f"glaze_R{args.work_res}.png")
    Image.fromarray((np.clip(ren, 0, 1) * 255).astype(np.uint8)).save(png)
    payload = []
    for s in strokes:
        d = dict(s)
        d["points"] = np.round(np.asarray(d["points"]), 6).tolist()
        d["widths"] = np.round(np.asarray(d["widths"]), 7).tolist()
        d["color"] = np.round(np.asarray(d["color"]), 5).tolist()
        d["alpha"] = float(d.get("alpha", 1.0))
        d["layer"] = int(d.get("layer", 0))
        d["kind"] = str(d.get("kind", ""))
        d["closed"] = False
        payload.append(d)
    js = os.path.join(args.outdir, "glaze_strokes.json")
    json.dump(payload, open(js, "w", encoding="utf-8"), ensure_ascii=False)
    report = {"input": os.path.abspath(args.input), "base_strokes": len(base),
              "glaze": len(add), "total_strokes": len(strokes),
              "psnr": round(psnr, 4), "ssim": round(ssim, 5),
              "png": os.path.abspath(png), "strokes": os.path.abspath(js),
              "seconds": round(time.time() - t0, 1)}
    json.dump(report, open(os.path.join(args.outdir, "glaze_report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
