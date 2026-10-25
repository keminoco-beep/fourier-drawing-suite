#!/usr/bin/env python
"""Human-painter workflow reorderer.

Distilled from public painting tutorials:
  * Krita: line art on a layer above flat colour; colour below keeps line art clean.
  * Clip Studio anime skin: BASE -> SHADOW -> BLUSH -> HIGHLIGHT -> reflected light,
    all clipped under the line-art layer; hard pen for base, watercolour/soft brush
    for shadow/blush, soft airbrush for highlights/reflected light.
  * Classical oil: toned ground -> broad block-in -> shadow/midtone -> details/highlights.

Applied to stroke output:
  * add a low-alpha broad "toned ground" under everything;
  * regroup existing strokes into human-like layers;
  * keep line/detail strokes on the top layer (line art must stay crisp);
  * micro-fix strokes move below line art instead of painting over it.
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

# human layer order (low = first / underneath)
LAYER = {
    "ground": -20,
    "soft_atmosphere": -10,
    "base": 0,
    "gradient_swath": 0,
    "tex": 2,
    "fix": 4,
    "oil_texture": 5,
    "oil_fine_hatch": 6,
    "oil_dark_detail": 7,
    "blush": 10,
    "highlight": 15,
    "soft_glaze": 18,
    "detail": 20,
    "outline": 20,
    "faceink": 20,
    "boundary": 20,
    "edge_detail": 20,
    "line_detail": 20,
    "oil_edge": 20,
    "hairflow": 22,
    "oil_streamline": 22,
    "precision_repair": 30,
    "ssim_repair": 30,
    "structure_detail": 32,
}


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strokes", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--work-res", type=int, default=1408)
    ap.add_argument("--ground", type=int, default=300,
                    help="number of broad toned-ground strokes added underneath")
    ap.add_argument("--ground-alpha", type=float, default=0.45)
    ap.add_argument("--highlight", type=int, default=0,
                    help="optional soft highlights on top of colour, under line art")
    ap.add_argument("--seed", type=int, default=20261029)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    t0 = time.time()
    rgb = hg.load_image(args.input, args.work_res)
    H, W = rgb.shape[:2]
    L = float(max(H, W))
    fg = ~np.all(rgb > 0.945, axis=2)
    strokes = load_strokes(args.strokes)

    # 1) toned ground: broad low-alpha colour masses under everything
    ground = []
    if args.ground > 0:
        rng = np.random.default_rng(args.seed)
        blur = hg.gaussian_blur(rgb.astype(np.float32), 8.0).astype(np.float64)
        jxx, jyy, jxy = hg.orientation_field(blur, sigma_t=2.0, sigma_g=1.2)
        theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy) + math.pi / 2.0
        ys = np.arange(8, H - 8, 16)
        xs = np.arange(8, W - 8, 16)
        seeds = [(int(x), int(y)) for y in ys for x in xs if fg[y, x]]
        rng.shuffle(seeds)
        for x0, y0 in seeds:
            if len(ground) >= args.ground:
                break
            th = float(theta[y0, x0] + rng.normal(0, 0.25))
            ln = float(rng.uniform(45.0, 110.0))
            ca, sa = math.cos(th), math.sin(th)
            p0 = [x0 - ca * ln / 2, y0 - sa * ln / 2]
            p1 = [x0 + rng.normal(0, 2.0), y0 + rng.normal(0, 2.0)]
            p2 = [x0 + ca * ln / 2, y0 + sa * ln / 2]
            pts = np.array([p0, p1, p2])
            xi = np.clip(pts[:, 0].astype(int), 0, W - 1)
            yi = np.clip(pts[:, 1].astype(int), 0, H - 1)
            col = np.median(blur[yi, xi], axis=0)
            ground.append({
                "points": pts / L,
                "widths": np.full(3, float(rng.uniform(22.0, 46.0)) / L),
                "color": np.clip(col, 0, 1).tolist(),
                "alpha": float(args.ground_alpha),
                "layer": LAYER["ground"],
                "kind": "ground",
                "closed": False,
            })

    # 2) highlights (optional): short soft light strokes on colour, under line art
    highlights = []
    if args.highlight > 0:
        rng = np.random.default_rng(args.seed + 1)
        lum = rgb @ np.array([0.2126, 0.7152, 0.0722])
        thr = float(np.percentile(lum[fg], 92)) if fg.any() else 0.9
        seeds = np.argwhere(fg & (lum > thr))
        rng.shuffle(seeds)
        for y0, x0 in seeds:
            if len(highlights) >= args.highlight:
                break
            th = rng.uniform(0, math.pi)
            ln = float(rng.uniform(8, 22))
            ca, sa = math.cos(th), math.sin(th)
            pts = np.array([[x0 - ca * ln / 2, y0 - sa * ln / 2],
                            [x0 + ca * ln / 2, y0 + sa * ln / 2]])
            col = np.clip(rgb[y0, x0] * 0.55 + 0.45, 0, 1)
            highlights.append({
                "points": pts / L,
                "widths": np.full(2, float(rng.uniform(4.0, 9.0)) / L),
                "color": col.tolist(),
                "alpha": 0.28,
                "layer": LAYER["highlight"],
                "kind": "highlight",
                "closed": False,
            })

    # 3) regroup existing strokes: line art remains the top layer
    for s in strokes:
        kind = str(s.get("kind", ""))
        s["layer"] = int(LAYER.get(kind, s.get("layer", 0)))
    out = ground + strokes + highlights
    out.sort(key=lambda s: int(s.get("layer", 0)))
    ren = hgr._render_strokes(out, H, W, ss=1)
    psnr = hgr._psnr(rgb, ren)
    ssim = hgr._ssim(rgb, ren)
    png = os.path.join(args.outdir, f"human_workflow_R{args.work_res}.png")
    Image.fromarray((np.clip(ren, 0, 1) * 255).astype(np.uint8)).save(png)
    js = os.path.join(args.outdir, "human_workflow_strokes.json")
    save_strokes(js, out)
    report = {
        "input": os.path.abspath(args.input), "work_res": args.work_res,
        "strokes_in": len(strokes), "ground": len(ground), "highlight": len(highlights),
        "strokes_out": len(out), "psnr": round(psnr, 4), "ssim": round(ssim, 5),
        "layers": {k: sum(1 for s in out if int(s.get("layer", 0)) == v)
                   for k, v in sorted(LAYER.items(), key=lambda kv: kv[1])},
        "png": os.path.abspath(png), "strokes": os.path.abspath(js),
        "seconds": round(time.time() - t0, 1),
    }
    json.dump(report, open(os.path.join(args.outdir, "human_workflow_report.json"), "w",
                           encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
