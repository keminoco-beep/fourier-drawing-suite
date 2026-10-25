#!/usr/bin/env python
"""Broad soft-atmosphere underpainting for hazy/low-contrast paintings.

Impressionist haze is mostly low-frequency colour, while the hard segmentation /
hatching pipeline emphasises local regions.  This pass adds wide, translucent,
smoothly coloured ribbons along the blurred structure-tensor direction; it is
additive and content-agnostic.
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
    ap.add_argument("--count", type=int, default=0, help="0 = auto 2200 * megapixels")
    ap.add_argument("--under-layer", type=int, default=1)
    ap.add_argument("--seed", type=int, default=20261023)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    t0 = time.time()
    rng = np.random.default_rng(args.seed)
    rgb = hg.load_image(args.input, args.work_res)
    H, W = rgb.shape[:2]
    L = float(max(H, W))
    fg = ~np.all(rgb > 0.945, axis=2)
    scale = (H * W) / (1280.0 * 1280.0)
    count = args.count or int(round(2200 * scale))
    base = json.load(open(args.strokes, encoding="utf-8"))
    for s in base:
        s["points"] = np.asarray(s["points"], dtype=np.float64)
        s["widths"] = np.asarray(s["widths"], dtype=np.float64)
        s["color"] = np.asarray(s["color"], dtype=np.float64)

    # Three scales of blur: broad masses -> atmosphere.
    layers = []
    for sigma, width_px, n in ((2.0, 8.0, 0.40), (5.0, 14.0, 0.35), (10.0, 24.0, 0.25)):
        n = max(1, int(round(count * n)))
        blurred = hg.gaussian_blur(rgb.astype(np.float32), sigma).astype(np.float64)
        jxx, jyy, jxy = hg.orientation_field(blurred, sigma_t=2.0, sigma_g=1.2)
        theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy) + math.pi / 2.0
        ys = np.arange(6, H - 6, 14)
        xs = np.arange(6, W - 6, 14)
        seeds = [(int(x), int(y)) for y in ys for x in xs if fg[y, x]]
        rng.shuffle(seeds)
        added = 0
        for x0, y0 in seeds:
            if added >= n:
                break
            th = float(theta[y0, x0] + rng.normal(0, 0.18))
            ln = float(rng.uniform(24.0, 60.0))
            ca, sa = math.cos(th), math.sin(th)
            p0 = np.array([x0 - ca * ln / 2, y0 - sa * ln / 2])
            p1 = np.array([x0 + rng.normal(0, 1.2), y0 + rng.normal(0, 1.2)])
            p2 = np.array([x0 + ca * ln / 2, y0 + sa * ln / 2])
            pts = np.stack([p0, p1, p2], axis=0)
            xi = np.clip(pts[:, 0].astype(int), 0, W - 1)
            yi = np.clip(pts[:, 1].astype(int), 0, H - 1)
            if fg[yi, xi].mean() < 0.6:
                continue
            col = np.median(blurred[yi, xi], axis=0)
            widths = np.array([0.85, 1.0, 0.85]) * float(width_px) / L
            layers.append({
                "points": (pts / L).tolist(),
                "widths": widths.tolist(),
                "color": np.clip(col, 0, 1).tolist(),
                "alpha": 0.42,
                "layer": int(args.under_layer if sigma < 4 else args.under_layer + 1),
                "kind": "soft_atmosphere",
                "closed": False,
            })
            added += 1
    print(f"[soft] added={len(layers)}", flush=True)
    strokes = base + layers
    strokes.sort(key=lambda s: int(s.get("layer", 0)))
    ren = hgr._render_strokes(strokes, H, W, ss=1)
    psnr = hgr._psnr(rgb, ren)
    ssim = hgr._ssim(rgb, ren)
    from PIL import Image
    png = os.path.join(args.outdir, f"soft_R{args.work_res}.png")
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
    js = os.path.join(args.outdir, "soft_strokes.json")
    json.dump(payload, open(js, "w", encoding="utf-8"), ensure_ascii=False)
    report = {"input": os.path.abspath(args.input), "base_strokes": len(base),
              "added": len(layers), "total_strokes": len(strokes),
              "psnr": round(psnr, 4), "ssim": round(ssim, 5),
              "png": os.path.abspath(png), "strokes": os.path.abspath(js),
              "seconds": round(time.time() - t0, 1)}
    json.dump(report, open(os.path.join(args.outdir, "soft_report.json"), "w",
                           encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
