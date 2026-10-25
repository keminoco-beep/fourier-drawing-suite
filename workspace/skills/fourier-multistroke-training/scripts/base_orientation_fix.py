#!/usr/bin/env python
"""Remove straight base-hatching artifacts that disagree with local source flow.

Root cause: the base layer is generated as scanline hatching at one orientation
per quantised colour region.  Across a curved/ swirling structure the region
orientation can be arbitrary, producing long straight strokes that cross the
flow (e.g. a diagonal line through a Van Gogh swirl).

Generic fix: for each base stroke, compare its direction with the local
structure-tensor tangent.  If they disagree, replace the stroke with several
short strokes aligned to the local tangent.  Detail/line layers are untouched.
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
    ap.add_argument("--work-res", type=int, default=1280)
    ap.add_argument("--min-length-px", type=float, default=45.0)
    ap.add_argument("--angle-tol-deg", type=float, default=18.0)
    ap.add_argument("--pieces", type=int, default=2)
    ap.add_argument("--piece-max-px", type=float, default=32.0)
    ap.add_argument("--force-long", action="store_true",
                    help="replace every base stroke longer than min-length, not only misaligned ones")
    ap.add_argument("--width-mul", type=float, default=1.0,
                    help="multiply width of replacement local strokes (coverage compensation)")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    t0 = time.time()
    rgb = hg.load_image(args.input, args.work_res)
    H, W = rgb.shape[:2]
    L = float(max(H, W))
    jxx, jyy, jxy = hg.orientation_field(rgb)
    theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy) + math.pi / 2.0
    strokes = load_strokes(args.strokes)
    out = []
    replaced = kept = 0
    for s in strokes:
        kind = str(s.get("kind", ""))
        pts = s["points"]
        if kind != "base" or len(pts) < 2:
            out.append(s)
            continue
        seg = pts[-1] - pts[0]
        length_px = float(np.linalg.norm(seg) * L)
        if length_px < args.min_length_px:
            out.append(s); kept += 1; continue
        mid = pts[len(pts) // 2]
        ix = int(np.clip(mid[0] * W, 0, W - 1)); iy = int(np.clip(mid[1] * H, 0, H - 1))
        phi = math.atan2(seg[1], seg[0])
        th = float(theta[iy, ix])
        diff = abs((phi - th + math.pi / 2) % math.pi - math.pi / 2)
        if not args.force_long and diff < math.radians(args.angle_tol_deg):
            out.append(s); kept += 1; continue
        # replacement: short local-tangent strokes along original footprint
        n = max(2, int(args.pieces))
        seglen_px = min(args.piece_max_px, max(10.0, length_px / (n + 0.5)))
        width = np.full(2, float(np.mean(s["widths"])) * float(args.width_mul))
        for frac in np.linspace(0.2, 0.8, n):
            idx = min(len(pts) - 1, max(0, int((len(pts) - 1) * frac)))
            c = pts[idx]
            ix = int(np.clip(c[0] * W, 0, W - 1)); iy = int(np.clip(c[1] * H, 0, H - 1))
            th2 = float(theta[iy, ix])
            ca, sa = math.cos(th2), math.sin(th2)
            half = seglen_px / (2.0 * L)
            pp = np.array([[c[0] - ca * half, c[1] - sa * half],
                           [c[0] + ca * half, c[1] + sa * half]])
            z = dict(s)
            z["points"] = pp
            z["widths"] = width
            z["color"] = np.asarray(rgb[iy, ix], dtype=np.float64)
            z["kind"] = "base_local"
            z["closed"] = False
            out.append(z)
        replaced += 1
    out.sort(key=lambda s: int(s.get("layer", 0)))
    ren = hgr._render_strokes(out, H, W, ss=1)
    psnr = hgr._psnr(rgb, ren)
    ssim = hgr._ssim(rgb, ren)
    png = os.path.join(args.outdir, f"base_orientation_fix_R{args.work_res}.png")
    Image.fromarray((np.clip(ren, 0, 1) * 255).astype(np.uint8)).save(png)
    js = os.path.join(args.outdir, "base_orientation_fix_strokes.json")
    save_strokes(js, out)
    report = {
        "input": os.path.abspath(args.input), "work_res": args.work_res,
        "strokes_in": len(strokes), "strokes_out": len(out),
        "base_replaced": replaced, "base_kept": kept,
        "min_length_px": args.min_length_px, "angle_tol_deg": args.angle_tol_deg,
        "pieces": args.pieces, "piece_max_px": args.piece_max_px,
        "psnr": round(psnr, 4), "ssim": round(ssim, 5),
        "png": os.path.abspath(png), "strokes": os.path.abspath(js),
        "seconds": round(time.time() - t0, 1),
    }
    json.dump(report, open(os.path.join(args.outdir, "base_orientation_fix_report.json"), "w",
                           encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
