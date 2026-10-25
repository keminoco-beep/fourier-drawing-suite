#!/usr/bin/env python
"""Region-gradient swath generator.

Replaces dense scanline hatching inside large, smooth colour regions with a
small number of broad strokes whose colour ramps smoothly from one endpoint to
the other (arc-length interpolation).  Existing line/detail strokes are kept.

This is the generator behind the "fewer strokes + no hatch texture" direction:
  * region analysis -> orientation + width from area;
  * one gradient swath per few pixels of cross-section, with endpoint colours
    sampled from a blurred source (detail goes to the retained line pass);
  * non-region strokes are left untouched, so outside detail is preserved.
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

LINE_KINDS = {"detail", "oil_edge", "oil_dark_detail", "oil_streamline",
              "faceink", "outline", "boundary", "hairflow", "precision_repair"}


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
    ap.add_argument("--strokes", required=True, help="existing stroke JSON to replace in flat regions")
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--work-res", type=int, default=1408)
    ap.add_argument("--min-area", type=int, default=250)
    ap.add_argument("--max-color-std", type=float, default=0.11)
    ap.add_argument("--max-gradient", type=float, default=0.050)
    ap.add_argument("--width-factor", type=float, default=0.35,
                    help="swath width = clip(factor*sqrt(area), 8, 30) px")
    ap.add_argument("--spacing-mul", type=float, default=0.85)
    ap.add_argument("--blur-sigma", type=float, default=4.0)
    ap.add_argument("--layer", type=int, default=0)
    ap.add_argument("--outline", action="store_true", help="add traced region outlines")
    ap.add_argument("--target-count", type=int, default=0,
                    help="if >0, prune low-importance non-line strokes to this count")
    ap.add_argument("--repair", type=int, default=0,
                    help="add up to N residual high-error repair strokes")
    ap.add_argument("--repair-percentile", type=float, default=95.0)
    ap.add_argument("--additive", action="store_true",
                    help="keep all original strokes and add swaths on top (no replacement)")
    ap.add_argument("--max-hf", type=float, default=0.020,
                    help="skip regions whose mean high-frequency energy exceeds this (0=off)")
    ap.add_argument("--max-edge-frac", type=float, default=0.10,
                    help="skip regions whose strong-edge fraction exceeds this (0=off)")
    ap.add_argument("--protect-edges", action="store_true",
                    help="don't paint swaths over strong-edge pixels (masts/buildings)")
    ap.add_argument("--protect-px", type=int, default=2,
                    help="dilation radius around strong edges for swath protection")
    ap.add_argument("--edge-detail", type=int, default=0,
                    help="add up to N source-edge detail strokes on top (structures)")
    ap.add_argument("--edge-pct", type=float, default=88.0)
    ap.add_argument("--edge-straight", type=float, default=0.55)
    ap.add_argument("--line-detail", type=int, default=0,
                    help="restore up to N long dark/light line structures (masts/buildings)")
    ap.add_argument("--structure-detail", type=int, default=0,
                    help="trace up to N long coherent structure strokes (masts/buildings)")
    ap.add_argument("--struct-grad-pct", type=float, default=88.0)
    ap.add_argument("--struct-coh", type=float, default=0.50)
    ap.add_argument("--overlay-hf", type=float, default=0.012,
                    help="smooth regions below this HF energy get swaths on top")
    ap.add_argument("--overlay-edge", type=float, default=0.04,
                    help="regions above this edge fraction get swaths underneath")
    ap.add_argument("--under-layer", type=int, default=-1,
                    help="layer for swaths in structured regions")
    ap.add_argument("--skip-structured", action="store_true",
                    help="do not paint swaths in structured regions at all")
    ap.add_argument("--max-line-frac", type=float, default=0.0,
                    help="skip regions with more long dark/light line structure than this")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    t0 = time.time()
    rgb = hg.load_image(args.input, args.work_res)
    H, W = rgb.shape[:2]
    L = float(max(H, W))
    fg = ~np.all(rgb > 0.945, axis=2)
    blur = hg.gaussian_blur(rgb.astype(np.float32), float(args.blur_sigma)).astype(np.float64)
    gray = rgb @ np.array([0.2126, 0.7152, 0.0722])
    gs = hg.gaussian_blur(gray.astype(np.float32), 0.8).astype(np.float64)
    gy, gx = np.gradient(gs)
    grad = np.hypot(gx, gy)
    sm = hg.gaussian_blur(rgb.astype(np.float32), 2.0).astype(np.float64)
    hf = np.abs(rgb - blur).mean(axis=2)
    dark = (hg.gaussian_blur(gray.astype(np.float32), 8.0) - gray) > 0.015
    vline = ndi.binary_opening(dark, structure=np.ones((16, 1), dtype=bool))
    hline = ndi.binary_opening(dark, structure=np.ones((1, 16), dtype=bool))
    struct_lines = (vline | hline) & fg
    strong = grad > 0.05
    structure_guard = strong | struct_lines
    protect = (ndi.binary_dilation(structure_guard, iterations=max(1, int(args.protect_px)))
               if args.protect_edges else np.zeros_like(strong))
    lab, _, _ = hg.quantize(sm, 14)
    comps = []
    for lid in range(14):
        m = (lab == lid) & fg
        cc, n = ndi.label(m, structure=np.ones((3, 3), dtype=np.int32))
        sizes = np.bincount(cc.ravel())
        for cid in range(1, n + 1):
            a = int(sizes[cid])
            if a < args.min_area:
                continue
            cm = cc == cid
            cstd = float(np.mean(np.std(rgb[cm], axis=0)))
            gmean = float(grad[cm].mean())
            hf_mean = float(hf[cm].mean())
            edge_frac = float((grad[cm] > 0.04).mean())
            if cstd >= args.max_color_std or gmean >= args.max_gradient:
                continue
            if args.max_hf > 0 and hf_mean > args.max_hf:
                continue
            if args.max_edge_frac > 0 and edge_frac > args.max_edge_frac:
                continue
            if args.max_line_frac > 0 and float(struct_lines[cm].mean()) > args.max_line_frac:
                continue
            comps.append((cm, a, hf_mean, edge_frac))
    # strokes fully inside a flat region are replaced; line strokes are kept
    region_mask = np.zeros((H, W), dtype=bool)
    for cm, _, _, _ in comps:
        region_mask |= cm
    old = load_strokes(args.strokes)
    kept, removed = [], 0
    for s in old:
        p = s["points"]
        xi = np.clip((p[:, 0] * W).astype(int), 0, W - 1)
        yi = np.clip((p[:, 1] * H).astype(int), 0, H - 1)
        if (not args.additive and region_mask[yi, xi].mean() > 0.9
                and s.get("kind") not in LINE_KINDS):
            removed += 1
            continue
        kept.append(s)
    # swaths
    add = []
    rng = np.random.default_rng(20261026)
    jxx, jyy, jxy = hg.orientation_field(rgb)
    for cm, a, hf_mean, edge_frac in comps:
        # smooth sky/water: swath on top (covers hatch).  structured regions:
        # put the swath underneath retained base/detail strokes.
        structured = (hf_mean > args.overlay_hf or edge_frac > args.overlay_edge)
        if args.additive and structured:
            if args.skip_structured:
                continue
            swath_layer = int(args.under_layer)
        else:
            swath_layer = int(args.layer)
        paint = cm & ~protect
        if args.protect_edges and int(paint.sum()) < max(60, int(0.35 * a)):
            continue
        ys, xs = np.nonzero(paint)
        x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
        ang = hg.region_orientation(cm, jxx, jyy, jxy)
        width = float(np.clip(args.width_factor * math.sqrt(a), 8.0, 30.0))
        spacing = width * max(0.5, float(args.spacing_mul))
        out = hg.scanline_hatch(paint, (x0, y0, x1, y1), ang, width, spacing, None,
                                (rgb, 0.004, 1.0), rng, max_strokes=600)
        for st in out:
            pts = np.asarray(st["points"], dtype=np.float64)
            xi = np.clip(pts[:, 0].astype(int), 0, W - 1)
            yi = np.clip(pts[:, 1].astype(int), 0, H - 1)
            cols = blur[yi, xi]
            i0 = max(0, int(len(pts) * 0.18))
            i1 = min(len(pts) - 1, int(len(pts) * 0.82))
            c0, c1 = cols[i0], cols[i1]
            # endpoint near-white means the swath leaks into the background;
            # fall back to its mid colour rather than creating a white ramp.
            if np.any(c0 > 0.94) or np.any(c1 > 0.94):
                c0 = c1 = (c0 + c1) * 0.5
            add.append({
                "points": pts / L,
                "widths": np.full(len(pts), width / L),
                "color": np.clip((c0 + c1) * 0.5, 0, 1).tolist(),
                "gradient": {"start": np.clip(c0, 0, 1).tolist(),
                             "end": np.clip(c1, 0, 1).tolist()},
                "alpha": 1.0,
                "layer": int(swath_layer),
                "kind": "gradient_swath",
                "closed": False,
            })
    # source-edge detail: restore thin structures (masts/buildings/ridges) that
    # the broad swaths deliberately avoid painting over.
    if args.edge_detail > 0:
        from fbs_core.decompose import skeletonize, skeleton_graph
        edge_thr = float(np.percentile(grad[fg], float(args.edge_pct))) if fg.any() else 1.0
        em = fg & (grad > max(edge_thr, 0.015))
        sk = skeletonize(em, 6)
        branches = skeleton_graph(sk)
        # long, straight branches = masts / cranes / building edges; reject
        # short and wiggly cloud texture.
        branches.sort(key=len, reverse=True)
        edge_strokes = []
        for path in branches:
            if len(edge_strokes) >= args.edge_detail:
                break
            if len(path) < 12:
                continue
            p0, p1 = np.asarray(path[0], dtype=np.float64), np.asarray(path[-1], dtype=np.float64)
            straight = float(np.linalg.norm(p1 - p0) / max(len(path) - 1, 1))
            if straight < float(args.edge_straight):
                continue
            pts = np.asarray(path, dtype=np.float64) + 0.5
            idx = np.linspace(0, len(pts) - 1, min(50, len(pts))).astype(int)
            pp = pts[idx]
            cols = rgb[np.clip(pp[:, 1].astype(int), 0, H - 1),
                       np.clip(pp[:, 0].astype(int), 0, W - 1)]
            col = np.clip(np.median(cols, axis=0) * 0.88, 0, 1)
            edge_strokes.append({
                "points": pp / L,
                "widths": np.full(len(pp), 1.5 / L),
                "color": col.tolist(),
                "alpha": 0.95,
                "layer": 8,
                "kind": "edge_detail",
                "closed": False,
            })
        add.extend(edge_strokes)

    # restore long dark/light line structures (masts, cranes, building edges)
    if args.line_detail > 0:
        from fbs_core.decompose import skeletonize, skeleton_graph
        sk = skeletonize(struct_lines, 6)
        branches = skeleton_graph(sk)
        branches.sort(key=len, reverse=True)
        line_strokes = []
        for path in branches:
            if len(line_strokes) >= args.line_detail:
                break
            if len(path) < 8:
                continue
            pts = np.asarray(path, dtype=np.float64) + 0.5
            idx = np.linspace(0, len(pts) - 1, min(60, len(pts))).astype(int)
            pp = pts[idx]
            cols = rgb[np.clip(pp[:, 1].astype(int), 0, H - 1),
                       np.clip(pp[:, 0].astype(int), 0, W - 1)]
            col = np.clip(np.median(cols, axis=0) * 0.82, 0, 1)
            line_strokes.append({
                "points": pp / L,
                "widths": np.full(len(pp), 1.6 / L),
                "color": col.tolist(),
                "alpha": 0.95,
                "layer": 9,
                "kind": "line_detail",
                "closed": False,
            })
        add.extend(line_strokes)

    # long coherent structures (masts/cranes/building edges): trace the
    # structure-tensor tangent from strong-edge seeds; these strokes sit on the
    # top layer and are protected from the 20k budget pruning.
    if args.structure_detail > 0:
        jxx2, jyy2, jxy2 = hg.orientation_field(rgb, sigma_t=2.0, sigma_g=1.0)
        theta2 = 0.5 * np.arctan2(2 * jxy2, jxx2 - jyy2) + math.pi / 2.0
        coh = np.sqrt((jxx2 - jyy2) ** 2 + 4 * jxy2 ** 2) / np.maximum(jxx2 + jyy2, 1e-9)
        gthr = float(np.percentile(grad[fg], float(args.struct_grad_pct))) if fg.any() else 0.02
        seeds = np.argwhere(fg & (grad > gthr) & (coh > float(args.struct_coh)))
        rng2 = np.random.default_rng(20261028)
        rng2.shuffle(seeds)
        used = np.zeros((H, W), dtype=bool)
        struct_strokes = []
        for y0, x0 in seeds:
            if len(struct_strokes) >= args.structure_detail:
                break
            if used[y0, x0]:
                continue
            path = []
            for sgn in (1.0, -1.0):
                x, y = float(x0), float(y0)
                for _ in range(50):
                    ix, iy = int(round(x)), int(round(y))
                    if ix < 0 or iy < 0 or ix >= W or iy >= H:
                        break
                    if grad[iy, ix] < 0.004:
                        break
                    used[max(0, iy - 2):iy + 3, max(0, ix - 2):ix + 3] = True
                    if sgn > 0:
                        path.append((x, y))
                    else:
                        path.insert(0, (x, y))
                    th = float(theta2[iy, ix])
                    x += sgn * 2.0 * math.cos(th)
                    y += sgn * 2.0 * math.sin(th)
            if len(path) < 6:
                continue
            pts = np.asarray(path, dtype=np.float64)
            idx = np.linspace(0, len(pts) - 1, min(80, len(pts))).astype(int)
            pp = pts[idx]
            cols = rgb[np.clip(pp[:, 1].astype(int), 0, H - 1),
                       np.clip(pp[:, 0].astype(int), 0, W - 1)]
            col = np.clip(np.median(cols, axis=0) * 0.92, 0, 1)
            struct_strokes.append({
                "points": pp / L,
                "widths": np.full(len(pp), 1.7 / L),
                "color": col.tolist(),
                "alpha": 0.95,
                "layer": 8,
                "kind": "structure_detail",
                "closed": False,
            })
        add.extend(struct_strokes)

    # optional thin outlines (usually unnecessary: existing line strokes remain)
    if args.outline:
        from fbs_core.decompose import trace_boundary_loops
        for cm, _, _, _ in comps:
            for loop in trace_boundary_loops(cm):
                pts = np.asarray(loop, dtype=np.float64)
                if len(pts) < 8:
                    continue
                idx = np.linspace(0, len(pts) - 1, min(100, len(pts))).astype(int)
                pp = pts[idx] + 0.5
                col = np.median(rgb[np.clip(pp[:, 1].astype(int), 0, H - 1),
                                    np.clip(pp[:, 0].astype(int), 0, W - 1)], axis=0)
                add.append({
                    "points": pp / L,
                    "widths": np.full(len(pp), 1.1 / L),
                    "color": np.clip(col * 0.8, 0, 1).tolist(),
                    "alpha": 1.0, "layer": 5, "kind": "region_outline", "closed": False,
                })
    strokes = kept + add
    strokes.sort(key=lambda s: int(s.get("layer", 0)))

    def prune_to(items, target):
        if target <= 0 or len(items) <= target:
            return items
        protected = LINE_KINDS | {"gradient_swath", "region_outline", "ssim_repair",
                                  "edge_detail", "structure_detail", "line_detail"}
        cand = [s for s in items if s.get("kind") not in protected]
        cand.sort(key=lambda s: float(
            np.linalg.norm(np.diff(np.asarray(s["points"], float), axis=0), axis=1).sum() *
            np.mean(s["widths"])))
        drop = set(id(s) for s in cand[: len(items) - target])
        return [s for s in items if id(s) not in drop]

    strokes = prune_to(strokes, args.target_count)
    ren = hgr._render_strokes(strokes, H, W, ss=1)
    if args.repair > 0:
        err = np.abs(ren - rgb).mean(axis=2)
        err[~fg] = 0.0
        thr = float(np.percentile(err[fg], float(args.repair_percentile))) if fg.any() else 1.0
        mask = err > thr
        cc, n = ndi.label(mask, structure=np.ones((3, 3), dtype=np.int32))
        sizes = np.bincount(cc.ravel())
        jxx2, jyy2, jxy2 = hg.orientation_field(rgb)
        rng2 = np.random.default_rng(20261027)
        repaired = []
        for cid in range(1, n + 1):
            if len(repaired) >= args.repair:
                break
            if sizes[cid] < 6:
                continue
            cm = cc == cid
            ys, xs = np.nonzero(cm)
            x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
            color = np.median(rgb[ys, xs], axis=0)
            ang = hg.region_orientation(cm, jxx2, jyy2, jxy2)
            got = hg.scanline_hatch(cm, (x0, y0, x1, y1), ang, 2.2, 1.7, color,
                                    (rgb, 0.01, 1.0), rng2, max_strokes=2)
            for st in got:
                st = dict(st)
                st["points"] = st["points"] / L
                st["widths"] = st["widths"] / L
                st["alpha"] = 0.9
                st["layer"] = 7
                st["kind"] = "ssim_repair"
                repaired.append(st)
        strokes = strokes + repaired
        strokes.sort(key=lambda s: int(s.get("layer", 0)))
        strokes = prune_to(strokes, args.target_count)
        ren = hgr._render_strokes(strokes, H, W, ss=1)
    psnr = hgr._psnr(rgb, ren)
    ssim = hgr._ssim(rgb, ren)
    png = os.path.join(args.outdir, f"gradient_swath_R{args.work_res}.png")
    Image.fromarray((np.clip(ren, 0, 1) * 255).astype(np.uint8)).save(png)
    js = os.path.join(args.outdir, "gradient_swath_strokes.json")
    save_strokes(js, strokes)
    report = {
        "input": os.path.abspath(args.input), "work_res": args.work_res,
        "regions": len(comps), "region_px": int(region_mask.sum()),
        "strokes_in": len(old), "kept": len(kept), "removed": removed,
        "swaths": sum(1 for s in add if s["kind"] == "gradient_swath"),
        "outlines": sum(1 for s in add if s["kind"] == "region_outline"),
        "strokes_out": len(strokes),
        "psnr": round(psnr, 4), "ssim": round(ssim, 5),
        "png": os.path.abspath(png), "strokes": os.path.abspath(js),
        "seconds": round(time.time() - t0, 1),
    }
    json.dump(report, open(os.path.join(args.outdir, "gradient_swath_report.json"), "w",
                           encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
