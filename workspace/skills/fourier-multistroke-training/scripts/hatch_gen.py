#!/usr/bin/env python
"""Pure-stroke colored-hatching generator (prototype v2).

Every mark is a stroke: Fourier-ready polyline, full width, RGB, alpha, layer.
No region fill element is ever produced. Hatching is scanline based (human
parallel strokes) with a per-region flow orientation; outlines are traced region
boundaries, coloured with the darkest local ink.
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
from fbs_core.decompose import trace_boundary_loops  # noqa: E402
from fbs_core import structpaint  # noqa: E402


@dataclass
class Params:
    work_res: int = 768
    k_colors: int = 16
    pre_blur: float = 2.0
    bg_lo: float = 0.945
    min_component: int = 70
    min_area: int = 260
    base_min_area: int = 600
    base_alpha: float = 1.0
    hatch_texture: bool = True
    width_min: float = 2.7
    width_max: float = 12.0
    width_area_ref: float = 60000.0
    spacing_mul: float = 0.78
    boundary_width: float = 1.9
    detail_boundary_width: float = 1.25
    min_boundary_len: float = 6.0
    boundary_dark: float = 0.92
    color_jitter: float = 0.018
    max_hatch_strokes: int = 8000
    max_boundary_strokes: int = 3000
    seed: int = 20261021
    boundary_layers: bool = True


def load_image(path: str, res: int) -> np.ndarray:
    im = Image.open(path).convert("RGB")
    if max(im.size) != res:
        im = im.resize((res, int(round(res * im.size[1] / im.size[0]))), Image.LANCZOS)
    return np.asarray(im, dtype=np.float64) / 255.0


def quantize(rgb: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    u8 = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)
    q = Image.fromarray(u8, "RGB").quantize(colors=int(k), method=Image.MEDIANCUT, dither=Image.NONE)
    labels = np.asarray(q, dtype=np.int32)
    centers = np.zeros((int(k), 3), dtype=np.float64)
    for i in range(int(k)):
        m = labels == i
        if m.any():
            centers[i] = np.median(rgb[m], axis=0)
    return labels, centers


def structure_tensor(rgb: np.ndarray, sigma_t: float = 2.2, sigma_g: float = 1.0):
    gray = rgb @ np.array([0.2126, 0.7152, 0.0722])
    gs = gaussian_blur(gray.astype(np.float32), sigma_g).astype(np.float64)
    gy, gx = np.gradient(gs)
    jxx = gaussian_blur((gx * gx).astype(np.float32), sigma_t)
    jyy = gaussian_blur((gy * gy).astype(np.float32), sigma_t)
    jxy = gaussian_blur((gx * gy).astype(np.float32), sigma_t)
    return jxx, jyy, jxy


def _component_orientation(mask: np.ndarray, jxx, jyy, jxy) -> float:
    ys, xs = np.nonzero(mask)
    if len(xs) < 3:
        return 0.0
    x = xs.astype(np.float64) - xs.mean()
    y = ys.astype(np.float64) - ys.mean()
    cov_xx = float((x * x).mean())
    cov_yy = float((y * y).mean())
    cov_xy = float((x * y).mean())
    pca_ang = 0.5 * math.atan2(2 * cov_xy, cov_xx - cov_yy)
    pca_ratio = (cov_xx + cov_yy) / max(1e-6, math.sqrt((cov_xx - cov_yy) ** 2 + 4 * cov_xy ** 2))
    m = mask.astype(np.float64)
    sxx = float((jxx * m).sum())
    syy = float((jyy * m).sum())
    sxy = float((jxy * m).sum())
    tan = 0.5 * math.atan2(2 * sxy, sxx - syy) + math.pi / 2
    coh = math.sqrt((sxx - syy) ** 2 + 4 * sxy ** 2) / max(sxx + syy, 1e-9)
    # elongated regions follow their long axis; blobs follow the image flow
    if pca_ratio > 1.6:
        return float(pca_ang)
    if coh > 0.08:
        return float(tan)
    return float(pca_ang)


def _scanline_strokes(cm: np.ndarray, bbox: tuple[int, int, int, int], ang: float,
                      width: float, spacing: float, color: np.ndarray,
                      p: Params, rng: np.random.Generator, out: list[dict]) -> None:
    x0, y0, x1, y1 = bbox
    ca, sa = math.cos(ang), math.sin(ang)
    # support endpoints for every bbox corner
    corners = np.array([[x0, y0], [x1, y0], [x0, y1], [x1, y1]], dtype=np.float64)
    u = corners[:, 0] * ca + corners[:, 1] * sa
    v = -corners[:, 0] * sa + corners[:, 1] * ca
    umin, umax = float(u.min()), float(u.max())
    vmin, vmax = float(v.min()), float(v.max())
    # centre the v grid on the actual mask centroid to keep pattern stable
    ys, xs = np.nonzero(cm)
    vc = float((-xs.mean() * sa + ys.mean() * ca))
    v0 = vc - math.floor((vc - vmin) / spacing) * spacing
    levels = np.arange(v0, vmax + spacing, spacing)
    H, W = cm.shape
    for vl in levels:
        # sample along the rotated u axis at 1px
        us = np.arange(umin, umax, 1.0)
        x = us * ca - vl * sa
        y = us * sa + vl * ca
        xi = np.rint(x).astype(np.int64)
        yi = np.rint(y).astype(np.int64)
        ok = (xi >= x0) & (xi < x1) & (yi >= y0) & (yi < y1)
        if not ok.any():
            continue
        ok[ok] &= cm[yi[ok], xi[ok]]
        if not ok.any():
            continue
        # contiguous runs
        idx = np.nonzero(ok)[0]
        splits = np.nonzero(np.diff(idx) > 3)[0]
        groups = np.split(idx, splits + 1)
        for g in groups:
            if len(g) < 2:
                continue
            length = float(len(g)) * 1.0
            if length < max(2.0, width * 0.3):
                continue
            # shrink ends a little (human strokes stop before the exact edge)
            a = g[0] + int(round(width * 0.12))
            b = g[-1] - int(round(width * 0.12))
            if b - a < 1:
                a, b = g[0], g[-1]
            if b - a < 1:
                continue
            uu = us[[a, (a + b) // 2, b]]
            pts = np.stack([uu * ca - vl * sa, uu * sa + vl * ca], axis=1)
            # slight line wobble (hand-drawn), smaller than a pixel
            pts += rng.normal(0.0, 0.22, pts.shape)
            # pressure taper: thin ends, full middle
            s = np.array([0.72, 1.0, 0.72], dtype=np.float64)
            w = width * s
            c = np.clip(color + rng.normal(0.0, p.color_jitter, 3), 0.0, 1.0)
            out.append({
                "kind": "hatch", "layer": 1 if len(g) > 14 else 2, "closed": False,
                "points": pts, "widths": w, "color": c, "alpha": 1.0,
                "length": float(length),
            })


def _boundary_color(rgb: np.ndarray, pts: np.ndarray, dark: float) -> np.ndarray:
    H, W = rgb.shape[:2]
    cols = []
    for x, y in pts:
        xi = int(np.clip(round(x), 0, W - 1))
        yi = int(np.clip(round(y), 0, H - 1))
        nb = rgb[max(0, yi - 2):yi + 3, max(0, xi - 2):xi + 3].reshape(-1, 3)
        lum = nb @ np.array([0.2126, 0.7152, 0.0722])
        cols.append(nb[int(np.argmin(lum))])
    c = np.median(np.asarray(cols), axis=0)
    return np.clip(c * dark, 0.0, 1.0)


def _resample_loop(loop: np.ndarray, max_pts: int = 32) -> np.ndarray:
    pts = np.asarray(loop, dtype=np.float64)
    if len(pts) > 1 and np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    if len(pts) < 8:
        return np.empty((0, 2))
    # arclength resample
    d = np.linalg.norm(np.diff(np.vstack([pts, pts[:1]]), axis=0), axis=1)
    keep = np.concatenate([[True], d[1:] > 0.6])
    pts = pts[keep]
    if len(pts) < 3:
        return np.empty((0, 2))
    idx = np.linspace(0, len(pts) - 1, min(max_pts, len(pts))).astype(np.int64)
    return pts[idx] + 0.5


def generate(rgb: np.ndarray, p: Params, rng: np.random.Generator) -> tuple[list[dict], dict]:
    H, W = rgb.shape[:2]
    smooth = gaussian_blur(rgb.astype(np.float32), float(p.pre_blur)).astype(np.float64)
    labels, centers = quantize(smooth, p.k_colors)
    bg = np.all(rgb > p.bg_lo, axis=2)
    fg = ~bg

    # absorb tiny components into the nearest big one (avoid white holes)
    comps = []
    comp_label = []
    big = np.zeros((H, W), dtype=bool)
    tmp_id = np.zeros((H, W), dtype=np.int32)
    next_id = 1
    for lab in range(len(centers)):
        m = (labels == lab) & fg
        if not m.any():
            continue
        cc, n = ndi.label(m, structure=np.ones((3, 3), dtype=np.int32))
        sizes = np.bincount(cc.ravel())
        for cid in range(1, n + 1):
            area = int(sizes[cid])
            if area < p.min_component:
                continue
            sel = cc == cid
            comps.append({"area": area, "label": lab, "mask": sel, "id": next_id})
            comp_label.append(lab)
            if area >= p.min_component:
                big |= sel
            tmp_id[sel] = next_id
            next_id += 1
    if not comps:
        return [], {"stroke_count": 0}
    # nearest-big absorption for foreground not selected
    missing = fg & ~big
    if missing.any():
        _, inds = ndi.distance_transform_edt(~big, return_indices=True)
        nearest_lab = labels[inds[0], inds[1]]
        labels = np.where(missing, nearest_lab, labels)
        # owner id map by nearest selected pixel
        ids = tmp_id[inds[0], inds[1]]
        tmp_id = np.where(missing, ids, tmp_id)
    # re-extract components from cleaned labels
    comps = []
    used = np.zeros((H, W), dtype=bool)
    idx = 0
    for lab in range(len(centers)):
        m = (labels == lab) & fg & ~used
        if not m.any():
            continue
        cc, n = ndi.label(m, structure=np.ones((3, 3), dtype=np.int32))
        sizes = np.bincount(cc.ravel())
        for cid in range(1, n + 1):
            area = int(sizes[cid])
            if area < p.min_component:
                continue
            sel = cc == cid
            used |= sel
            comps.append({"area": area, "label": lab, "mask": sel, "id": idx})
            idx += 1
    comps.sort(key=lambda d: -d["area"])
    jxx, jyy, jxy = structure_tensor(rgb)
    strokes: list[dict] = []
    stats = {"components": len(comps), "hatch": 0, "boundary": 0, "base": 0, "skipped": 0}
    maxW = float(max(W, H))
    for ci, comp in enumerate(comps):
        area = comp["area"]
        cm = comp["mask"]
        ys, xs = np.nonzero(cm)
        if len(ys) == 0:
            continue
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        color = np.median(smooth[ys, xs], axis=0)
        used_base = False
        # wide skeleton strokes: exact region coverage with few human-like strokes
        if area >= p.base_min_area and len([s for s in strokes if s["kind"] == "base"]) < 4000:
            geom = {"bbox": (x0, y0, x1, y1), "mask": cm[y0:y1, x0:x1]}
            struct = {
                "kind": "region", "class": "flat", "rid": -1, "ys": ys, "xs": xs,
                "area_px": area, "orientation": 0.0, "color": color, "alpha": 1.0,
                "bbox_px": (x0, y0, x1, y1), "geom": geom,
            }
            policy = {"preset": "basic_round_hard", "width_scale": 1.0,
                      "length_scale": 1.0, "alpha_scale": 1.0}
            base = structpaint._fill_strokes_for_region(struct, smooth, max(H, W), policy)
            for st in base:
                pts_px = np.asarray(st["points_px"], dtype=np.float64)
                if len(pts_px) < 2:
                    continue
                mid = pts_px[len(pts_px) // 2]
                miy = int(np.clip(round(mid[1]), 0, H - 1))
                mix = int(np.clip(round(mid[0]), 0, W - 1))
                c = np.clip(smooth[miy, mix], 0.0, 1.0)
                # width=2*DT reconstructs the region; alpha 1 keeps the colour solid
                # structpaint's flat pass uses width_mult=0.86; undo it so the
                # disc-union actually reconstructs the region (no white gaps)
                widths_px = np.asarray(st["widths_px"], dtype=np.float64).copy() / 0.86
                if len(widths_px) < len(pts_px):
                    widths_px = np.interp(np.linspace(0, 1, len(pts_px)),
                                          np.linspace(0, 1, len(widths_px)), widths_px)
                strokes.append({
                    "kind": "base", "layer": 0, "closed": False,
                    "points": pts_px / maxW,
                    "widths": widths_px / maxW,
                    "color": c, "alpha": float(p.base_alpha),
                    "length": float(np.linalg.norm(np.diff(pts_px, axis=0), axis=1).sum()),
                })
            used_base = True
            stats["base"] = int(stats.get("base", 0)) + len(base)
        # hatch colour masses (texture / small regions)
        if not used_base and area >= p.min_area and len(strokes) < p.max_hatch_strokes:
            width = float(np.clip(
                p.width_min + (p.width_max - p.width_min) * math.sqrt(area / p.width_area_ref),
                p.width_min, p.width_max))
            spacing = max(2.2, width * p.spacing_mul)
            ang = _component_orientation(cm, jxx, jyy, jxy)
            # split very large components into strips to let orientation differ
            if area > 22000:
                n_strips = int(min(4, max(2, round(math.sqrt(area) / 120.0))))
                step = max(1, (y1 - y0) // n_strips)
                for sy in range(y0, y1, step):
                    ey = min(y1, sy + step)
                    sub = cm[sy:ey, x0:x1]
                    if int(sub.sum()) < p.min_area * 0.5:
                        continue
                    # local tensor orientation for this strip
                    m2 = np.zeros_like(cm)
                    m2[sy:ey, x0:x1] = sub
                    sub_ang = _component_orientation(m2, jxx, jyy, jxy)
                    out: list[dict] = []
                    _scanline_strokes(cm, (x0, sy, x1, ey), sub_ang, width, spacing,
                                      color, p, rng, out)
                    for st in out:
                        strokes.append(_norm_stroke(st, maxW))
                    stats["hatch"] += len(out)
            else:
                out = []
                _scanline_strokes(cm, (x0, y0, x1, y1), ang, width, spacing, color, p, rng, out)
                for st in out:
                    strokes.append(_norm_stroke(st, maxW))
                stats["hatch"] += len(out)
        # outlines
        if p.boundary_layers and area >= p.min_area and stats["boundary"] < p.max_boundary_strokes:
            bw = p.boundary_width if area >= p.min_area * 2 else p.detail_boundary_width
            for loop in trace_boundary_loops(cm):
                pts = _resample_loop(loop, 32)
                if len(pts) == 0:
                    continue
                arclen = float(np.linalg.norm(np.diff(np.vstack([pts, pts[:1]]), axis=0), axis=1).sum())
                if arclen < p.min_boundary_len:
                    continue
                bc = _boundary_color(rgb, pts, p.boundary_dark)
                strokes.append({
                    "kind": "boundary", "layer": 3, "closed": True,
                    "points": pts / maxW,
                    "widths": np.full(len(pts), bw / maxW),
                    "color": bc, "alpha": 1.0, "length": arclen,
                })
                stats["boundary"] += 1
        else:
            stats["skipped"] += 1
    stats["stroke_count"] = len(strokes)
    stats["hatch"] = sum(1 for s in strokes if s["kind"] == "hatch")
    stats["boundary"] = sum(1 for s in strokes if s["kind"] == "boundary")
    return strokes, stats


def _norm_stroke(st: dict, maxW: float) -> dict:
    st = dict(st)
    st["points"] = np.asarray(st["points"], dtype=np.float64) / maxW
    st["widths"] = np.asarray(st["widths"], dtype=np.float64) / maxW
    return st


def _render_strokes(strokes: list[dict], H: int, W: int, ss: int = 2) -> np.ndarray:
    canvas = np.ones((H * ss, W * ss, 3), dtype=np.float32)
    # All stroke producers normalize BOTH axes by the long side L=max(H,W);
    # scale both axes by the same L.  The old W/H split is only correct when
    # the canvas is square (it vertically squashed wide/tall inputs).
    L_render = float(max(H, W))
    ordered = sorted(strokes, key=lambda s: int(s.get("layer", 0)))
    for st in ordered:
        pts_n = np.asarray(st["points"], dtype=np.float64)
        if len(pts_n) == 0:
            continue
        pts = np.stack([pts_n[:, 0] * L_render * ss, pts_n[:, 1] * L_render * ss], axis=1)
        widths = np.asarray(st["widths"], dtype=np.float64) * max(W, H) * ss
        # Color model: constant RGB, or one smooth linear gradient between two
        # endpoint colors (parameterized by arc length, not per control point).
        # Stored as {"gradient":{"start":[r,g,b],"end":[...]}} or color_start/
        # color_end; old constant strokes keep "color" and behave unchanged.
        grad = st.get("gradient") or st.get("color_gradient")
        c0 = c1 = None
        if grad is not None:
            c0 = np.asarray(grad["start"], dtype=np.float32)
            c1 = np.asarray(grad["end"], dtype=np.float32)
        elif "color_start" in st and "color_end" in st:
            c0 = np.asarray(st["color_start"], dtype=np.float32)
            c1 = np.asarray(st["color_end"], dtype=np.float32)
        col = None
        if c0 is None:
            col = np.asarray(st["color"], dtype=np.float32)
        alpha = float(st.get("alpha", 1.0))
        closed = bool(st.get("closed", False))
        if closed:
            pts = np.vstack([pts, pts[:1]])
            widths = np.concatenate([widths, widths[:1]])
        if len(pts) == 1:
            r = max(0.5, float(widths[0]) * 0.5)
            x0 = int(max(0, math.floor(pts[0, 0] - r - 2)))
            x1 = int(min(W * ss, math.ceil(pts[0, 0] + r + 3)))
            y0 = int(max(0, math.floor(pts[0, 1] - r - 2)))
            y1 = int(min(H * ss, math.ceil(pts[0, 1] + r + 3)))
            if x1 > x0 and y1 > y0:
                xg = np.arange(x0, x1)[None, :] + 0.5
                yg = np.arange(y0, y1)[:, None] + 0.5
                cov = np.clip(r + 0.5 - np.hypot(xg - pts[0, 0], yg - pts[0, 1]), 0, 1) * alpha
                sl = canvas[y0:y1, x0:x1]
                c = col if col is not None else c0
                sl[:] = sl * (1 - cov[..., None]) + c * cov[..., None]
            continue
        x0 = int(max(0, math.floor(pts[:, 0].min() - widths.max() - 2)))
        x1 = int(min(W * ss, math.ceil(pts[:, 0].max() + widths.max() + 3)))
        y0 = int(max(0, math.floor(pts[:, 1].min() - widths.max() - 2)))
        y1 = int(min(H * ss, math.ceil(pts[:, 1].max() + widths.max() + 3)))
        if x1 <= x0 or y1 <= y0:
            continue
        xs = np.arange(x0, x1, dtype=np.float64)[None, :] + 0.5
        ys = np.arange(y0, y1, dtype=np.float64)[:, None] + 0.5
        best_d = np.full((y1 - y0, x1 - x0), 1e9, dtype=np.float64)
        best_t = np.zeros_like(best_d)
        best_i = np.zeros((y1 - y0, x1 - x0), dtype=np.int32)
        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i + 1]
            vx, vy = bx - ax, by - ay
            l2 = vx * vx + vy * vy
            if l2 <= 1e-12:
                continue
            t = ((xs - ax) * vx + (ys - ay) * vy) / l2
            t = np.clip(t, 0.0, 1.0)
            d = np.hypot(xs - (ax + t * vx), ys - (ay + t * vy))
            upd = d < best_d
            best_d = np.where(upd, d, best_d)
            best_t = np.where(upd, t, best_t)
            best_i = np.where(upd, i, best_i)
        hw = np.zeros_like(best_d)
        for i in range(len(pts) - 1):
            m = best_i == i
            if m.any():
                hw = np.where(m, widths[i] * (1.0 - best_t) + widths[i + 1] * best_t, hw)
        cov = np.clip((hw * 0.5 + 0.5) - best_d, 0.0, 1.0) * alpha
        sl = canvas[y0:y1, x0:x1]
        a = cov[..., None]
        if c0 is not None:
            seg_len = np.hypot(*(pts[1:] - pts[:-1]).T).astype(np.float64)
            cum = np.concatenate([[0.0], np.cumsum(seg_len)])
            total = max(float(cum[-1]), 1e-9)
            safe_len = np.maximum(seg_len, 1e-9)
            s_global = cum[best_i] + best_t * safe_len[best_i]
            u = np.clip(s_global / total, 0.0, 1.0)[..., None]
            col_img = c0[None, None, :] * (1.0 - u) + c1[None, None, :] * u
            sl[:] = sl * (1.0 - a) + col_img * a
        else:
            sl[:] = sl * (1.0 - a) + col * a
    if ss > 1:
        canvas = canvas.reshape(H, ss, W, ss, 3).mean(axis=(1, 3))
    return np.clip(canvas, 0.0, 1.0)


def _psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a - b) ** 2))
    return 10.0 * math.log10(1.0 / max(mse, 1e-12))


def _ssim(a: np.ndarray, b: np.ndarray) -> float:
    ag, bg = a.mean(axis=2), b.mean(axis=2)
    mu_a = gaussian_blur(ag.astype(np.float32), 1.5)
    mu_b = gaussian_blur(bg.astype(np.float32), 1.5)
    va = gaussian_blur((ag * ag).astype(np.float32), 1.5) - mu_a ** 2
    vb = gaussian_blur((bg * bg).astype(np.float32), 1.5) - mu_b ** 2
    cov = gaussian_blur((ag * bg).astype(np.float32), 1.5) - mu_a * mu_b
    c1, c2 = 0.01 ** 2, 0.03 ** 2
    return float((((2 * mu_a * mu_b + c1) * (2 * cov + c2)) /
                  ((mu_a ** 2 + mu_b ** 2 + c1) * (va + vb + c2))).mean())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--work-res", type=int, default=768)
    ap.add_argument("--k-colors", type=int, default=16)
    ap.add_argument("--pre-blur", type=float, default=2.0)
    ap.add_argument("--max-hatch", type=int, default=8000)
    ap.add_argument("--max-boundary", type=int, default=3000)
    ap.add_argument("--width-max", type=float, default=12.0)
    ap.add_argument("--spacing-mul", type=float, default=0.78)
    ap.add_argument("--min-area", type=int, default=260)
    ap.add_argument("--seed", type=int, default=20261021)
    ap.add_argument("--no-boundary", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    p = Params(work_res=args.work_res, k_colors=args.k_colors, pre_blur=args.pre_blur,
               max_hatch_strokes=args.max_hatch, max_boundary_strokes=args.max_boundary,
               width_max=args.width_max, spacing_mul=args.spacing_mul,
               min_area=args.min_area, seed=args.seed)
    if args.no_boundary:
        p.boundary_layers = False
    t0 = time.time()
    rgb = load_image(args.input, int(args.work_res))
    H, W = rgb.shape[:2]
    rng = np.random.default_rng(int(args.seed))
    strokes, stats = generate(rgb, p, rng)
    t_gen = time.time() - t0
    t1 = time.time()
    rec = _render_strokes(strokes, H, W, ss=2)
    t_render = time.time() - t1
    psnr = _psnr(rgb, rec)
    ssim = _ssim(rgb, rec)
    out_png = os.path.join(args.outdir, "hatch_R%d.png" % W)
    Image.fromarray(np.clip(rec * 255, 0, 255).astype(np.uint8)).save(out_png)
    report = {"params": asdict(p), "stats": stats, "psnr": psnr, "ssim": ssim,
              "gen_s": t_gen, "render_s": t_render, "size": [W, H]}
    with open(os.path.join(args.outdir, "hatch_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print(json.dumps({k: report[k] for k in ("psnr", "ssim", "gen_s", "render_s")}, ensure_ascii=False))
    print(json.dumps(stats, ensure_ascii=False)[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
