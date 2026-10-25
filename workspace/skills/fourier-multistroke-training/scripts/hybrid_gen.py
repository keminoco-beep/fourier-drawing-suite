#!/usr/bin/env python
"""Two-channel pure-stroke drawing: colour masses + drawn detail lines.

Channel A (base): very coarse colour regions -> wide scanline strokes, solid
coverage, colour sampled from the source. These are strokes, never fills.
Channel B (texture): finer regions -> thin hatch strokes, sampled colours.
Channel C (lines): high-gradient / dark-ink skeleton -> hand-drawn line strokes.

Everything is exported as (points, widths, colour, alpha, layer) and can be
compiled to `.fbs` Fourier strokes without any region_fill element.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass, asdict

import numpy as np
from PIL import Image
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
SKILL = os.path.join(WS, "fourier-brushstroke", "scripts")
if SKILL not in sys.path:
    sys.path.insert(0, SKILL)

from fbs_core.util import gaussian_blur  # noqa: E402
from fbs_core.decompose import (trace_boundary_loops, skeletonize,  # noqa: E402
                                clean_skeleton, skeleton_graph)
from fbs_core import structpaint  # noqa: E402


@dataclass
class HParams:
    work_res: int = 768
    # coarse base colour masses
    base_blur: float = 6.0
    base_k: int = 10
    base_min_area: int = 120
    base_width_min: float = 6.0
    base_width_max: float = 16.0
    base_width_area_ref: float = 100000.0
    base_spacing_mul: float = 0.48
    base_alpha: float = 1.0
    # medium texture hatch
    tex_blur: float = 2.0
    tex_k: int = 32
    tex_min_area: int = 80
    tex_width_min: float = 2.2
    tex_width_max: float = 6.0
    tex_width_area_ref: float = 30000.0
    tex_spacing_mul: float = 0.90
    tex_alpha: float = 0.80
    max_tex_strokes: int = 3500
    # detail lines
    edge_blur: float = 0.9
    edge_percentile: float = 94.0
    edge_min_len: float = 6.0
    edge_width: float = 1.35
    edge_alpha: float = 1.0
    edge_dark: float = 0.88
    max_edge_strokes: int = 1400
    dark_ink_lum: float = 0.30
    # boundaries
    boundary_width: float = 1.4
    min_boundary_area: int = 400
    max_boundary_strokes: int = 900
    bg_lo: float = 0.945
    color_jitter: float = 0.012
    seed: int = 20261021


def load_image(path: str, res: int) -> np.ndarray:
    im = Image.open(path).convert("RGB")
    if max(im.size) != res:
        im = im.resize((res, int(round(res * im.size[1] / im.size[0]))), Image.LANCZOS)
    return np.asarray(im, dtype=np.float64) / 255.0


def quantize(rgb: np.ndarray, k: int):
    u8 = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)
    q = Image.fromarray(u8, "RGB").quantize(colors=int(k), method=Image.MEDIANCUT, dither=Image.NONE)
    labels = np.asarray(q, dtype=np.int32)
    centers = np.zeros((int(k), 3), dtype=np.float64)
    present = np.zeros(int(k), dtype=bool)
    for i in range(int(k)):
        m = labels == i
        if m.any():
            centers[i] = np.median(rgb[m], axis=0)
            present[i] = True
    return labels, centers, present


def orientation_field(rgb: np.ndarray, sigma_t=2.2, sigma_g=1.0):
    gray = rgb @ np.array([0.2126, 0.7152, 0.0722])
    gs = gaussian_blur(gray.astype(np.float32), sigma_g).astype(np.float64)
    gy, gx = np.gradient(gs)
    jxx = gaussian_blur((gx * gx).astype(np.float32), sigma_t)
    jyy = gaussian_blur((gy * gy).astype(np.float32), sigma_t)
    jxy = gaussian_blur((gx * gy).astype(np.float32), sigma_t)
    return jxx, jyy, jxy


def region_orientation(mask, jxx, jyy, jxy) -> float:
    ys, xs = np.nonzero(mask)
    if len(xs) < 3:
        return 0.0
    x = xs.astype(np.float64) - xs.mean()
    y = ys.astype(np.float64) - ys.mean()
    cxx, cyy, cxy = float((x * x).mean()), float((y * y).mean()), float((x * y).mean())
    pca = 0.5 * math.atan2(2 * cxy, cxx - cyy)
    ratio = (cxx + cyy) / max(1e-6, math.sqrt((cxx - cyy) ** 2 + 4 * cxy ** 2))
    m = mask.astype(np.float64)
    sxx = float((jxx * m).sum()); syy = float((jyy * m).sum()); sxy = float((jxy * m).sum())
    tan = 0.5 * math.atan2(2 * sxy, sxx - syy) + math.pi / 2
    coh = math.sqrt((sxx - syy) ** 2 + 4 * sxy ** 2) / max(sxx + syy, 1e-9)
    if ratio > 1.7:
        return float(pca)
    if coh > 0.06:
        return float(tan)
    return float(pca)


def components(label_img: np.ndarray, fg: np.ndarray, min_area: int, k: int):
    out = []
    for lab in range(k):
        m = (label_img == lab) & fg
        if not m.any():
            continue
        cc, n = ndi.label(m, structure=np.ones((3, 3), dtype=np.int32))
        sizes = np.bincount(cc.ravel())
        for cid in range(1, n + 1):
            a = int(sizes[cid])
            if a < min_area:
                continue
            out.append({"area": a, "label": lab, "mask": cc == cid})
    out.sort(key=lambda d: -d["area"])
    return out


def scanline_hatch(cm, bbox, ang, width, spacing, color, p, rng, max_strokes=100000):
    x0, y0, x1, y1 = bbox
    ca, sa = math.cos(ang), math.sin(ang)
    corners = np.array([[x0, y0], [x1, y0], [x0, y1], [x1, y1]], dtype=np.float64)
    u = corners[:, 0] * ca + corners[:, 1] * sa
    v = -corners[:, 0] * sa + corners[:, 1] * ca
    umin, umax = float(u.min()), float(u.max())
    vmin, vmax = float(v.min()), float(v.max())
    levels = np.arange(vmin + rng.uniform(0, spacing), vmax, spacing)
    H, W = cm.shape
    out = []
    for vl in levels:
        us = np.arange(umin, umax, 1.0)
        x = us * ca - vl * sa
        y = us * sa + vl * ca
        xi = np.rint(x).astype(np.int64)
        yi = np.rint(y).astype(np.int64)
        ok = (xi >= x0) & (xi < x1) & (yi >= y0) & (yi < y1)
        if not ok.any():
            continue
        ok[ok] = cm[yi[ok], xi[ok]]
        idx = np.nonzero(ok)[0]
        if len(idx) < 2:
            continue
        splits = np.nonzero(np.diff(idx) > 3)[0]
        for g in np.split(idx, splits + 1):
            if len(g) < 2:
                continue
            seg_len = float(len(g))
            if seg_len < max(3.0, width * 0.28):
                continue
            a = int(g[0] + round(width * 0.10))
            b = int(g[-1] - round(width * 0.10))
            if b - a < 1:
                a, b = int(g[0]), int(g[-1])
            if b - a < 1:
                continue
            uu = us[[a, (a + b) // 2, b]]
            pts = np.stack([uu * ca - vl * sa, uu * sa + vl * ca], axis=1)
            pts += rng.normal(0.0, 0.20, pts.shape)
            # pressure profile: ends thinner, middle full
            w = width * np.array([0.68, 1.0, 0.68])
            # sample local source colour at three points
            cs = []
            for (px, py) in pts:
                ix = int(np.clip(round(px), 0, W - 1)); iy = int(np.clip(round(py), 0, H - 1))
                cs.append(p[0][iy, ix])
            c = np.median(np.asarray(cs), axis=0)
            c = np.clip(c + rng.normal(0.0, p[1], 3), 0.0, 1.0)
            out.append({"points": pts, "widths": w, "color": c, "alpha": p[2]})
            if len(out) >= max_strokes:
                return out
    return out


def edge_line_strokes(rgb, fg, hp: HParams, rng):
    H, W = rgb.shape[:2]
    rs = max(H, W) / 1024.0
    gray = rgb @ np.array([0.2126, 0.7152, 0.0722])
    gs = gaussian_blur(gray.astype(np.float32), hp.edge_blur).astype(np.float64)
    gy, gx = np.gradient(gs)
    mag = np.hypot(gx, gy)
    thr = max(float(np.percentile(mag, hp.edge_percentile)), 0.02)
    edge = mag > thr
    # dark ink is always a line, even in low-gradient shadows
    edge |= (gray < hp.dark_ink_lum) & (mag > 0.008)
    # keep only lines on/near actual foreground content (drop faint background specks)
    near_fg = ndi.binary_dilation(fg, iterations=3)
    edge &= near_fg
    # ignore the outer white margin
    edge &= np.arange(H)[:, None] >= 1
    edge &= np.arange(W)[None, :] >= 1
    padded = np.pad(edge, 1, mode="constant", constant_values=False)
    sk = clean_skeleton(skeletonize(padded, 4))[1:-1, 1:-1]
    branches = skeleton_graph(sk)
    items = []
    for path in branches:
        if len(path) < 3:
            continue
        pts = np.asarray([(x, y) for (y, x) in path], dtype=np.float64)
        arclen = float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())
        if arclen < hp.edge_min_len * rs:
            continue
        # colour = median source colour along the branch, then darken into ink
        cols = []
        for x, y in pts:
            xi = int(np.clip(round(x), 0, W - 1)); yi = int(np.clip(round(y), 0, H - 1))
            cols.append(rgb[yi, xi])
        col = np.median(np.asarray(cols), axis=0)
        items.append({"pts": pts, "len": arclen, "color": col})
    # longest first, then merge collinear fragments into long human strokes
    items.sort(key=lambda d: -d["len"])
    items = items[: int(hp.max_edge_strokes * 2)]
    lines = []
    for it in items:
        if len(lines) >= hp.max_edge_strokes:
            break
        pts = it["pts"]
        if len(pts) > 40:
            idx = np.linspace(0, len(pts) - 1, 40).astype(np.int64)
            pts = pts[idx]
        lines.append({"kind": "detail", "layer": 5, "closed": False,
                      "points": pts, "widths": np.full(len(pts), hp.edge_width * rs),
                      "color": np.clip(it["color"] * hp.edge_dark, 0.0, 1.0),
                      "alpha": hp.edge_alpha, "length": it["len"]})
    return lines


def boundary_strokes(rgb, label_img, fg, k, hp, rng):
    H, W = rgb.shape[:2]
    rs = max(H, W) / 1024.0
    out = []
    for comp in components(label_img, fg, hp.min_boundary_area, k):
        cm = comp["mask"]
        ys, xs = np.nonzero(cm)
        if len(ys) == 0:
            continue
        for loop in trace_boundary_loops(cm):
            pts = np.asarray(loop, dtype=np.float64)
            if len(pts) > 1 and np.allclose(pts[0], pts[-1]):
                pts = pts[:-1]
            if len(pts) < 8:
                continue
            d = np.linalg.norm(np.diff(np.vstack([pts, pts[:1]]), axis=0), axis=1)
            arclen = float(d.sum())
            if arclen < 8.0 * rs:
                continue
            # keep enough vertices that a closed contour never becomes a chord
            n_keep = min(160, max(24, int(arclen / 2.0)))
            idx = np.linspace(0, len(pts) - 1, n_keep).astype(np.int64)
            lp = pts[idx] + 0.5
            # darkest ink colour around the loop
            cols = []
            for x, y in lp:
                xi = int(np.clip(round(x), 0, W - 1)); yi = int(np.clip(round(y), 0, H - 1))
                nb = rgb[max(0, yi - 2):yi + 3, max(0, xi - 2):xi + 3].reshape(-1, 3)
                lum = nb @ np.array([0.2126, 0.7152, 0.0722])
                cols.append(nb[int(np.argmin(lum))])
            col = np.median(np.asarray(cols), axis=0)
            out.append({"kind": "boundary", "layer": 4, "closed": True,
                        "points": lp, "widths": np.full(len(lp), hp.boundary_width * rs),
                        "color": np.clip(col * 0.9, 0.0, 1.0), "alpha": 1.0,
                        "length": arclen})
            if len(out) >= hp.max_boundary_strokes:
                return out
    return out


def generate(rgb: np.ndarray, hp: HParams, rng: np.random.Generator):
    H, W = rgb.shape[:2]
    L = float(max(H, W))
    rs = L / 1024.0  # keep line count / visual line weight independent of work res
    bg = np.all(rgb > hp.bg_lo, axis=2)
    fg = ~bg
    jxx, jyy, jxy = orientation_field(rgb)

    # ---- coarse base ----
    sm = gaussian_blur(rgb.astype(np.float32), hp.base_blur).astype(np.float64)
    lab, cen, present = quantize(sm, hp.base_k)
    base = []
    for comp in components(lab, fg, hp.base_min_area, hp.base_k):
        cm = comp["mask"]; area = comp["area"]
        ys, xs = np.nonzero(cm)
        x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
        ang = region_orientation(cm, jxx, jyy, jxy)
        w = float(np.clip(hp.base_width_min + (hp.base_width_max - hp.base_width_min)
                          * math.sqrt(area / hp.base_width_area_ref),
                          hp.base_width_min, hp.base_width_max)) * rs
        spacing = max(2.5 * rs, w * hp.base_spacing_mul)
        out = scanline_hatch(cm, (x0, y0, x1, y1), ang, w, spacing, np.array([0.5, 0.5, 0.5]),
                             (rgb, hp.color_jitter, hp.base_alpha), rng)
        for st in out:
            st["kind"] = "base"; st["layer"] = 0
            st["points"] = st["points"] / L; st["widths"] = st["widths"] / L
            st["length"] = float(np.linalg.norm(np.diff(st["points"], axis=0), axis=1).sum() * L)
            base.append(st)
    # ---- medium texture ----
    sm2 = gaussian_blur(rgb.astype(np.float32), hp.tex_blur).astype(np.float64)
    lab2, cen2, present2 = quantize(sm2, hp.tex_k)
    tex = []
    for comp in components(lab2, fg, hp.tex_min_area, hp.tex_k):
        if len(tex) >= hp.max_tex_strokes:
            break
        cm = comp["mask"]; area = comp["area"]
        ys, xs = np.nonzero(cm)
        x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
        ang = region_orientation(cm, jxx, jyy, jxy)
        w = float(np.clip(hp.tex_width_min + (hp.tex_width_max - hp.tex_width_min)
                          * math.sqrt(area / hp.tex_width_area_ref),
                          hp.tex_width_min, hp.tex_width_max)) * rs
        spacing = max(1.8 * rs, w * hp.tex_spacing_mul)
        out = scanline_hatch(cm, (x0, y0, x1, y1), ang, w, spacing, np.array([0.5, 0.5, 0.5]),
                             (rgb, hp.color_jitter, hp.tex_alpha), rng,
                             max_strokes=hp.max_tex_strokes - len(tex))
        for st in out:
            st["kind"] = "tex"; st["layer"] = 2
            st["points"] = st["points"] / L; st["widths"] = st["widths"] / L
            st["length"] = float(np.linalg.norm(np.diff(st["points"], axis=0), axis=1).sum() * L)
            tex.append(st)
    # ---- drawn lines ----
    detail = edge_line_strokes(rgb, fg, hp, rng)
    for st in detail:
        st["points"] = st["points"] / L; st["widths"] = st["widths"] / L
    # ---- region outlines ----
    outline = boundary_strokes(rgb, lab, fg, hp.base_k, hp, rng)
    for st in outline:
        st["points"] = st["points"] / L; st["widths"] = st["widths"] / L
    strokes = base + tex + outline + detail
    stats = {
        "base": len(base), "tex": len(tex), "boundary": len(outline), "detail": len(detail),
        "stroke_count": len(strokes),
        "base_px": int(sum(s["length"] for s in base)),
        "tex_px": int(sum(s["length"] for s in tex)),
    }
    return strokes, stats


# rendering / metrics are reused from hatch_gen (identical semantics)
def _load_hatch_gen():
    sys.path.insert(0, os.path.dirname(__file__))
    import hatch_gen  # noqa: PLC0415
    return hatch_gen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--work-res", type=int, default=768)
    ap.add_argument("--seed", type=int, default=20261021)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    hp = HParams(work_res=args.work_res, seed=args.seed)
    rgb = load_image(args.input, args.work_res)
    H, W = rgb.shape[:2]
    rng = np.random.default_rng(args.seed)
    t0 = time.time()
    strokes, stats = generate(rgb, hp, rng)
    gen_s = time.time() - t0
    hg = _load_hatch_gen()
    t1 = time.time()
    rec = hg._render_strokes(strokes, H, W, ss=2)
    render_s = time.time() - t1
    psnr = hg._psnr(rgb, rec); ssim = hg._ssim(rgb, rec)
    Image.fromarray(np.clip(rec * 255, 0, 255).astype(np.uint8)).save(
        os.path.join(args.outdir, "hybrid_R%d.png" % W))
    report = {"params": asdict(hp), "stats": stats, "psnr": psnr, "ssim": ssim,
              "gen_s": gen_s, "render_s": render_s, "size": [W, H]}
    with open(os.path.join(args.outdir, "hybrid_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1, default=float)
    print(json.dumps({k: report[k] for k in ("psnr", "ssim", "gen_s", "render_s")}, ensure_ascii=False))
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
