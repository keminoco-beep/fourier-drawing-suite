#!/usr/bin/env python
"""Sparse-precision stroke budgeter.

Goals:
  * fewer strokes (drop low-information base/texture strokes, micro-fix strokes)
  * keep the large shapes; only repair high-gradient residuals
  * flatten large uniform regions (alpha=1 base, no micro-fix hatching)
Content-agnostic; line/detail strokes are protected.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
from scipy import ndimage as ndi

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

LINE_KINDS = {"detail", "outline", "faceink", "boundary", "hairflow"}


def flat_mask(rgb, fg):
    H, W = fg.shape
    gray = rgb @ np.array([0.2126, 0.7152, 0.0722])
    gs = hg.gaussian_blur(gray.astype(np.float32), 0.8).astype(np.float64)
    gy, gx = np.gradient(gs)
    grad = np.hypot(gx, gy)
    sm = hg.gaussian_blur(rgb.astype(np.float32), 3.0).astype(np.float64)
    lab, _, _ = hg.quantize(sm, 12)
    flat = np.zeros((H, W), dtype=bool)
    for lid in range(12):
        m = (lab == lid) & fg
        cc, n = ndi.label(m, structure=np.ones((3, 3), dtype=np.int32))
        sizes = np.bincount(cc.ravel())
        for cid in range(1, n + 1):
            a = int(sizes[cid])
            if a < 650:
                continue
            cm = cc == cid
            fl = float((grad[cm] < 0.008).mean())
            cstd = float(np.mean(np.std(rgb[cm], axis=0)))
            if fl > 0.90 and cstd < 0.022:
                flat |= cm
    return flat, grad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strokes", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--work-res", type=int, default=1408)
    ap.add_argument("--target-keep", type=float, default=0.75,
                    help="fraction of non-line strokes to keep")
    ap.add_argument("--repair", type=int, default=500)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    t0 = time.time()
    rgb = hg.load_image(args.input, args.work_res)
    H, W = rgb.shape[:2]
    L = float(max(H, W))
    fg = ~np.all(rgb > 0.945, axis=2)
    flat, grad = flat_mask(rgb, fg)
    data = json.load(open(args.strokes, encoding="utf-8"))
    strokes = []
    for s in data:
        d = dict(s)
        d["points"] = np.asarray(d["points"], dtype=np.float64)
        d["widths"] = np.asarray(d["widths"], dtype=np.float64)
        d["color"] = np.asarray(d["color"], dtype=np.float64)
        strokes.append(d)
    n0 = len(strokes)

    # flat-region cleanup: alpha=1 for base, drop micro-fix strokes
    kept = []
    dropped_flat = 0
    for s in strokes:
        p = np.asarray(s["points"])
        xi = np.clip((p[:, 0] * W).astype(int), 0, W - 1)
        yi = np.clip((p[:, 1] * H).astype(int), 0, H - 1)
        frac = float(flat[yi, xi].mean())
        kind = str(s.get("kind", ""))
        if frac > 0.45:
            if kind in ("fix", "detail"):
                dropped_flat += 1
                continue
            if kind in ("base", "tex"):
                s["alpha"] = 1.0
                s["widths"] = s["widths"] * 1.10
        kept.append(s)

    # budgeted drop: weakest non-line strokes first
    line = [s for s in kept if str(s.get("kind", "")) in LINE_KINDS]
    cand = [s for s in kept if str(s.get("kind", "")) not in LINE_KINDS]
    score = [float(np.linalg.norm(np.diff(np.asarray(s["points"]), axis=0), axis=1).sum() *
                   np.mean(s["widths"])) for s in cand]
    order = np.argsort(score)  # weakest first
    keep_n = max(1, int(round(len(cand) * float(args.target_keep))))
    drop_idx = set(order[: max(0, len(cand) - keep_n)].tolist())
    out = line + [s for i, s in enumerate(cand) if i not in drop_idx]
    dropped_budget = len(cand) - keep_n

    # residual repair only in high-gradient regions
    ren = hgr._render_strokes(out, H, W, ss=1)
    if args.repair > 0:
        err = np.abs(ren - rgb).mean(axis=2)
        high = grad > float(np.percentile(grad[fg], 70)) if fg.any() else np.zeros_like(fg)
        mask = fg & high & (err > float(np.percentile(err[fg], 96))) if fg.any() else np.zeros_like(fg)
        cc, n = ndi.label(mask, structure=np.ones((3, 3), dtype=np.int32))
        sizes = np.bincount(cc.ravel())
        jxx, jyy, jxy = hg.orientation_field(rgb)
        rng = np.random.default_rng(20261025)
        added = []
        for cid in range(1, n + 1):
            if sizes[cid] < 20:
                continue
            cm = cc == cid
            ys, xs = np.nonzero(cm)
            x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
            ang = hg.region_orientation(cm, jxx, jyy, jxy)
            color = np.median(rgb[ys, xs], axis=0)
            got = hg.scanline_hatch(cm, (x0, y0, x1, y1), ang, 2.4, 1.8, color,
                                    (rgb, 0.01, 1.0), rng, max_strokes=2)
            for st in got:
                st = dict(st)
                st["points"] = st["points"] / L
                st["widths"] = st["widths"] / L
                st["kind"] = "precision_repair"
                st["layer"] = 7
                st["alpha"] = 0.95
                added.append(st)
            if len(added) >= args.repair:
                break
        out += added
    out.sort(key=lambda s: int(s.get("layer", 0)))
    ren = hgr._render_strokes(out, H, W, ss=1)
    psnr = hgr._psnr(rgb, ren)
    ssim = hgr._ssim(rgb, ren)
    from PIL import Image
    png = os.path.join(args.outdir, f"sparse_R{args.work_res}.png")
    Image.fromarray((np.clip(ren, 0, 1) * 255).astype(np.uint8)).save(png)
    payload = []
    for s in out:
        d = dict(s)
        d["points"] = np.round(np.asarray(d["points"]), 6).tolist()
        d["widths"] = np.round(np.asarray(d["widths"]), 7).tolist()
        d["color"] = np.round(np.asarray(d["color"]), 5).tolist()
        d["alpha"] = float(d.get("alpha", 1.0))
        d["layer"] = int(d.get("layer", 0))
        d["kind"] = str(d.get("kind", ""))
        d["closed"] = False
        payload.append(d)
    js = os.path.join(args.outdir, "sparse_strokes.json")
    json.dump(payload, open(js, "w", encoding="utf-8"), ensure_ascii=False)
    report = {"input": os.path.abspath(args.input), "strokes_in": n0,
              "strokes_out": len(out), "target_keep": args.target_keep,
              "dropped_budget": dropped_budget, "dropped_flat": dropped_flat,
              "repair": len(out) - len(line) - keep_n if args.repair > 0 else 0,
              "psnr": round(psnr, 4), "ssim": round(ssim, 5),
              "png": os.path.abspath(png), "strokes": os.path.abspath(js),
              "seconds": round(time.time() - t0, 1)}
    json.dump(report, open(os.path.join(args.outdir, "sparse_report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
