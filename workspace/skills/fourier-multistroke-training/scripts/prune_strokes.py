#!/usr/bin/env python
"""Stroke-budget pruning: remove the least informative base/texture strokes.

Detail/outline/face-ink strokes are never pruned. Candidate keep-fractions are
rendered and scored; the best J wins (quality + fewer strokes).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
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


def load_strokes(path: str):
    data = json.load(open(path, encoding="utf-8"))
    out = []
    for d in data:
        s = dict(d)
        s["points"] = np.asarray(s["points"], dtype=np.float64)
        s["widths"] = np.asarray(s["widths"], dtype=np.float64)
        s["color"] = np.asarray(s["color"], dtype=np.float64)
        out.append(s)
    return out


def save_strokes(path: str, strokes: list[dict]) -> None:
    payload = []
    for s in strokes:
        d = dict(s)
        d["points"] = np.round(np.asarray(d["points"]), 6).tolist()
        d["widths"] = np.round(np.asarray(d["widths"]), 6).tolist()
        d["color"] = np.round(np.asarray(d["color"]), 5).tolist()
        d["alpha"] = float(d.get("alpha", 1.0))
        d["closed"] = bool(d.get("closed", False))
        d["layer"] = int(d.get("layer", 0))
        d["kind"] = str(d.get("kind", ""))
        payload.append(d)
    json.dump(payload, open(path, "w", encoding="utf-8"), ensure_ascii=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strokes", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--stroke-weight", type=float, default=0.10)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    rgb = hg.load_image(args.input, 1024)
    H, W = rgb.shape[:2]
    strokes = load_strokes(args.strokes)
    p0 = hgr._psnr(rgb, hgr._render_strokes(strokes, H, W, ss=1))
    s0 = hgr._ssim(rgb, hgr._render_strokes(strokes, H, W, ss=1))
    n0 = len(strokes)
    line_kinds = {"detail", "outline", "faceink", "boundary", "hairflow"}
    prunable = [i for i, s in enumerate(strokes) if s.get("kind") not in line_kinds]
    importance = np.asarray([
        float(s.get("length", 0.0)) * float(np.mean(s.get("widths", [1.0]))) *
        (1.0 + 0.8 * (s.get("kind") == "base"))
        for s in strokes
    ])
    order = sorted(prunable, key=lambda i: importance[i])  # weakest first
    results = []
    best = None
    for keep_frac in (1.0, 0.92, 0.85, 0.78, 0.70, 0.62, 0.55, 0.48):
        drop_n = int(round(len(prunable) * (1.0 - keep_frac)))
        drop = set(order[:drop_n])
        sub = [s for i, s in enumerate(strokes) if i not in drop]
        if not sub:
            continue
        img = hgr._render_strokes(sub, H, W, ss=1)
        psnr = hgr._psnr(rgb, img); ssim = hgr._ssim(rgb, img)
        J = psnr + 1.5 * ssim - args.stroke_weight * (len(sub) / 1000.0)
        rec = {"keep_frac": keep_frac, "strokes": len(sub), "psnr": round(psnr, 4),
               "ssim": round(ssim, 5), "J": round(J, 4),
               "d_psnr": round(psnr - p0, 4), "d_ssim": round(ssim - s0, 5)}
        results.append(rec)
        if best is None or J > best[0]["J"]:
            best = (rec, sub, img)
        print(json.dumps(rec, ensure_ascii=False))
    rec, sub, img = best
    save_strokes(os.path.join(args.outdir, "pruned_strokes.json"), sub)
    Image.fromarray(np.clip(img * 255, 0, 255).astype(np.uint8)).save(
        os.path.join(args.outdir, "pruned_R1024.png"))
    report = {"baseline": {"strokes": n0, "psnr": round(p0, 4), "ssim": round(s0, 5)},
              "chosen": rec, "candidates": results,
              "strokes_json": os.path.join(args.outdir, "pruned_strokes.json")}
    json.dump(report, open(os.path.join(args.outdir, "prune_report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("[prune] chosen", json.dumps(rec, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
