#!/usr/bin/env python
"""Opaque underpainting pass.

Human workflow rule: do not build the image with a long sequence of translucent
smears.  The base colour should be painted opaque and uniform first; later
layers cover it.  This pass removes translucent underlayers (ground /
soft_atmosphere / soft_glaze) and inserts one opaque colour fill underneath.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

import numpy as np
from scipy import ndimage as ndi
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

TRANSLUCENT_UNDER = {"ground", "soft_atmosphere", "soft_glaze"}


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
    ap.add_argument("--width-factor", type=float, default=0.32)
    ap.add_argument("--width-max", type=float, default=40.0)
    ap.add_argument("--spacing-mul", type=float, default=0.80)
    ap.add_argument("--min-area", type=int, default=120)
    ap.add_argument("--full-ground", action="store_true", default=True,
                    help="cover full canvas with an opaque toned ground when bg is not white")
    ap.add_argument("--no-full-ground", dest="full_ground", action="store_false")
    ap.add_argument("--color-mode", choices=("gradient", "median"), default="gradient",
                    help="opaque fill colour: per-stroke endpoint gradient or region median")
    ap.add_argument("--sample-sigma", type=float, default=1.2,
                    help="blur before sampling endpoint colours (0 = source pixels)")
    ap.add_argument("--k-colors", type=int, default=12,
                    help="quantised region count for the opaque fill")
    ap.add_argument("--seed", type=int, default=20261031)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    t0 = time.time()
    rgb = hg.load_image(args.input, args.work_res)
    H, W = rgb.shape[:2]
    L = float(max(H, W))
    fg = ~np.all(rgb > 0.945, axis=2)
    old = load_strokes(args.strokes)
    kept = [s for s in old if str(s.get("kind", "")) not in TRANSLUCENT_UNDER]
    removed = len(old) - len(kept)

    # Opaque toned ground across the whole canvas when the reference is a
    # full-bleed painting (no large white background).  This prevents the
    # "blotchy base with white holes" look while the region fill is being laid.
    ground = []
    bg_frac = float(np.all(rgb > 0.94, axis=2).mean())
    if args.full_ground and bg_frac < 0.20:
        blur = hg.gaussian_blur(rgb.astype(np.float32), 6.0).astype(np.float64)
        nb = 48
        for j in range(nb):
            y = (j + 0.5) / nb
            p = np.array([[0.002, y], [0.5, y], [0.998, y]])
            c0 = blur[min(H - 1, int(y * H)), int(0.01 * W)]
            c1 = blur[min(H - 1, int(y * H)), int(0.99 * W)]
            ground.append({
                "points": p,
                "widths": np.full(3, 1.15 / nb),
                "color": np.clip((c0 + c1) * 0.5, 0, 1).tolist(),
                "gradient": {"start": np.clip(c0, 0, 1).tolist(),
                             "end": np.clip(c1, 0, 1).tolist()},
                "alpha": 1.0,
                "layer": -10,
                "kind": "opaque_ground",
                "closed": False,
            })

    # opaque flat-colour fill: broad, overlapping, alpha=1, one base layer.
    # Sample per-stroke endpoint colours from the source instead of one median
    # per quantised region; a median makes the whole underpainting grey/flat.
    sample = (hg.gaussian_blur(rgb.astype(np.float32), float(args.sample_sigma)).astype(np.float64)
              if args.sample_sigma > 0 else rgb.astype(np.float64))
    sm = hg.gaussian_blur(rgb.astype(np.float32), 3.0).astype(np.float64)
    lab, _, _ = hg.quantize(sm, int(args.k_colors))
    jxx, jyy, jxy = hg.orientation_field(rgb)
    rng = np.random.default_rng(args.seed)
    fill = []
    for lid in range(int(args.k_colors)):
        m = (lab == lid) & fg
        cc, n = ndi.label(m, structure=np.ones((3, 3), dtype=np.int32))
        sizes = np.bincount(cc.ravel())
        for cid in range(1, n + 1):
            a = int(sizes[cid])
            if a < args.min_area:
                continue
            cm = cc == cid
            ys, xs = np.nonzero(cm)
            x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
            col = np.median(rgb[ys, xs], axis=0)
            ang = hg.region_orientation(cm, jxx, jyy, jxy)
            width = float(np.clip(args.width_factor * math.sqrt(a), 10.0, args.width_max))
            spacing = width * float(args.spacing_mul)
            out = hg.scanline_hatch(cm, (x0, y0, x1, y1), ang, width, spacing, None,
                                    (rgb, 0.003, 1.0), rng, max_strokes=800)
            for st in out:
                st = dict(st)
                pts_px = np.asarray(st["points"], dtype=np.float64)
                if args.color_mode == "gradient" and len(pts_px) >= 2:
                    xi = np.clip(pts_px[:, 0].astype(int), 0, W - 1)
                    yi = np.clip(pts_px[:, 1].astype(int), 0, H - 1)
                    c0 = sample[yi[0], xi[0]]
                    c1 = sample[yi[-1], xi[-1]]
                    # keep gradients from leaking into a neighbouring white border
                    if np.any(c0 > 0.94) or np.any(c1 > 0.94):
                        c0 = c1 = np.median(sample[yi, xi], axis=0)
                    st["gradient"] = {
                        "start": np.clip(c0, 0, 1).tolist(),
                        "end": np.clip(c1, 0, 1).tolist(),
                    }
                    st["color"] = np.clip((c0 + c1) * 0.5, 0, 1).tolist()
                else:
                    st["color"] = np.clip(col, 0, 1).tolist()
                st["points"] = pts_px / L
                st["widths"] = np.full(len(pts_px), width / L)
                st["alpha"] = 1.0
                st["layer"] = -5
                st["kind"] = "opaque_base"
                st["closed"] = False
                fill.append(st)
    # make remaining base strokes opaque too
    for s in kept:
        if str(s.get("kind", "")) == "base":
            s["alpha"] = 1.0
    out = ground + kept + fill
    out.sort(key=lambda s: int(s.get("layer", 0)))
    ren = hgr._render_strokes(out, H, W, ss=1)
    psnr = hgr._psnr(rgb, ren)
    ssim = hgr._ssim(rgb, ren)

    def mean_sat(im):
        mx = im.max(axis=2); mn = im.min(axis=2)
        return float(((mx - mn) / np.maximum(mx, 1e-6)).mean())

    sat_ref = mean_sat(rgb)
    sat_ren = mean_sat(ren)
    png = os.path.join(args.outdir, f"opaque_base_R{args.work_res}.png")
    Image.fromarray((np.clip(ren, 0, 1) * 255).astype(np.uint8)).save(png)
    js = os.path.join(args.outdir, "opaque_base_strokes.json")
    save_strokes(js, out)
    report = {
        "input": os.path.abspath(args.input), "work_res": args.work_res,
        "strokes_in": len(old), "removed_translucent": removed,
        "ground": len(ground), "opaque_fill": len(fill), "strokes_out": len(out),
        "color_mode": args.color_mode, "sample_sigma": args.sample_sigma,
        "psnr": round(psnr, 4), "ssim": round(ssim, 5),
        "sat_ref": round(sat_ref, 4), "sat_render": round(sat_ren, 4),
        "png": os.path.abspath(png), "strokes": os.path.abspath(js),
        "seconds": round(time.time() - t0, 1),
    }
    json.dump(report, open(os.path.join(args.outdir, "opaque_base_report.json"), "w",
                           encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
