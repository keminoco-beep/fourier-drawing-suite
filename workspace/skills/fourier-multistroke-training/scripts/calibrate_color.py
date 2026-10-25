#!/usr/bin/env python
"""Generic colour calibration for per-stroke Fourier drawings.

The renderer composites translucent brush strokes, so the produced image is
usually slightly brighter/flatter/less saturated than the reference.  This pass
renders the current strokes, compares global/lobal colour statistics and
nudges each stroke colour back towards the source.  No image-specific rules.
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


def _stats(img: np.ndarray):
    flat = img.reshape(-1, 3)
    mean = flat.mean(axis=0)
    std = flat.std(axis=0)
    mx = img.max(axis=2); mn = img.min(axis=2)
    sat = float(((mx - mn) / np.maximum(mx, 1e-6)).mean())
    return mean, std, sat


def calibrate_once(strokes, rgb, out_hw, gain_limit=(0.75, 1.65), sat_limit=(0.85, 1.50),
                   local_k=0.0, sat_mul=1.0, contrast_mul=1.0):
    H, W = out_hw
    ren = hgr._render_strokes(strokes, H, W, ss=1)
    m_ref, s_ref, sat_ref = _stats(rgb)
    m_ren, s_ren, sat_ren = _stats(ren)
    gain = np.clip(s_ref / np.maximum(s_ren, 1e-6) * float(contrast_mul), gain_limit[0], gain_limit[1])
    off = m_ref - m_ren * gain
    sat_gain = float(np.clip(sat_ref / max(sat_ren, 1e-6) * float(sat_mul), sat_limit[0], sat_limit[1]))
    out = []
    for s in strokes:
        d = dict(s)
        c = np.asarray(s["color"], dtype=np.float64).copy()
        c = np.clip(c * gain + off, 0.0, 1.0)
        lum = float(c @ np.array([0.2126, 0.7152, 0.0722]))
        c = np.clip(lum + (c - lum) * sat_gain, 0.0, 1.0)
        if local_k > 0:
            pts = np.asarray(s["points"], dtype=np.float64)
            cen = pts.mean(axis=0)
            xi = int(np.clip(cen[0] * W, 0, W - 1)); yi = int(np.clip(cen[1] * H, 0, H - 1))
            delta = np.asarray(rgb[yi, xi], dtype=np.float64) - np.asarray(ren[yi, xi], dtype=np.float64)
            c = np.clip(c + float(local_k) * delta, 0.0, 1.0)
        d["color"] = c
        out.append(d)
    # Re-render after applying the new colours: metrics must describe the
    # calibrated strokes, not the pre-calibration render.
    ren2 = hgr._render_strokes(out, H, W, ss=1)
    m_new, s_new, sat_new = _stats(ren2)
    return out, ren2, (m_ref, s_ref, sat_ref), (m_new, s_new, sat_new)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strokes", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--work-res", type=int, default=1280)
    ap.add_argument("--iters", type=int, default=3)
    ap.add_argument("--local-k", type=float, default=0.0,
                    help="0 = global only; 0.1-0.4 blends local residual at stroke centroid")
    ap.add_argument("--sat-mul", type=float, default=1.0, help="extra saturation multiplier")
    ap.add_argument("--contrast-mul", type=float, default=1.0, help="extra contrast multiplier")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    t0 = time.time()
    rgb = hg.load_image(args.input, args.work_res)
    H, W = rgb.shape[:2]
    data = json.load(open(args.strokes, encoding="utf-8"))
    strokes = []
    for s in data:
        d = dict(s)
        d["points"] = np.asarray(d["points"], dtype=np.float64)
        d["widths"] = np.asarray(d["widths"], dtype=np.float64)
        d["color"] = np.asarray(d["color"], dtype=np.float64)
        strokes.append(d)
    best = None
    for it in range(max(1, args.iters)):
        calibrated, ren, st_ref, st_ren = calibrate_once(
            strokes, rgb, (H, W), local_k=args.local_k,
            sat_mul=args.sat_mul, contrast_mul=args.contrast_mul)
        psnr = hgr._psnr(rgb, ren)
        ssim = hgr._ssim(rgb, ren)
        rec = {"iter": it + 1, "psnr": round(psnr, 4), "ssim": round(ssim, 5),
               "mean_ref": np.round(st_ref[0], 4).tolist(), "mean_ren": np.round(st_ren[0], 4).tolist(),
               "std_ref": np.round(st_ref[1], 4).tolist(), "std_ren": np.round(st_ren[1], 4).tolist(),
               "sat_ref": round(st_ref[2], 4), "sat_ren": round(st_ren[2], 4)}
        print("[calib]", json.dumps(rec, ensure_ascii=False), flush=True)
        if best is None or psnr > best[0]:
            best = (psnr, ssim, calibrated, ren, rec)
        strokes = calibrated
    psnr, ssim, strokes, ren, rec = best
    from PIL import Image
    png = os.path.join(args.outdir, f"calibrated_R{args.work_res}.png")
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
    out_json = os.path.join(args.outdir, "calibrated_strokes.json")
    json.dump(payload, open(out_json, "w", encoding="utf-8"), ensure_ascii=False)
    report = {"input": os.path.abspath(args.input), "base_strokes": len(data),
              "work_res": args.work_res, "iters": args.iters, "local_k": args.local_k,
              "sat_mul": args.sat_mul, "contrast_mul": args.contrast_mul,
              "psnr": round(psnr, 4), "ssim": round(ssim, 5),
              "best": rec, "png": os.path.abspath(png), "strokes": os.path.abspath(out_json),
              "seconds": round(time.time() - t0, 1)}
    json.dump(report, open(os.path.join(args.outdir, "color_report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
