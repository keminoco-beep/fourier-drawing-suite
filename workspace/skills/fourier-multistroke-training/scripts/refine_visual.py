#!/usr/bin/env python
"""Visual refinement: taper ink lines + add hair-flow strokes (pure strokes).

The hair-flow layer follows the source structure tensor inside the blue-hair
mask, using long curved strokes with sampled colour and soft alpha. It breaks
the flat quantisation mosaic without adding any fill element.
"""
from __future__ import annotations

import argparse
import json
import math
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
        d["points"] = np.round(np.asarray(d["points"], dtype=np.float64), 6).tolist()
        d["widths"] = np.round(np.asarray(d["widths"], dtype=np.float64), 6).tolist()
        d["color"] = np.round(np.asarray(d["color"], dtype=np.float64), 5).tolist()
        d["alpha"] = float(d.get("alpha", 1.0))
        d["closed"] = bool(d.get("closed", False))
        d["layer"] = int(d.get("layer", 0))
        d["kind"] = str(d.get("kind", ""))
        payload.append(d)
    json.dump(payload, open(path, "w", encoding="utf-8"), ensure_ascii=False)


def hair_flow(rgb: np.ndarray, count: int = 520, seed: int = 20261022) -> list[dict]:
    H, W = rgb.shape[:2]
    L = float(max(H, W))
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    hair = (b > r * 1.12) & (b > g * 1.02) & (b > 0.20) & (r < 0.78)
    hair = ndi.binary_opening(hair, iterations=1)
    hair = ndi.binary_closing(hair, iterations=2)
    jxx, jyy, jxy = hg.orientation_field(rgb)
    base_ang = 0.5 * np.arctan2(2 * jxy, jxx - jyy) + math.pi / 2
    cos_t, sin_t = np.cos(base_ang), np.sin(base_ang)
    rng = np.random.default_rng(seed)
    covered = np.zeros((H, W), dtype=bool)
    flow: list[dict] = []
    step = 1.5
    ys, xs = np.mgrid[20:H - 20:13, 20:W - 20:13]
    cands = list(zip(xs.ravel(), ys.ravel()))
    rng.shuffle(cands)

    def trace(cx, cy, sign, maxlen):
        pts = []
        x, y = float(cx), float(cy)
        ang = None
        for _ in range(int(maxlen / step) + 1):
            xi, yi = int(round(x)), int(round(y))
            if xi < 1 or yi < 1 or xi >= W - 1 or yi >= H - 1 or not hair[yi, xi]:
                break
            ca, sa = float(cos_t[yi, xi]), float(sin_t[yi, xi])
            if ang is not None and ca * math.cos(ang) + sa * math.sin(ang) < 0:
                ca, sa = -ca, -sa
            ang = math.atan2(sa, ca)
            x += sign * ca * step
            y += sign * sa * step
            pts.append((x, y))
        return np.asarray(pts, dtype=np.float64)

    for cx, cy in cands:
        if len(flow) >= count:
            break
        cx, cy = int(cx), int(cy)
        if covered[cy, cx] or not hair[cy, cx]:
            continue
        p1 = trace(cx, cy, 1, 70)
        p2 = trace(cx, cy, -1, 70)
        path = np.vstack([p2[::-1], p1]) if len(p2) and len(p1) else (p1 if len(p1) else p2)
        if len(path) < 6:
            continue
        if len(path) > 40:
            path = path[np.linspace(0, len(path) - 1, 40).astype(int)]
        idx = (np.clip(path[:, 1].astype(int), 0, H - 1),
               np.clip(path[:, 0].astype(int), 0, W - 1))
        col = np.median(rgb[idx], axis=0)
        w = float(rng.uniform(2.2, 4.2))
        t = np.linspace(0.0, 1.0, len(path))
        prof = 0.65 + 0.5 * np.sin(np.pi * t) ** 0.5
        flow.append({
            "kind": "hairflow", "layer": 8, "closed": False,
            "points": path / L, "widths": (w * prof) / L,
            "color": np.clip(col * 0.95, 0.0, 1.0), "alpha": 0.72,
            "length": float(np.linalg.norm(np.diff(path, axis=0), axis=1).sum()),
        })
        for x, y in path:
            xi, yi = int(round(x)), int(round(y))
            rr = 5
            covered[max(0, yi - rr):yi + rr + 1, max(0, xi - rr):xi + rr + 1] |= \
                hair[max(0, yi - rr):yi + rr + 1, max(0, xi - rr):xi + rr + 1]
    return flow


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strokes", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--work-res", type=int, default=1408)
    ap.add_argument("--flow-count", type=int, default=520)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    rgb = hg.load_image(args.input, int(args.work_res))
    H, W = rgb.shape[:2]
    strokes = load_strokes(args.strokes)
    before = hgr._render_strokes(strokes, H, W, ss=1)
    p0, s0 = hgr._psnr(rgb, before), hgr._ssim(rgb, before)
    # taper the ink lines (detail/outline/faceink) for hand-drawn pressure
    for s in strokes:
        if s.get("kind") in ("detail", "faceink", "outline"):
            w = np.asarray(s["widths"], dtype=np.float64)
            n = len(w)
            if n >= 3:
                t = np.linspace(0.0, 1.0, n)
                s["widths"] = w * (0.62 + 0.55 * np.sin(np.pi * t) ** 0.55)
    flow = hair_flow(rgb, count=int(args.flow_count))
    refined = strokes + flow
    after = hgr._render_strokes(refined, H, W, ss=1)
    p1, s1 = hgr._psnr(rgb, after), hgr._ssim(rgb, after)
    print(f"[visual] {len(strokes)} + {len(flow)} flow = {len(refined)} strokes; "
          f"PSNR {p0:.3f}->{p1:.3f} SSIM {s0:.4f}->{s1:.4f}")
    out_json = os.path.join(args.outdir, "visual_strokes.json")
    save_strokes(out_json, refined)
    Image.fromarray(np.clip(after * 255, 0, 255).astype(np.uint8)).save(
        os.path.join(args.outdir, f"visual_R{args.work_res}.png"))
    report = {
        "strokes_before": len(strokes), "flow_strokes": len(flow),
        "strokes_after": len(refined),
        "psnr_before": round(p0, 4), "psnr_after": round(p1, 4),
        "ssim_before": round(s0, 5), "ssim_after": round(s1, 5),
        "strokes_json": out_json,
    }
    json.dump(report, open(os.path.join(args.outdir, "visual_report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
