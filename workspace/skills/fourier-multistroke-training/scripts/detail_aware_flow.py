#!/usr/bin/env python
"""Detail-aware flow tiling + dark-line restoration.

Solves the small-detail loss (village roofs, windows, thin linework) of the
coarse flow-tiled base:

  * flat regions  -> coarse flow tiles (fast, smooth);
  * high-gradient regions -> fine flow tiles (6/5 px) so the base itself does
    not low-pass away small structures;
  * an explicit dark-line layer restores the darkest thin lines from the source
    high-frequency residual (the line work human painters do last).

Generic: thresholds come from the source distribution, not from a specific
image or object region.
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


# Repair/translucent layers from an earlier pipeline must never be merged on
# top of freshly painted detail: they are opaque micro-fixes and wipe texture.
DROP_DETAIL_KINDS = {"fix", "soft_atmosphere", "soft_glaze"}


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


def flow_pass(rgb, fg, theta, mask, spacing, width_factor, length_factor, layer, kind, rng):
    H, W = fg.shape
    L = float(max(H, W))
    out = []
    off = spacing * 0.5
    for y in np.arange(off, H, spacing):
        for x in np.arange(off, W, spacing):
            ix, iy = int(x), int(y)
            if ix < 0 or iy < 0 or ix >= W or iy >= H or not fg[iy, ix] or not mask[iy, ix]:
                continue
            th = float(theta[iy, ix] + rng.normal(0.0, 0.10))
            ln = float(rng.uniform(length_factor * 0.8, length_factor * 1.2) * spacing)
            wpx = float(rng.uniform(width_factor * 0.85, width_factor * 1.15) * spacing)
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
            out.append({
                "points": pts / L, "widths": np.full(3, wpx / L),
                "color": np.clip(col, 0, 1).tolist(), "alpha": 1.0,
                "layer": layer, "kind": kind, "closed": False,
            })
    return out


def dark_line_pass(rgb, fg, count, width_px, layer, rng, threshold=0.035,
                   color_pct=40.0, alpha=0.75):
    from fbs_core.decompose import skeletonize, skeleton_graph
    H, W = fg.shape
    L = float(max(H, W))
    gray = rgb @ np.array([0.2126, 0.7152, 0.0722])
    bg = hg.gaussian_blur(gray.astype(np.float32), 4.0).astype(np.float64)
    dark = bg - gray
    mask = fg & (dark > float(threshold))
    sk = skeletonize(mask, 6)
    branches = skeleton_graph(sk)
    branches.sort(key=len, reverse=True)
    out = []
    for path in branches:
        if len(out) >= count:
            break
        if len(path) < 4:
            continue
        pts = np.array([(x, y) for (y, x) in path], dtype=np.float64) + 0.5
        idx = np.linspace(0, len(pts) - 1, min(40, len(pts))).astype(int)
        pp = pts[idx]
        cols = rgb[np.clip(pp[:, 1].astype(int), 0, H - 1),
                   np.clip(pp[:, 0].astype(int), 0, W - 1)]
        col = np.clip(np.percentile(cols, float(color_pct), axis=0) * 0.88, 0, 1)
        out.append({
            "points": pp / L, "widths": np.full(len(pp), width_px / L),
            "color": col.tolist(), "alpha": float(alpha),
            "layer": layer, "kind": "dark_line_detail", "closed": False,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--detail", default=None)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--work-res", type=int, default=1280)
    ap.add_argument("--coarse-spacings", default="16,12")
    ap.add_argument("--fine-spacings", default="6,5")
    ap.add_argument("--detail-grad-pct", type=float, default=80.0)
    ap.add_argument("--dark-lines", type=int, default=2500)
    ap.add_argument("--dark-line-width", type=float, default=2.0)
    ap.add_argument("--dark-line-threshold", type=float, default=0.035)
    ap.add_argument("--dark-line-alpha", type=float, default=0.75)
    ap.add_argument("--edge-lines", type=int, default=0,
                    help="source-resolution edge-line detail (0 = off)")
    ap.add_argument("--edge-pct", type=float, default=90.0)
    ap.add_argument("--edge-width", type=float, default=1.4)
    ap.add_argument("--edge-alpha", type=float, default=0.6)
    ap.add_argument("--seed", type=int, default=20261102)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    t0 = time.time()
    rgb = hg.load_image(args.input, args.work_res)
    H, W = rgb.shape[:2]
    fg = ~np.all(rgb > 0.945, axis=2)
    jxx, jyy, jxy = hg.orientation_field(rgb)
    theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy) + math.pi / 2.0
    gray = rgb @ np.array([0.2126, 0.7152, 0.0722])
    gs = hg.gaussian_blur(gray.astype(np.float32), 0.8).astype(np.float64)
    gy, gx = np.gradient(gs)
    grad = np.hypot(gx, gy)
    hi = fg & (grad > float(np.percentile(grad[fg], args.detail_grad_pct)))
    rng = np.random.default_rng(args.seed)
    flow = []
    for sp in [float(x) for x in str(args.coarse_spacings).split(",") if x.strip()]:
        flow += flow_pass(rgb, fg, theta, ~hi, sp, 1.35, 1.9, 0, "flow_base", rng)
        flow += flow_pass(rgb, fg, theta, hi, sp, 0.9, 1.5, 1, "flow_base_detail", rng)
    for sp in [float(x) for x in str(args.fine_spacings).split(",") if x.strip()]:
        flow += flow_pass(rgb, fg, theta, hi, sp, 0.9, 1.45, 1, "flow_fine", rng)
    dark = (dark_line_pass(rgb, fg, args.dark_lines, args.dark_line_width, 7, rng,
                           threshold=args.dark_line_threshold,
                           alpha=args.dark_line_alpha)
            if args.dark_lines else [])
    edge = []
    if args.edge_lines > 0:
        hp = hg.HParams(work_res=args.work_res, edge_percentile=args.edge_pct,
                        max_edge_strokes=args.edge_lines, edge_width=args.edge_width,
                        edge_min_len=4.0)
        edge = hg.edge_line_strokes(rgb, fg, hp, rng)
        for st in edge:
            st["points"] = np.asarray(st["points"], dtype=np.float64) / (max(H, W))
            st["widths"] = np.asarray(st["widths"], dtype=np.float64) / (max(H, W))
            st["layer"] = 6
            st["kind"] = "edge_detail"
            st["alpha"] = float(args.edge_alpha)
    detail = (load_strokes(args.detail, skip={"base"} | DROP_DETAIL_KINDS)
              if args.detail else [])
    # If a caller explicitly wants repair strokes, keep them under detail.
    for s in detail:
        if str(s.get("kind", "")) == "fix":
            s["layer"] = 1
    out = []
    for s in flow + dark + edge + detail:
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
    png = os.path.join(args.outdir, f"detail_aware_R{args.work_res}.png")
    Image.fromarray((np.clip(ren, 0, 1) * 255).astype(np.uint8)).save(png)
    js = os.path.join(args.outdir, "detail_aware_strokes.json")
    save_strokes(js, out)
    report = {
        "input": os.path.abspath(args.input), "work_res": args.work_res,
        "flow_base": len(flow), "dark_lines": len(dark), "edge_lines": len(edge),
        "detail": len(detail),
        "strokes_out": len(out), "white_px_in_fg": white_in_fg,
        "psnr": round(psnr, 4), "ssim": round(ssim, 5),
        "png": os.path.abspath(png), "strokes": os.path.abspath(js),
        "seconds": round(time.time() - t0, 1),
    }
    json.dump(report, open(os.path.join(args.outdir, "detail_aware_report.json"), "w",
                           encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
