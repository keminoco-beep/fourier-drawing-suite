#!/usr/bin/env python
"""Layer-dependent precision schedule.

Human painters block in colour loosely: broad, imprecise edges; later layers
cover the sloppy edges with progressively finer strokes.  This pass:

  * jitters/widens low-layer colour strokes (ground/base/tex/shadow),
  * keeps line/detail strokes untouched (fine layer),
  * optionally adds a precise contour layer on top to clean up boundaries.

It deliberately does NOT try to make the base accurate; fidelity comes from the
upper layers, matching the human coarse-to-fine order.
"""
from __future__ import annotations

import argparse
import json
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

COARSE = {"ground", "base", "gradient_swath", "tex", "fix", "oil_texture",
          "oil_fine_hatch", "oil_dark_detail"}
MEDIUM = {"soft_atmosphere", "soft_glaze"}
LINE = {"detail", "outline", "faceink", "boundary", "oil_edge", "edge_detail",
        "line_detail", "hairflow", "oil_streamline", "precision_repair",
        "ssim_repair", "structure_detail"}


def load_strokes(path):
    out = []
    for s in json.load(open(path, encoding="utf-8")):
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


def contour_strokes(rgb, fg, L, count, seed):
    """Precise upper-layer contours from source edges."""
    from fbs_core.decompose import skeletonize, skeleton_graph
    gray = rgb @ np.array([0.2126, 0.7152, 0.0722])
    gs = hg.gaussian_blur(gray.astype(np.float32), 0.8).astype(np.float64)
    gy, gx = np.gradient(gs)
    grad = np.hypot(gx, gy)
    thr = float(np.percentile(grad[fg], 90)) if fg.any() else 0.02
    em = fg & (grad > max(thr, 0.015))
    sk = skeletonize(em, 6)
    branches = skeleton_graph(sk)
    branches.sort(key=len, reverse=True)
    out = []
    for path in branches:
        if len(out) >= count:
            break
        if len(path) < 10:
            continue
        p0, p1 = np.asarray(path[0], float), np.asarray(path[-1], float)
        if np.linalg.norm(p1 - p0) / max(len(path) - 1, 1) < 0.55:
            continue
        pts = np.asarray(path, float) + 0.5
        idx = np.linspace(0, len(pts) - 1, min(60, len(pts))).astype(int)
        pp = pts[idx]
        cols = rgb[np.clip(pp[:, 1].astype(int), 0, rgb.shape[0] - 1),
                   np.clip(pp[:, 0].astype(int), 0, rgb.shape[1] - 1)]
        col = np.clip(np.median(cols, axis=0) * 0.82, 0, 1)
        out.append({
            "points": pp / L,
            "widths": np.full(len(pp), 1.35 / L),
            "color": col.tolist(),
            "alpha": 0.95,
            "layer": 26,
            "kind": "contour_detail",
            "closed": False,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strokes", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--work-res", type=int, default=1408)
    ap.add_argument("--base-jitter-px", type=float, default=7.0)
    ap.add_argument("--base-width-mul", type=float, default=1.25)
    ap.add_argument("--medium-jitter-px", type=float, default=3.0)
    ap.add_argument("--contour", type=int, default=300)
    ap.add_argument("--min-coarse-px", type=float, default=8.0,
                    help="only broad base strokes are jittered; thin base lines stay precise")
    ap.add_argument("--seed", type=int, default=20261030)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    t0 = time.time()
    rgb = hg.load_image(args.input, args.work_res)
    H, W = rgb.shape[:2]
    L = float(max(H, W))
    fg = ~np.all(rgb > 0.945, axis=2)
    strokes = load_strokes(args.strokes)
    rng = np.random.default_rng(args.seed)
    for s in strokes:
        kind = str(s.get("kind", ""))
        if kind in LINE:
            continue
        pts = s["points"]
        width_px = float(np.mean(s["widths"])) * L
        if kind in COARSE and width_px >= args.min_coarse_px:
            # rougher, wider, but only for broad colour-blocking strokes;
            # thin base lines would fragment the drawing if jittered.
            sig = min(args.base_jitter_px, 0.25 * width_px) / L
            pts = pts + rng.normal(0, sig, pts.shape)
            s["widths"] = s["widths"] * args.base_width_mul
            s["alpha"] = min(1.0, float(s.get("alpha", 1.0)) * 1.05)
        elif kind in MEDIUM and width_px >= 6.0:
            sig = min(args.medium_jitter_px, 0.20 * width_px) / L
            pts = pts + rng.normal(0, sig, pts.shape)
            s["widths"] = s["widths"] * 1.08
        s["points"] = np.clip(pts, 0.0, 1.0)
    contours = contour_strokes(rgb, fg, L, args.contour, args.seed + 1) if args.contour > 0 else []
    out = strokes + contours
    out.sort(key=lambda s: int(s.get("layer", 0)))
    ren = hgr._render_strokes(out, H, W, ss=1)
    psnr = hgr._psnr(rgb, ren)
    ssim = hgr._ssim(rgb, ren)
    png = os.path.join(args.outdir, f"coarse_to_fine_R{args.work_res}.png")
    Image.fromarray((np.clip(ren, 0, 1) * 255).astype(np.uint8)).save(png)
    js = os.path.join(args.outdir, "coarse_to_fine_strokes.json")
    save_strokes(js, out)
    report = {
        "input": os.path.abspath(args.input), "work_res": args.work_res,
        "strokes_in": len(strokes), "strokes_out": len(out),
        "contours": len(contours), "base_jitter_px": args.base_jitter_px,
        "psnr": round(psnr, 4), "ssim": round(ssim, 5),
        "png": os.path.abspath(png), "strokes": os.path.abspath(js),
        "seconds": round(time.time() - t0, 1),
    }
    json.dump(report, open(os.path.join(args.outdir, "coarse_to_fine_report.json"), "w",
                           encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
