#!/usr/bin/env python
"""Final artist-touch-up round: add a small dark face/ink line layer.

Pure strokes only. The layer is intentionally small (~200 strokes) so global
fidelity barely moves while eyes/brows/mouth/hair strands become readable.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

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
SKILL = os.path.join(WS, "fourier-brushstroke", "scripts")
for p in (HERE, SKILL):
    if p not in sys.path:
        sys.path.insert(0, p)

import hatch_gen as hgr  # noqa: E402
import hybrid_gen as hg  # noqa: E402
from fbs_core.decompose import skeletonize, clean_skeleton, skeleton_graph  # noqa: E402


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


def face_ink_strokes(rgb: np.ndarray, count: int = 240) -> list[dict]:
    H, W = rgb.shape[:2]
    gray = rgb @ np.array([0.2126, 0.7152, 0.0722])
    gs = hg.gaussian_blur(gray.astype(np.float32), 0.7).astype(np.float64)
    gy, gx = np.gradient(gs)
    mag = np.hypot(gx, gy)
    fx0, fx1 = int(W * 0.30), int(W * 0.72)
    fy0, fy1 = int(H * 0.27), int(H * 0.56)
    face = np.zeros((H, W), dtype=bool)
    face[fy0:fy1, fx0:fx1] = True
    thr = float(np.percentile(mag[face], 70))
    edge = face & (mag > thr) & (gray < 0.74)
    padded = np.pad(edge, 1, mode="constant", constant_values=False)
    sk = clean_skeleton(skeletonize(padded, 3))[1:-1, 1:-1]
    branches = skeleton_graph(sk)
    items = []
    for path in branches:
        if len(path) < 3:
            continue
        pts = np.asarray([(int(x), int(y)) for (y, x) in path], dtype=np.float64)
        arclen = float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())
        if arclen < 3.0:
            continue
        items.append((arclen, pts))
    items.sort(key=lambda kv: -kv[0])
    out = []
    for arclen, pts in items[:count]:
        if len(pts) > 26:
            pts = pts[np.linspace(0, len(pts) - 1, 26).astype(int)]
        cols = []
        for x, y in pts:
            xi = int(np.clip(int(round(x)), 0, W - 1))
            yi = int(np.clip(int(round(y)), 0, H - 1))
            nb = rgb[max(0, yi - 1):yi + 2, max(0, xi - 1):xi + 2].reshape(-1, 3)
            lum = nb @ np.array([0.2126, 0.7152, 0.0722])
            k = max(1, len(lum) // 3)
            cols.append(np.mean(nb[np.argsort(lum)[:k]], axis=0))
        col = np.median(np.asarray(cols), axis=0) * 0.55
        # taper: thin at the ends, slightly thicker in the middle
        s = np.linspace(0.0, 1.0, len(pts))
        w = (1.15 + 0.25 * np.sin(np.pi * s)) / float(max(H, W))
        out.append({
            "kind": "faceink", "layer": 7, "closed": False,
            "points": pts / float(max(H, W)),
            "widths": w, "color": np.clip(col, 0.0, 1.0),
            "alpha": 0.95, "length": arclen,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--count", type=int, default=240)
    ap.add_argument("--work-res", type=int, default=1024)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    rgb = hg.load_image(args.input, int(args.work_res))
    H, W = rgb.shape[:2]
    strokes = load_strokes(os.path.join(args.run, "best_strokes.json"))
    before = hgr._render_strokes(strokes, H, W, ss=1)
    p0, s0 = hgr._psnr(rgb, before), hgr._ssim(rgb, before)
    add = face_ink_strokes(rgb, args.count)
    refined = strokes + add
    after = hgr._render_strokes(refined, H, W, ss=1)
    p1, s1 = hgr._psnr(rgb, after), hgr._ssim(rgb, after)
    print(f"[refine] {len(strokes)} + {len(add)} strokes; "
          f"PSNR {p0:.3f}->{p1:.3f} SSIM {s0:.4f}->{s1:.4f}")
    if p1 < p0 - 0.12:
        print("[refine] rejected: fidelity drop too large")
        return 0
    out_json = os.path.join(args.outdir, "refined_strokes.json")
    with open(out_json, "w", encoding="utf-8") as fh:
        payload = []
        for s in refined:
            d = dict(s)
            d["points"] = np.round(np.asarray(d["points"]), 6).tolist()
            d["widths"] = np.round(np.asarray(d["widths"]), 6).tolist()
            d["color"] = np.round(np.asarray(d["color"]), 5).tolist()
            d["alpha"] = float(d.get("alpha", 1.0))
            d["closed"] = bool(d.get("closed", False))
            d["layer"] = int(d.get("layer", 0))
            d["kind"] = str(d.get("kind", ""))
            payload.append(d)
        json.dump(payload, fh, ensure_ascii=False)
    Image.fromarray(np.clip(after * 255, 0, 255).astype(np.uint8)).save(
        os.path.join(args.outdir, f"refined_R{args.work_res}.png"))
    before_img = Image.fromarray(np.clip(before * 255, 0, 255).astype(np.uint8))
    after_img = Image.fromarray(np.clip(after * 255, 0, 255).astype(np.uint8))
    sheet = Image.new("RGB", (W * 2 + 8, H), "white")
    sheet.paste(before_img, (0, 0)); sheet.paste(after_img, (W + 8, 0))
    sheet.save(os.path.join(args.outdir, "refine_compare.png"))
    report = {
        "run": os.path.abspath(args.run), "input": os.path.abspath(args.input),
        "strokes_before": len(strokes), "strokes_added": len(add),
        "strokes_after": len(refined),
        "psnr_before": round(p0, 4), "psnr_after": round(p1, 4),
        "ssim_before": round(s0, 5), "ssim_after": round(s1, 5),
        "strokes_json": out_json,
    }
    with open(os.path.join(args.outdir, "refine_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
