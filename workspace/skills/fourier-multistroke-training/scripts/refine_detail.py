#!/usr/bin/env python
"""Generic global fine-detail refinement for any raster input.

No image-specific/local training: the same passes run on every image.
Targets the failure modes where similar-colour detail collapses into a flat
mass and bright areas lose their rings:
  * orientation streamlines from the structure tensor (local stroke direction)
  * fine low-contrast edge lines (coloured, not black-only)
  * fine hatching for small low-contrast colour regions
  * concentric halo arcs around bright blobs
  * dense fine curves inside dark masses
All additions are ordinary Fourier strokes; no fills.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont
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

import hybrid_gen as hg  # noqa: E402
import hatch_gen as hgr  # noqa: E402
from fbs_core.decompose import clean_skeleton, skeleton_graph, skeletonize  # noqa: E402


def mk(points, color, width, layer, kind, alpha=0.95):
    pts = np.asarray(points, dtype=np.float64)
    if len(pts) < 2:
        return None
    return {
        "points": pts,  # normalized
        "widths": np.full(len(pts), float(width)),
        "color": np.clip(np.asarray(color, dtype=np.float64), 0, 1),
        "alpha": float(alpha),
        "layer": int(layer),
        "kind": kind,
        "closed": False,
    }


def smooth_polyline(pts, iters=2):
    """Remove orientation-field zig-zag (leaf tips) before Fourier fitting."""
    p = np.asarray(pts, dtype=np.float64)
    if len(p) < 4:
        return p
    for _ in range(max(1, int(iters))):
        p[1:-1] = (p[:-2] + 2.0 * p[1:-1] + p[2:]) / 4.0
    return p


def streamline(rgb, fg, jxx, jyy, jxy, x0, y0, step=1.8, max_len=60.0, min_disp=10.0):
    H, W = fg.shape
    theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy) + math.pi / 2.0
    coh = np.sqrt((jxx - jyy) ** 2 + 4 * jxy ** 2) / np.maximum(jxx + jyy, 1e-9)
    pts = []

    def sample(img, x, y):
        xi, yi = int(round(x)), int(round(y))
        if xi < 0 or yi < 0 or xi >= W or yi >= H:
            return None
        return float(img[yi, xi])

    for direction in (1.0, -1.0):
        x, y = float(x0), float(y0)
        seg = []
        for _ in range(int(max_len / step) + 1):
            xi, yi = int(round(x)), int(round(y))
            if xi < 0 or yi < 0 or xi >= W or yi >= H:
                break
            if not fg[yi, xi]:
                break
            th = sample(theta, x, y)
            c = sample(coh, x, y)
            if th is None or c is None or c < 0.025:
                break
            seg.append((x, y))
            x += direction * step * math.cos(th)
            y += direction * step * math.sin(th)
        if direction > 0:
            pts = seg[::-1] + pts
        else:
            pts = pts + seg
    if len(pts) < 5:
        return None
    pts = np.asarray(pts, dtype=np.float64)
    if np.linalg.norm(pts[-1] - pts[0]) < float(min_disp):
        return None
    # light simplification + smoothing: Fourier fitting must not inherit the
    # integrator's 1-pixel zig-zag at leaf/flower tips.
    if len(pts) > 36:
        idx = np.linspace(0, len(pts) - 1, 36).astype(np.int64)
        pts = pts[idx]
    return smooth_polyline(pts, 3)


def orientation_streamlines(rgb, fg, rng, max_strokes=4200, rs=1.0):
    H, W = fg.shape
    jxx, jyy, jxy = hg.orientation_field(rgb, sigma_t=1.6 * rs, sigma_g=0.8 * rs)
    out = []
    step = 2.2 * rs
    step_seed = max(2, int(round(11 * rs)))
    margin = max(4, int(round(6 * rs)))
    ys = np.arange(margin, H - margin, step_seed)
    xs = np.arange(margin, W - margin, step_seed)
    seeds = [(int(x), int(y)) for y in ys for x in xs if fg[y, x]]
    order = rng.permutation(len(seeds))
    for k in order:
        x, y = seeds[int(k)]
        pts = streamline(rgb, fg, jxx, jyy, jxy, x, y, step=step,
                         max_len=60.0 * rs, min_disp=10.0 * rs)
        if pts is None:
            continue
        xi = np.clip(pts[:, 0].astype(int), 0, W - 1)
        yi = np.clip(pts[:, 1].astype(int), 0, H - 1)
        col = np.median(rgb[yi, xi], axis=0)
        w = float(rng.uniform(0.9, 1.7)) * rs / max(H, W)
        st = mk(pts / max(H, W), col, w, 9, "oil_streamline", alpha=0.92)
        if st is not None:
            out.append(st)
        if len(out) >= max_strokes:
            break
    return out


def fine_edges(rgb, fg, rng, max_strokes=3200, percentile=82.0, rs=1.0):
    H, W = fg.shape
    gray = rgb @ np.array([0.2126, 0.7152, 0.0722])
    gs = hg.gaussian_blur(gray.astype(np.float32), 0.75 * rs).astype(np.float64)
    gy, gx = np.gradient(gs)
    mag = np.hypot(gx, gy)
    vals = mag[fg]
    if vals.size == 0:
        return []
    thr = float(np.percentile(vals, percentile))
    edge = fg & (mag >= thr)
    edge = ndi.binary_opening(edge, iterations=max(1, int(round(rs))))
    edge = ndi.binary_closing(edge, iterations=max(1, int(round(rs))))
    pad = np.pad(edge, 1)
    sk = clean_skeleton(skeletonize(pad, max(3, int(round(3 * rs)))))[1:-1, 1:-1]
    branches = skeleton_graph(sk)
    out = []
    min_len = max(4.0, 6.0 * rs)
    max_len = 140.0 * rs
    for path in branches:
        if len(path) < max(4, int(round(4 * rs))):
            continue
        pts = np.asarray([(x, y) for (y, x) in path], dtype=np.float64)
        L = float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())
        if L < min_len or L > max_len:
            continue
        if len(pts) > 40:
            idx = np.linspace(0, len(pts) - 1, 40).astype(np.int64)
            pts = pts[idx]
        pts = smooth_polyline(pts, 2)
        xi = np.clip(pts[:, 0].astype(int), 0, W - 1)
        yi = np.clip(pts[:, 1].astype(int), 0, H - 1)
        col = np.median(rgb[yi, xi], axis=0)
        w = float(rng.uniform(0.75, 1.35)) * rs / max(H, W)
        st = mk(pts / max(H, W), col, w, 10, "oil_edge", alpha=0.95)
        if st is not None:
            out.append(st)
        if len(out) >= max_strokes:
            break
    return out


def fine_hatch(rgb, fg, rng, max_strokes=3600, spacing=1.7, width=2.0, rs=1.0):
    H, W = fg.shape
    K = 64
    spacing = float(spacing) * rs
    width = float(width) * rs
    sm = hg.gaussian_blur(rgb.astype(np.float32), 1.0 * rs).astype(np.float64)
    lab, centers, present = hg.quantize(sm, K)
    jxx, jyy, jxy = hg.orientation_field(rgb, sigma_t=1.2, sigma_g=0.7)
    out = []
    for lid in range(K):
        m = (lab == lid) & fg
        if int(m.sum()) < max(24, int(round(24 * rs * rs))):
            continue
        cc, n = ndi.label(m, structure=np.ones((3, 3), dtype=np.int32))
        sizes = np.bincount(cc.ravel())
        for cid in range(1, n + 1):
            area = int(sizes[cid])
            if area < max(24, int(round(24 * rs * rs))) or area > int(700 * rs * rs):
                continue
            cm = cc == cid
            ys, xs = np.nonzero(cm)
            x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
            if x1 - x0 < 3 or y1 - y0 < 3:
                continue
            ang = hg.region_orientation(cm, jxx, jyy, jxy)
            col = np.median(rgb[ys, xs], axis=0)
            got = hg.scanline_hatch(cm, (x0, y0, x1, y1), ang, width, spacing, col,
                                    (rgb, 0.01, 1.0), rng, max_strokes=3)
            L = float(max(H, W))
            for st in got:
                st = dict(st)
                st.update({"kind": "oil_fine_hatch", "layer": 8})
                st["points"] = np.asarray(st["points"], dtype=np.float64) / L
                st["widths"] = np.asarray(st["widths"], dtype=np.float64) / L
                st["widths"] = st["widths"] * float(rng.uniform(0.85, 1.25))
                st["alpha"] = 0.88
                out.append(st)
            if len(out) >= max_strokes:
                return out
    return out


def dark_detail(rgb, fg, rng, max_strokes=4000, step=1.9, max_len=55.0, rs=1.0):
    """Fine curved strokes inside dark masses (cypress, roofs) so they are not flat black."""
    H, W = rgb.shape[:2]
    step = float(step) * rs
    max_len = float(max_len) * rs
    lum = rgb @ np.array([0.2126, 0.7152, 0.0722])
    dark = fg & (lum < 0.34)
    jxx, jyy, jxy = hg.orientation_field(rgb, sigma_t=1.0 * rs, sigma_g=0.7 * rs)
    theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy) + math.pi / 2.0
    coh = np.sqrt((jxx - jyy) ** 2 + 4 * jxy ** 2) / np.maximum(jxx + jyy, 1e-9)
    out = []
    seed_step = max(2, int(round(5 * rs)))
    margin = max(3, int(round(4 * rs)))
    ys = np.arange(margin, H - margin, seed_step)
    xs = np.arange(margin, W - margin, seed_step)
    seeds = [(int(x), int(y)) for y in ys for x in xs if dark[y, x]]
    order = rng.permutation(len(seeds))
    for k in order:
        x0, y0 = seeds[int(k)]
        pts = []
        for direction in (1.0, -1.0):
            x, y = float(x0), float(y0)
            seg = []
            for _ in range(int(max_len / step) + 1):
                xi, yi = int(round(x)), int(round(y))
                if xi < 0 or yi < 0 or xi >= W or yi >= H or not fg[yi, xi]:
                    break
                th = theta[yi, xi]
                c = coh[yi, xi]
                if c < 0.01:
                    break
                seg.append((x, y))
                x += direction * step * math.cos(th)
                y += direction * step * math.sin(th)
            if direction > 0:
                pts = seg[::-1] + pts
            else:
                pts = pts + seg
        if len(pts) < 6:
            continue
        pts = np.asarray(pts, dtype=np.float64)
        if len(pts) > 32:
            idx = np.linspace(0, len(pts) - 1, 32).astype(np.int64)
            pts = pts[idx]
        pts = smooth_polyline(pts, 2)
        xi = np.clip(pts[:, 0].astype(int), 0, W - 1)
        yi = np.clip(pts[:, 1].astype(int), 0, H - 1)
        col = np.median(rgb[yi, xi], axis=0)
        w = float(rng.uniform(0.65, 1.15)) * rs / max(H, W)
        st = mk(pts / max(H, W), col, w, 11, "oil_dark_detail", alpha=0.95)
        if st is not None:
            out.append(st)
        if len(out) >= max_strokes:
            break
    return out


def texture_detail(rgb, fg, rng, max_strokes=9000, step=1.45, max_len=30.0, rs=1.0):
    """Low-chroma-difference texture pass.

    Seed-head fibres / impasto ridges may differ from their surroundings by only
    a few RGB levels, so plain edge/percentile masks miss them.  Use high-pass
    local texture energy (not colour difference) to find them, then trace very
    short, smoothed curves along the local orientation.
    """
    H, W = fg.shape
    step = float(step) * rs
    max_len = float(max_len) * rs
    gray = rgb @ np.array([0.2126, 0.7152, 0.0722])
    low = hg.gaussian_blur(gray.astype(np.float32), 3.0 * rs).astype(np.float64)
    hp = gray - low
    energy = hg.gaussian_blur((hp * hp).astype(np.float32), 2.0 * rs).astype(np.float64)
    energy[~fg] = 0.0
    vals = energy[fg]
    if vals.size == 0:
        return []
    thr = float(np.percentile(vals, 58.0))
    mask = fg & (energy >= thr)
    # orientation of the high-pass texture
    gy, gx = np.gradient(hg.gaussian_blur(hp.astype(np.float32), 0.8 * rs).astype(np.float64))
    jxx = hg.gaussian_blur((gx * gx).astype(np.float32), 1.2 * rs).astype(np.float64)
    jyy = hg.gaussian_blur((gy * gy).astype(np.float32), 1.2 * rs).astype(np.float64)
    jxy = hg.gaussian_blur((gx * gy).astype(np.float32), 1.2 * rs).astype(np.float64)
    seed_step = max(2, int(round(3 * rs)))
    margin = max(3, int(round(4 * rs)))
    ys = np.arange(margin, H - margin, seed_step)
    xs = np.arange(margin, W - margin, seed_step)
    seeds = [(int(x), int(y)) for y in ys for x in xs if mask[y, x]]
    order = rng.permutation(len(seeds))
    out = []
    for k in order:
        x, y = seeds[int(k)]
        pts = streamline(rgb, fg, jxx, jyy, jxy, x, y, step=step,
                         max_len=max_len, min_disp=4.0 * rs)
        if pts is None or len(pts) < 4:
            continue
        xi = np.clip(pts[:, 0].astype(int), 0, W - 1)
        yi = np.clip(pts[:, 1].astype(int), 0, H - 1)
        # per-point colour median, then a small contrast lift for impasto fibres
        col = np.median(rgb[yi, xi], axis=0)
        w = float(rng.uniform(0.55, 1.0)) * rs / max(H, W)
        st = mk(pts / max(H, W), col, w, 15, "oil_texture", alpha=0.88)
        if st is not None:
            out.append(st)
        if len(out) >= max_strokes:
            break
    return out


def soften_coarse_in_dark(base, rgb):
    """Reduce coarse base coverage in dark/village masses so fine strokes read."""
    H, W = rgb.shape[:2]
    lum = rgb @ np.array([0.2126, 0.7152, 0.0722])
    n = 0
    for s in base:
        pts = np.asarray(s["points"])
        cen = pts.mean(axis=0)
        xi = int(np.clip(cen[0] * W, 0, W - 1))
        yi = int(np.clip(cen[1] * H, 0, H - 1))
        l = float(lum[yi, xi])
        if l < 0.30 or cen[1] > 0.72:
            s["alpha"] = float(s.get("alpha", 1.0)) * (0.62 if l < 0.30 else 0.78)
            s["widths"] = np.asarray(s["widths"], dtype=np.float64) * (0.72 if l < 0.30 else 0.86)
            n += 1
    return n


def halo_arcs(rgb, fg, rng, max_rings=420, max_radius=90, rs=1.0):
    max_radius = float(max_radius) * rs
    H, W = fg.shape
    lum = rgb @ np.array([0.2126, 0.7152, 0.0722])
    bright = fg & (lum > 0.66)
    cc, n = ndi.label(bright, structure=np.ones((3, 3), dtype=np.int32))
    sizes = np.bincount(cc.ravel())
    blobs = []
    for cid in range(1, n + 1):
        a = int(sizes[cid])
        if a < max(8, int(round(8 * rs * rs))) or a > int(12000 * rs * rs):
            continue
        ys, xs = np.nonzero(cc == cid)
        bx0, bx1, by0, by1 = xs.min(), xs.max(), ys.min(), ys.max()
        bw, bh = bx1 - bx0 + 1, by1 - by0 + 1
        if bw < max(4, int(round(4 * rs))) or bh < max(4, int(round(4 * rs))) \
                or bw > 2 * max_radius or bh > 2 * max_radius:
            continue
        circ = min(bw, bh) / max(bw, bh)
        if circ < 0.45:
            continue
        blobs.append((a, bx0, bx1, by0, by1, xs.mean(), ys.mean()))
    blobs.sort(key=lambda t: -t[0])
    out = []
    for a, bx0, bx1, by0, by1, cx, cy in blobs[:90]:
        rmax = 0.5 * max(bx1 - bx0, by1 - by0)
        if rmax < max(3, 3 * rs):
            continue
        for ring in range(3):
            r = rmax * (0.75 + 0.18 * ring)
            if r > max_radius:
                break
            npts = max(18, int(2 * math.pi * r / (2.0 * rs)))
            ang = np.linspace(0, 2 * math.pi, npts, endpoint=False)
            pts = np.stack([cx + r * np.cos(ang), cy + r * np.sin(ang)], axis=1)
            xi = np.clip(pts[:, 0].astype(int), 0, W - 1)
            yi = np.clip(pts[:, 1].astype(int), 0, H - 1)
            inside = fg[yi, xi].mean()
            if inside < 0.55:
                continue
            col = np.median(rgb[yi, xi], axis=0)
            w = float(rng.uniform(0.7, 1.2)) * rs / max(H, W)
            st = mk(pts / max(H, W), col, w, 9, "oil_halo", alpha=0.9)
            if st is not None:
                out.append(st)
            if len(out) >= max_rings:
                return out
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strokes", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--work-res", type=int, default=1280)
    ap.add_argument("--seed", type=int, default=20261021)
    ap.add_argument("--streamlines", type=int, default=0, help="0 = auto-scale by megapixels")
    ap.add_argument("--edges", type=int, default=0)
    ap.add_argument("--hatch", type=int, default=0)
    ap.add_argument("--halos", type=int, default=0)
    ap.add_argument("--dark", type=int, default=0)
    ap.add_argument("--texture", type=int, default=0, help="low-chroma texture strokes (0 = auto)")
    ap.add_argument("--scale", type=float, default=0.0, help="override megapixel scale (1.0 = 1280^2)")
    ap.add_argument("--res-scale", type=float, default=0.0,
                    help="pixel-size scale for widths/steps; 0 = auto: max(H,W)/1280")
    ap.add_argument("--soften-dark", action="store_true")
    ap.add_argument("--skip-streamlines", action="store_true")
    ap.add_argument("--skip-edges", action="store_true")
    ap.add_argument("--skip-hatch", action="store_true")
    ap.add_argument("--skip-halos", action="store_true")
    ap.add_argument("--skip-dark", action="store_true")
    ap.add_argument("--skip-texture", action="store_true")
    ap.add_argument("--max-total", type=int, default=90000)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    t0 = time.time()
    rng = np.random.default_rng(args.seed)
    rgb = hg.load_image(args.input, args.work_res)
    H, W = rgb.shape[:2]
    fg = ~np.all(rgb > 0.945, axis=2)
    base = json.load(open(args.strokes, encoding="utf-8"))
    for s in base:
        s["points"] = np.asarray(s["points"], dtype=np.float64)
        s["widths"] = np.asarray(s["widths"], dtype=np.float64)
        s["color"] = np.asarray(s["color"], dtype=np.float64)
    scale = float(args.scale) if args.scale > 0 else (H * W) / (1280.0 * 1280.0)
    # Pixel-space scale: at 1280 a detail line is ~1.5px; at 5000 the same
    # visual weight is ~5.9px.  Without this, high-res detail becomes hairline
    # and cannot cover the underlying colour blocks.
    rs = (float(args.res_scale) if args.res_scale > 0
          else float(max(H, W)) / 1280.0)
    # Counts must not explode past the 100k curve cap; cap each stage by area
    # scale, then let --max-total enforce the final budget.
    n_stream = args.streamlines or int(round(5200 * scale))
    n_edges = args.edges or int(round(4600 * scale))
    n_hatch = args.hatch or int(round(6000 * scale))
    n_halos = args.halos or int(round(420 * scale))
    n_dark = args.dark or int(round(4200 * scale))
    n_texture = args.texture or int(round(9000 * scale))
    print(f"[detail] mpscale={scale:.3f} res_scale={rs:.3f} streamlines={n_stream} "
          f"edges={n_edges} hatch={n_hatch} halos={n_halos} dark={n_dark} "
          f"texture={n_texture}", flush=True)
    add = []
    if not args.skip_streamlines:
        add += orientation_streamlines(rgb, fg, rng, max_strokes=n_stream, rs=rs)
    if not args.skip_edges:
        add += fine_edges(rgb, fg, rng, max_strokes=n_edges, rs=rs)
    if not args.skip_hatch:
        add += fine_hatch(rgb, fg, rng, max_strokes=n_hatch, rs=rs)
    if not args.skip_halos:
        add += halo_arcs(rgb, fg, rng, max_rings=n_halos, rs=rs)
    if not args.skip_dark:
        add += dark_detail(rgb, fg, rng, max_strokes=n_dark, rs=rs)
    if not args.skip_texture:
        add += texture_detail(rgb, fg, rng, max_strokes=n_texture, rs=rs)
    if args.max_total > 0 and len(base) + len(add) > args.max_total:
        add = add[: max(0, args.max_total - len(base))]
    softened = 0
    if args.soften_dark:
        softened = soften_coarse_in_dark(base, rgb)
    strokes = base + add
    strokes.sort(key=lambda s: int(s.get("layer", 0)))
    ren = hgr._render_strokes(strokes, H, W, ss=1)
    psnr = hgr._psnr(rgb, ren)
    ssim = hgr._ssim(rgb, ren)
    out_png = os.path.join(args.outdir, f"detail_R{args.work_res}.png")
    Image.fromarray((np.clip(ren, 0, 1) * 255).astype(np.uint8)).save(out_png)
    # side-by-side sheet
    ref = (np.clip(rgb, 0, 1) * 255).astype(np.uint8)
    sheet = np.ones((H, W * 2 + 8, 3), dtype=np.uint8) * 255
    sheet[:, :W] = ref
    sheet[:, W + 8:] = (np.clip(ren, 0, 1) * 255).astype(np.uint8)
    sheet_img = Image.fromarray(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", max(16, W // 50))
    except Exception:
        font = ImageFont.load_default()
    ImageDraw.Draw(sheet_img).text((10, 8), f"detail pass: base {len(base)} + add {len(add)} = {len(strokes)}",
                                  fill=(20, 20, 20), font=font)
    sheet_png = os.path.join(args.outdir, f"detail_compare_R{args.work_res}.png")
    sheet_img.save(sheet_png)
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
    json.dump(payload, open(os.path.join(args.outdir, "detail_strokes.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    report = {
        "input": os.path.abspath(args.input), "work_res": args.work_res,
        "base_strokes": len(base), "softened": softened,
        "added": {k: sum(1 for s in add if s["kind"] == k) for k in
                  ("oil_streamline", "oil_edge", "oil_fine_hatch", "oil_halo", "oil_dark_detail", "oil_texture")},
        "total_strokes": len(strokes), "psnr": round(psnr, 4), "ssim": round(ssim, 5),
        "png": os.path.abspath(out_png), "compare_png": os.path.abspath(sheet_png),
        "seconds": round(time.time() - t0, 1),
    }
    json.dump(report, open(os.path.join(args.outdir, "detail_report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
