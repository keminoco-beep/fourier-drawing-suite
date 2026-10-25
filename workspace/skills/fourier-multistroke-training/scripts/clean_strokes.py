#!/usr/bin/env python
"""Generic stroke cleanup: resample, smooth, and remove jittery curls.

The Fourier fit preserves every pixel-level wiggle.  Orientation streamlines and
skeleton edges can curl at leaf/flower tips; those turns are visually read as
"jitter".  This pass smooths every stroke and drops the highest-curvature added
strokes.  It is content-agnostic (curve thresholds, not object masks).
"""
from __future__ import annotations

import argparse
import json
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


def resample_smooth(points, max_pts=16, smooth_iters=3):
    p = np.asarray(points, dtype=np.float64)
    if len(p) < 3:
        return p
    seg = np.linalg.norm(np.diff(p, axis=0), axis=1)
    total = float(seg.sum())
    if total <= 1e-12:
        return p
    t = np.concatenate([[0.0], np.cumsum(seg)]) / total
    n = min(int(max_pts), max(4, len(p)))
    ti = np.linspace(0.0, 1.0, n)
    q = np.stack([np.interp(ti, t, p[:, 0]), np.interp(ti, t, p[:, 1])], axis=1)
    for _ in range(max(1, int(smooth_iters))):
        q[1:-1] = (q[:-2] + 2.0 * q[1:-1] + q[2:]) / 4.0
    return q


def curvature(points, scale=1.0):
    p = np.asarray(points, dtype=np.float64) * float(scale)
    if len(p) < 3:
        return 0.0, 0.0
    v = np.diff(p, axis=0)
    ang = np.arctan2(v[:, 1], v[:, 0])
    da = (np.diff(ang) + np.pi) % (2.0 * np.pi) - np.pi
    return float(np.abs(da).sum()), float(np.abs(da).max()) if len(da) else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strokes", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--work-res", type=int, default=1408)
    ap.add_argument("--max-pts", type=int, default=16)
    ap.add_argument("--smooth-iters", type=int, default=3)
    ap.add_argument("--max-turn", type=float, default=2.2, help="total absolute turn (radians)")
    ap.add_argument("--max-step-turn", type=float, default=0.75, help="single-step turn (radians)")
    ap.add_argument("--protect-kinds", default="base,tex,fix,fill",
                    help="comma-separated kinds never dropped for curvature")
    ap.add_argument("--render", type=int, default=1)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    t0 = time.time()
    rgb = hg.load_image(args.input, args.work_res)
    H, W = rgb.shape[:2]
    data = json.load(open(args.strokes, encoding="utf-8"))
    protect = {k.strip() for k in args.protect_kinds.split(",") if k.strip()}
    out = []
    dropped = 0
    resampled = 0
    for s in data:
        p = np.asarray(s["points"], dtype=np.float64)
        q = resample_smooth(p, args.max_pts, args.smooth_iters)
        kind = str(s.get("kind", ""))
        if len(q) >= 3 and kind not in protect:
            turn, mx = curvature(q, scale=max(H, W))
            if turn > args.max_turn or mx > args.max_step_turn:
                dropped += 1
                continue
        d = dict(s)
        d["points"] = q
        d["widths"] = np.full(len(q), float(np.mean(s["widths"])))
        d["color"] = np.asarray(d["color"], dtype=np.float64)
        out.append(d)
        resampled += 1
    ren = hgr._render_strokes(out, H, W, ss=1)
    psnr = hgr._psnr(rgb, ren)
    ssim = hgr._ssim(rgb, ren)
    from PIL import Image
    png = os.path.join(args.outdir, f"cleaned_R{args.work_res}.png")
    if args.render:
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
    js = os.path.join(args.outdir, "cleaned_strokes.json")
    json.dump(payload, open(js, "w", encoding="utf-8"), ensure_ascii=False)
    report = {"input": os.path.abspath(args.input), "strokes_in": len(data),
              "strokes_out": len(out), "resampled": resampled, "dropped_curls": dropped,
              "work_res": args.work_res, "psnr": round(psnr, 4), "ssim": round(ssim, 5),
              "png": os.path.abspath(png) if args.render else None,
              "strokes": os.path.abspath(js), "seconds": round(time.time() - t0, 1)}
    json.dump(report, open(os.path.join(args.outdir, "clean_report.json"), "w",
                           encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
