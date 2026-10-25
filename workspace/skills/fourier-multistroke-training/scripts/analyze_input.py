#!/usr/bin/env python
"""Local image analysis: recommend a pipeline preset without calling an LLM."""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import hybrid_gen as hg  # noqa: E402


def analyze(path: str) -> dict:
    im = Image.open(path).convert("RGB")
    w, h = im.size
    # analysis copy: long side <= 512 keeps this fast and resolution-independent
    scale = min(1.0, 512.0 / max(w, h))
    small = im.resize((max(2, int(round(w * scale))), max(2, int(round(h * scale)))),
                      Image.LANCZOS) if scale < 1.0 else im
    rgb = np.asarray(small, dtype=np.float64) / 255.0
    H, W = rgb.shape[:2]
    lum = rgb @ np.array([0.2126, 0.7152, 0.0722])
    mx = rgb.max(axis=2); mn = rgb.min(axis=2)
    sat = float(((mx - mn) / np.maximum(mx, 1e-6)).mean())
    contrast = float(lum.std())
    gs = hg.gaussian_blur(lum.astype(np.float32), 0.8).astype(np.float64)
    gy, gx = np.gradient(gs)
    grad = float(np.hypot(gx, gy).mean())
    low = hg.gaussian_blur(lum.astype(np.float32), 4.0).astype(np.float64)
    hp = lum - low
    hp_ratio = float(hp.std() / max(lum.std(), 1e-6))
    # edge density at a modest threshold
    mag = np.hypot(gx, gy)
    edge_frac = float((mag > max(np.percentile(mag, 90), 1e-4)).mean())
    # soft atmospheric images: low contrast, low high-frequency ratio
    soft_score = float(np.clip((0.16 - contrast) / 0.10, 0, 1) * 0.6 +
                       np.clip((0.10 - hp_ratio) / 0.07, 0, 1) * 0.4)
    impasto_score = float(np.clip((hp_ratio - 0.035) / 0.05, 0, 1) * 0.6 +
                          np.clip((grad - 0.015) / 0.03, 0, 1) * 0.4)
    bg_frac = float(np.all(rgb > 0.94, axis=2).mean())
    # White-background line art / chibi: high local gradient but no impasto, and
    # a huge flat background.  Do not send it to the thick-dab branch.
    if bg_frac > 0.22 and sat < 0.35:
        preset, mode = "default", "detail"
    elif soft_score > 0.55:
        preset, mode = "soft", "soft_atmosphere"
    elif impasto_score > 0.62:
        preset, mode = "impasto", "thick_dabs"
    else:
        preset, mode = "default", "detail"
    megapixels = (w * h) / 1e6
    work_res = 1024 if bg_frac > 0.22 and megapixels < 1.0 else (1536 if megapixels >= 3.2 else 1408)
    rounds = 10 if (preset == "soft" or (bg_frac > 0.22 and megapixels < 1.0)) else 12
    detail_scale = 0.8 if (bg_frac > 0.22 and megapixels < 1.0) else (1.0 if preset != "soft" else 0.35)
    return {
        "input": os.path.abspath(path), "size": [w, h], "aspect": round(w / h, 4),
        "megapixels": round(megapixels, 2), "mean_rgb": np.round(rgb.mean(axis=(0, 1)), 4).tolist(),
        "contrast": round(contrast, 4), "saturation": round(sat, 4),
        "gradient": round(grad, 5), "edge_fraction": round(edge_frac, 4),
        "white_bg_fraction": round(bg_frac, 4),
        "high_freq_ratio": round(hp_ratio, 4),
        "soft_score": round(soft_score, 3), "impasto_score": round(impasto_score, 3),
        "recommend": {"preset": preset, "mode": mode, "work_res": work_res,
                      "rounds": rounds, "detail_scale": detail_scale,
                      "sat_mul": 1.02 if preset == "soft" else 1.05,
                      "dabs": 14000 if mode == "thick_dabs" else 0},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    rep = analyze(args.input)
    if args.out:
        json.dump(rep, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
