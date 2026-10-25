#!/usr/bin/env python
"""Add Van Gogh-like thick short dabs to an existing pure-stroke drawing.

Short (4-12 px), thick (3-7 px) directional strokes are placed on a jittered
grid, oriented by the local structure tensor and coloured from the source.
They are additive: existing strokes are preserved.  Content-agnostic.
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
    ap.add_argument("--work-res", type=int, default=1536)
    ap.add_argument("--count", type=int, default=12000)
    ap.add_argument("--len-min", type=float, default=4.0)
    ap.add_argument("--len-max", type=float, default=12.0)
    ap.add_argument("--width-min", type=float, default=3.0)
    ap.add_argument("--width-max", type=float, default=7.0)
    ap.add_argument("--layer", type=int, default=4)
    ap.add_argument("--seed", type=int, default=20261022)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    t0 = time.time()
    rng = np.random.default_rng(args.seed)
    rgb = hg.load_image(args.input, args.work_res)
    H, W = rgb.shape[:2]
    L = float(max(H, W))
    fg = ~np.all(rgb > 0.945, axis=2)
    jxx, jyy, jxy = hg.orientation_field(rgb, sigma_t=1.2, sigma_g=0.7)
    theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy) + math.pi / 2.0
    coh = np.sqrt((jxx - jyy) ** 2 + 4 * jxy ** 2) / np.maximum(jxx + jyy, 1e-9)
    # candidate seeds: a jittered grid; prefer coherent/textured areas but keep
    # enough coverage for flat wheat/sky fields.
    seeds = []
    step = 4
    for y in range(4, H - 4, step):
        for x in range(4, W - 4, step):
            if fg[y, x] and rng.random() < 0.55:
                seeds.append((x + rng.uniform(-1.8, 1.8), y + rng.uniform(-1.8, 1.8)))
    rng.shuffle(seeds)
    dabs = []
    for x0, y0 in seeds:
        if len(dabs) >= args.count:
            break
        xi = int(np.clip(round(x0), 0, W - 1)); yi = int(np.clip(round(y0), 0, H - 1))
        if not fg[yi, xi]:
            continue
        th = float(theta[yi, xi] + rng.normal(0.0, 0.22))
        ln = float(rng.uniform(args.len_min, args.len_max))
        wd = float(rng.uniform(args.width_min, args.width_max))
        ca, sa = math.cos(th), math.sin(th)
        # 3-point shallow curve
        p0 = np.array([x0 - ca * ln / 2, y0 - sa * ln / 2])
        p1 = np.array([x0 + rng.normal(0, 0.6), y0 + rng.normal(0, 0.6)])
        p2 = np.array([x0 + ca * ln / 2, y0 + sa * ln / 2])
        pts = np.stack([p0, p1, p2], axis=0)
        # keep all three points on the foreground
        pxi = np.clip(pts[:, 0].astype(int), 0, W - 1); pyi = np.clip(pts[:, 1].astype(int), 0, H - 1)
        if fg[pyi, pxi].mean() < 0.66:
            continue
        # local colour, slightly modulated by the source centre
        col = np.median(rgb[pyi, pxi], axis=0)
        col = np.clip(col + rng.normal(0, 0.012, 3), 0, 1)
        widths = np.array([0.78, 1.0, 0.78]) * wd / L
        dabs.append({
            "points": (pts / L).tolist(),
            "widths": widths.tolist(),
            "color": col.tolist(),
            "alpha": 0.94,
            "layer": int(args.layer),
            "kind": "thick_dab",
            "closed": False,
        })
    print(f"[dabs] seeds={len(seeds)} dabs={len(dabs)}", flush=True)
    base = json.load(open(args.strokes, encoding="utf-8"))
    for s in base:
        s["points"] = np.asarray(s["points"], dtype=np.float64)
        s["widths"] = np.asarray(s["widths"], dtype=np.float64)
        s["color"] = np.asarray(s["color"], dtype=np.float64)
    strokes = base + dabs
    strokes.sort(key=lambda s: int(s.get("layer", 0)))
    ren = hgr._render_strokes(strokes, H, W, ss=1)
    psnr = hgr._psnr(rgb, ren)
    ssim = hgr._ssim(rgb, ren)
    from PIL import Image
    png = os.path.join(args.outdir, f"dabs_R{args.work_res}.png")
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
    js = os.path.join(args.outdir, "dabs_strokes.json")
    json.dump(payload, open(js, "w", encoding="utf-8"), ensure_ascii=False)
    report = {"input": os.path.abspath(args.input), "work_res": args.work_res,
              "base_strokes": len(base), "dabs": len(dabs), "total_strokes": len(strokes),
              "psnr": round(psnr, 4), "ssim": round(ssim, 5), "png": os.path.abspath(png),
              "strokes": os.path.abspath(js), "seconds": round(time.time() - t0, 1)}
    json.dump(report, open(os.path.join(args.outdir, "dabs_report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
