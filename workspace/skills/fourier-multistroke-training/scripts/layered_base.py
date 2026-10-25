#!/usr/bin/env python
"""Human-like layered underpainting: full coverage only once, then local fixes.

Oil painters tone/cover the whole canvas in the first layer.  Later layers are
painted only where the picture still needs correction (shadow shapes, accents,
high-detail areas), not another full-canvas coat.

This script:
  1. lays one coarse, opaque, local-flow pass over the whole foreground;
  2. renders it, measures the error against the reference;
  3. adds a mid-size pass only in the high-error regions;
  4. adds a fine pass only in the remaining high-error / high-detail regions;
  5. optionally merges reference detail lines (never base/fix/translucent).

Upper-pass gradients (``--upper-gradient``) are colour-interpolated per stroke.
A naive endpoint sample can straddle a colour boundary and render the stroke as
a smeared wedge/blob instead of paint.  The *gradient safety gate* (on by
default, ``--gradient-gate off`` reproduces the old behaviour) rejects such
strokes and falls back to one flat local median colour:

  * sampling point outside the foreground mask (white canvas) -> rejected_oob;
  * endpoint colour difference > ``--gradient-max-delta``   -> rejected_delta;
  * adjacent colour step > ``--gradient-edge-delta``, or the stroke path crosses
    a high local-gradient pixel (> percentile ``--gradient-edge-pct``, off by
    default)                                               -> rejected_edge.

Definitions (all colours are 0..1 RGB samples of the pass colour source):
  endpoint delta  = max_c |src(start)_c - src(end)_c|       (max-abs channel);
                    the gate also checks the jittered endpoints that are actually
                    stored, so no saved gradient exceeds --gradient-max-delta
  max step        = max over adjacent path samples of max_c |c_i - c_{i+1}|
  edge pixel      = fg & (maximum_filter(|grad(gray)|, 5) > percentile(grad[fg], pct))
  path samples    = 9 evenly spaced nearest-pixel samples start..end
The gate is evaluated per stroke and reported in ``layered_base_report.json``
(``gradient_strokes``, ``gradient_rejected_*``, ``gradient_mean_delta``,
``gradient_max_delta``, ``gradient_mean_step``, ``gradient_max_step``,
``stray_color_px``, ...).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

import numpy as np
from scipy import ndimage as ndi
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

DROP_DETAIL_KINDS = {"base", "fix", "soft_atmosphere", "soft_glaze"}
# A detail JSON may itself be a layered_base output whose first passes are
# stored as ``base_pass1/2/3``.  Those must never be merged back as detail or
# the underpainting gets painted twice, so match them by prefix as well (the
# old exact-match check silently let ``base_pass3`` through).
DROP_DETAIL_PREFIXES = ("base_pass", "base_toned", "fix_", "soft_")

# Number of evenly spaced samples along a candidate gradient stroke used by the
# safety gate (foreground validity, Local edge crossing, colour-profile step).
_GRADIENT_PATH_SAMPLES = 9
_LUMA = np.array([0.2126, 0.7152, 0.0722])
# 4 GiB delivery contract: max_curves <= 100000 (see SKILL.md).
_MAX_STROKES = 100000


def _kind_is_dropped(kind, skip=DROP_DETAIL_KINDS, skip_prefixes=DROP_DETAIL_PREFIXES) -> bool:
    k = str(kind)
    if k in skip:
        return True
    return bool(skip_prefixes) and k.startswith(tuple(skip_prefixes))


def load_strokes(path, skip=None, skip_prefixes=None):
    """Load strokes, dropping old base/fix/translucent layers by kind.

    Defaults drop the exact kinds in ``DROP_DETAIL_KINDS`` plus the
    ``base_pass*`` / ``fix_*`` / ``soft_*`` prefixes.  Pass ``skip=()`` and
    ``skip_prefixes=()`` to load every stroke verbatim.
    """
    skip = DROP_DETAIL_KINDS if skip is None else set(skip)
    skip_prefixes = DROP_DETAIL_PREFIXES if skip_prefixes is None else tuple(skip_prefixes)
    out = []
    for s in json.load(open(path, encoding="utf-8")):
        if _kind_is_dropped(s.get("kind", ""), skip, skip_prefixes):
            continue
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


def local_palette_map(rgb, fg, tile: int, k: int, blur_sigma: float, rng):
    """Per-tile K-colour palette: local colour precision, still flat per stroke."""
    sm = hg.gaussian_blur(rgb.astype(np.float32), float(blur_sigma)).astype(np.float64)
    H, W = fg.shape
    cmap = sm.copy()
    global_map, _ = palette_map(rgb, fg, max(8, int(k) * 3), max(4.0, float(blur_sigma) * 2), rng)
    for y0 in range(0, H, int(tile)):
        for x0 in range(0, W, int(tile)):
            y1 = min(H, y0 + int(tile)); x1 = min(W, x0 + int(tile))
            m = fg[y0:y1, x0:x1]
            if int(m.sum()) < max(8, int(k) * 3):
                sub_out = cmap[y0:y1, x0:x1]
                sub_out[m] = global_map[y0:y1, x0:x1][m]
                cmap[y0:y1, x0:x1] = sub_out
                continue
            sub = sm[y0:y1, x0:x1]
            lab, centers, _ = hg.quantize(sub, min(int(k), max(2, int(m.sum()))))
            centers = np.asarray(centers, dtype=np.float64)
            sub_out = cmap[y0:y1, x0:x1]
            for i in range(len(centers)):
                mm = m & (lab == i)
                sub_out[mm] = centers[i]
            cmap[y0:y1, x0:x1] = sub_out
    return cmap


def palette_map(rgb, fg, k: int, blur_sigma: float, rng):
    """Return a colour map sampled from a K-colour palette, plus the palette."""
    sm = hg.gaussian_blur(rgb.astype(np.float32), float(blur_sigma)).astype(np.float64)
    lab, centers, _ = hg.quantize(sm, int(k))
    centers = np.asarray(centers, dtype=np.float64)
    cmap = np.zeros_like(rgb)
    H, W = fg.shape
    for i in range(int(k)):
        m = lab == i
        if m.any():
            cmap[m] = centers[i]
    return cmap, centers


def _new_gradient_stats() -> dict:
    return {
        "candidates": 0, "accepted": 0,
        "rejected_delta": 0, "rejected_edge": 0,
        "rejected_edge_color": 0, "rejected_edge_grad": 0,
        "rejected_oob": 0,
        "delta_sum": 0.0, "delta_max": 0.0, "accepted_delta_sum": 0.0,
        "step_sum": 0.0, "step_max": 0.0,
    }


def _merge_gradient_stats(total: dict, part: dict) -> None:
    for k, v in part.items():
        if k == "delta_max":
            total[k] = max(float(total.get(k, 0.0)), float(v))
        else:
            total[k] = total.get(k, 0) + v


def _finalize_gradient_stats(gs: dict) -> dict:
    cand = int(gs.get("candidates", 0))
    acc = int(gs.get("accepted", 0))
    rej_d = int(gs.get("rejected_delta", 0))
    rej_e = int(gs.get("rejected_edge", 0))
    rej_e_color = int(gs.get("rejected_edge_color", 0))
    rej_e_grad = int(gs.get("rejected_edge_grad", 0))
    rej_o = int(gs.get("rejected_oob", 0))
    dsum = float(gs.get("delta_sum", 0.0))
    asum = float(gs.get("accepted_delta_sum", 0.0))
    ssum = float(gs.get("step_sum", 0.0))

    def _frac(n):
        return round(n / cand, 5) if cand else 0.0

    return {
        "candidates": cand,
        "accepted": acc,
        "rejected_delta": rej_d,
        "rejected_edge": rej_e,
        "rejected_edge_color": rej_e_color,
        "rejected_edge_grad": rej_e_grad,
        "rejected_oob": rej_o,
        "mean_delta": round(dsum / cand, 5) if cand else 0.0,
        "max_delta": round(float(gs.get("delta_max", 0.0)), 5),
        "mean_step": round(ssum / cand, 5) if cand else 0.0,
        "max_step": round(float(gs.get("step_max", 0.0)), 5),
        "accepted_mean_delta": round(asum / acc, 5) if acc else 0.0,
        "rejected_delta_frac": _frac(rej_d),
        "rejected_edge_frac": _frac(rej_e),
        "rejected_oob_frac": _frac(rej_o),
    }


def _stray_color_metrics(rgb, ren, fg, thr: float = 0.25):
    """Pixels in the foreground whose rendered colour misses the source.

    ``stray_color_px`` = count of foreground pixels with
    ``max_c |ren - rgb| > thr`` (default 0.25).  This catches the wrong-colour
    wedges/smears the gradient gate is meant to remove; it is a full-figure
    metric and should be compared before/after at identical parameters.
    """
    diff = np.abs(ren - rgb)
    stray = int(((diff.max(axis=2) > float(thr)) & fg).sum())
    fg_n = int(fg.sum())
    return stray, (stray / fg_n if fg_n else 0.0)


def texture_flat_pass(rgb, fg, ren, rng, *, count: int, spacing: float,
                      lstd_thr: float, ref_std_thr: float,
                      len_min: float, len_max: float,
                      width_min: float, width_max: float, layer: int,
                      jitter: float, hard_cap: int):
    """Dense fine-texture pass over locally flat areas (v1.34, opt-in).

    The layered underpainting is made of large flat colour dabs; in low-error
    regions later passes never revisit them, so the render keeps "colour band"
    blocks.  This pass renders nothing large: it puts short, thin, source-
    coloured strokes on a jittered grid *only where the current render is
    locally flat* (3x3 luma std < ``lstd_thr``), which breaks the flat blocks
    into fine painterly texture at almost no PSNR cost.

    Seeds are ranked by the reference texture std/energy so a limited stroke
    budget goes to textured-but-flat areas (where banding is visible) instead of
    smooth background.  Row/col placement is on the regular grid (spacing scaled
    by max(H,W)/1280), the per-stroke angle comes from the reference structure
    tensor, and the colour is the reference pixel value plus tiny jitter.
    Stroke size defaults to 5-9 px long x 2.5-3.5 px wide at a 1280 long side
    (scaled by rs) so 16k-19k global strokes still cover a useful share of flat
    areas.

    Returns ``(strokes, info)``; the caller enforces the 100k total cap through
    ``hard_cap``.
    """
    H, W = fg.shape
    rs = float(max(H, W)) / 1280.0
    step = max(1.0, float(spacing) * rs)
    # local flatness of the current render
    gray_ren = ren @ _LUMA
    mu = ndi.uniform_filter(gray_ren, 3)
    m2 = ndi.uniform_filter(gray_ren * gray_ren, 3)
    lstd = np.sqrt(np.maximum(m2 - mu * mu, 0.0))
    flat = fg & (lstd < float(lstd_thr))
    # reference texture energy / local std / orientation
    gray = rgb @ _LUMA
    mu_r = ndi.uniform_filter(gray, 3)
    m2_r = ndi.uniform_filter(gray * gray, 3)
    ref_std = np.sqrt(np.maximum(m2_r - mu_r * mu_r, 0.0))
    low = hg.gaussian_blur(gray.astype(np.float32), 3.0).astype(np.float64)
    hp = gray - low
    energy = hg.gaussian_blur((hp * hp).astype(np.float32), 2.0).astype(np.float64)
    energy[~fg] = 0.0
    gy, gx = np.gradient(gray)
    jxx = ndi.gaussian_filter(gx * gx, 2.2)
    jyy = ndi.gaussian_filter(gy * gy, 2.2)
    jxy = ndi.gaussian_filter(gx * gy, 2.2)
    theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy) + math.pi / 2.0

    auto = int(round(6000.0 * (H * W) / (1536.0 * 1536.0)))
    want = auto if int(count) <= 0 else int(count)
    ys = np.arange(1.0, H - 1.0, step)
    xs = np.arange(1.0, W - 1.0, step)
    X, Y = np.meshgrid(xs, ys)
    X = X.ravel(); Y = Y.ravel()
    ix = np.clip(X.astype(np.int32), 0, W - 1)
    iy = np.clip(Y.astype(np.int32), 0, H - 1)
    ok = flat[iy, ix]
    if float(ref_std_thr) > 0:
        # only paint texture where the *reference* also has local texture;
        # this keeps the budget away from genuinely smooth regions
        ok &= ref_std[iy, ix] >= float(ref_std_thr)
    X = X[ok]; Y = Y[ok]; ix = ix[ok]; iy = iy[ok]
    n = max(0, min(want, len(X), int(hard_cap)))
    info = {
        "requested": int(count), "auto": bool(int(count) <= 0),
        "planned": int(want), "candidates": int(len(X)),
        "budget": int(hard_cap), "emitted": int(n),
        "capped_100k": bool(n < want), "spacing_px": round(float(step), 3),
        "ref_std_thr": float(ref_std_thr),
        "flat_frac_fg": round(float((flat & fg).mean() / max(float(fg.mean()), 1e-9)), 4),
    }
    if n == 0:
        return [], info
    # Budget allocation: rank by reference local texture std (descending) so a
    # limited count is not diluted evenly over the whole canvas (A-region study:
    # uniform sampling gave <4% of the strokes to the banded flower head).
    if float(ref_std_thr) > 0:
        score = ref_std[iy, ix]
    else:
        score = energy[iy, ix]
    jitter_rank = rng.random(len(X)) * 1e-9
    order = np.argsort(-(score + jitter_rank))[:n]
    out = []
    for k in order:
        x = float(X[k]); y = float(Y[k])
        th = float(theta[iy[k], ix[k]] + rng.normal(0.0, 0.25))
        ln = float(rng.uniform(len_min, len_max)) * rs
        wd = float(rng.uniform(width_min, width_max)) * rs
        ca, sa = math.cos(th), math.sin(th)
        pts = np.array([
            [x - ca * ln / 2.0, y - sa * ln / 2.0],
            [x + rng.normal(0, 0.4), y + rng.normal(0, 0.4)],
            [x + ca * ln / 2.0, y + sa * ln / 2.0],
        ])
        pts[:, 0] = np.clip(pts[:, 0], 0, W - 1)
        pts[:, 1] = np.clip(pts[:, 1], 0, H - 1)
        xi = np.clip(pts[:, 0].astype(int), 0, W - 1)
        yi = np.clip(pts[:, 1].astype(int), 0, H - 1)
        col = rgb[yi, xi].mean(axis=0) + rng.normal(0.0, float(jitter), 3)
        out.append({
            "points": pts / float(max(H, W)),
            "widths": np.full(3, wd / float(max(H, W))),
            "color": np.clip(col, 0, 1).tolist(), "alpha": 1.0,
            "layer": int(layer), "kind": "oil_flat_texture", "closed": False,
        })
    return out, info


def tile_pass(rgb, fg, mask, theta, spacing, width_factor, length_factor,
              layer, kind, rng, max_strokes=0, color_map=None,
              color_jitter=0.0, gradient=False, gradient_gate=True,
              gradient_max_delta=0.12, gradient_edge_delta=0.10,
              gradient_edge_map=None, gradient_stats=None,
              flow=False, coh=None, labels=None, region_dirs=None,
              flow_jitter=0.45):
    """Grid-based stroke pass.

    ``flow=False`` is the v1.32-v1.35 scan-grid behaviour (byte-identical for
    legacy recipes).  ``flow=True`` (v1.36) jitters the sampling grid, follows
    the local structure-tensor tangent (region dominant direction +-15 deg when
    coherence is low), makes strokes 1.5-3x longer than wide and bows them a
    little, so the formal layers read as brushwork that follows the form
    instead of an axis-aligned scan.
    """
    H, W = fg.shape
    L = float(max(H, W))
    out = []
    off = spacing * 0.5
    for y in np.arange(off, H, spacing):
        for x in np.arange(off, W, spacing):
            if flow:
                xf = float(x) + float(rng.uniform(-flow_jitter, flow_jitter)) * spacing
                yf = float(y) + float(rng.uniform(-flow_jitter, flow_jitter)) * spacing
            else:
                xf, yf = float(x), float(y)
            ix, iy = int(xf), int(yf)
            if ix < 0 or iy < 0 or ix >= W or iy >= H:
                continue
            if not fg[iy, ix] or not mask[iy, ix]:
                continue
            if flow:
                c = float(coh[iy, ix]) if coh is not None else 1.0
                if c >= 0.06:
                    th = float(theta[iy, ix] + rng.normal(0.0, 0.10))
                elif labels is not None and region_dirs is not None:
                    rd = float(region_dirs[labels[iy, ix]])
                    # never fall back to a horizontal/vertical default: if the
                    # region's dominant direction is axis-aligned, rotate it
                    # away by 12-22 degrees (still a plausible form direction)
                    half_pi = 0.5 * math.pi
                    rr = ((rd % half_pi) + half_pi) % half_pi
                    d_axis = min(rr, half_pi - rr)
                    if d_axis < math.radians(12.0):
                        rd = rd + (1.0 if rng.random() < 0.5 else -1.0) * \
                            math.radians(float(rng.uniform(12.0, 22.0)))
                    th = float(rd + rng.uniform(-0.22, 0.22))
                else:
                    th = float(theta[iy, ix] + rng.normal(0.0, 0.5))
                wpx = float(rng.uniform(width_factor * 0.85, width_factor * 1.15) * spacing)
                ln = float(wpx * rng.uniform(1.5, 3.0))
            else:
                th = float(theta[iy, ix] + rng.normal(0.0, 0.12))
                ln = float(rng.uniform(length_factor * 0.8, length_factor * 1.2) * spacing)
                wpx = float(rng.uniform(width_factor * 0.85, width_factor * 1.15) * spacing)
            ca, sa = math.cos(th), math.sin(th)
            if flow:
                bow = float(rng.uniform(-0.12, 0.12)) * ln
                pts = np.array([
                    [xf - ca * ln / 2, yf - sa * ln / 2],
                    [xf - sa * bow + rng.normal(0, 0.6), yf + ca * bow + rng.normal(0, 0.6)],
                    [xf + ca * ln / 2, yf + sa * ln / 2],
                ])
            else:
                pts = np.array([
                    [xf - ca * ln / 2, yf - sa * ln / 2],
                    [xf + rng.normal(0, 1.0), yf + rng.normal(0, 1.0)],
                    [xf + ca * ln / 2, yf + sa * ln / 2],
                ])
            pts[:, 0] = np.clip(pts[:, 0], 0, W - 1)
            pts[:, 1] = np.clip(pts[:, 1], 0, H - 1)
            xi = np.clip(pts[:, 0].astype(int), 0, W - 1)
            yi = np.clip(pts[:, 1].astype(int), 0, H - 1)
            src = rgb if color_map is None else color_map
            col = np.median(src[yi, xi], axis=0)
            if color_jitter > 0:
                col = col + rng.normal(0.0, float(color_jitter), 3)
            stroke = {
                "points": pts / L, "widths": np.full(3, wpx / L),
                "color": np.clip(col, 0, 1).tolist(), "alpha": 1.0,
                "layer": layer, "kind": kind, "closed": False,
            }
            if gradient and len(pts) >= 2:
                # Two jittered endpoint colours form the gradient (old path).
                # Both jitter draws are consumed in every branch so that
                # --gradient-gate on/off changes only the rejected strokes and
                # keeps all later geometry identical (fair A/B).
                j0 = rng.normal(0.0, float(color_jitter), 3)
                j1 = rng.normal(0.0, float(color_jitter), 3)
                raw0 = np.asarray(src[yi[0], xi[0]], dtype=np.float64)
                rawm = np.asarray(src[yi[1], xi[1]], dtype=np.float64)
                raw1 = np.asarray(src[yi[-1], xi[-1]], dtype=np.float64)
                d_end = float(np.max(np.abs(raw0 - raw1)))
                # Jittered endpoint delta: the value that actually ends up in
                # the saved gradient, capped by the same --gradient-max-delta so
                # no output stroke can exceed the threshold either way.
                d_end_jit = float(np.max(np.abs((raw0 + j0) - (raw1 + j1))))
                gs = gradient_stats
                if gs is not None and not gs:
                    gs.update(_new_gradient_stats())
                if gs is not None:
                    gs["candidates"] += 1
                    gs["delta_sum"] += d_end
                    if d_end > gs["delta_max"]:
                        gs["delta_max"] = d_end
                accept = True
                reject = ""
                if gradient_gate:
                    # Sample the stroke axis at _GRADIENT_PATH_SAMPLES points
                    # (one shared precomputed edge map, no per-stroke filters):
                    #   fg_ok    -- every sample still on the painted subject;
                    #   max_step -- largest colour step between adjacent samples,
                    #               i.e. does the stroke cross a *sharp* boundary;
                    #   edge_hit -- a sample sits on a high-gradient pixel.
                    ax, ay = float(pts[0, 0]), float(pts[0, 1])
                    bx, by = float(pts[2, 0]), float(pts[2, 1])
                    fg_ok = True
                    edge_hit = False
                    prev = None
                    max_step = 0.0
                    for k in range(_GRADIENT_PATH_SAMPLES):
                        t = k / (_GRADIENT_PATH_SAMPLES - 1.0)
                        sx = int(ax + t * (bx - ax))
                        sy = int(ay + t * (by - ay))
                        sx = 0 if sx < 0 else (W - 1 if sx >= W else sx)
                        sy = 0 if sy < 0 else (H - 1 if sy >= H else sy)
                        if not fg[sy, sx]:
                            fg_ok = False
                            continue
                        if gradient_edge_map is not None and gradient_edge_map[sy, sx]:
                            edge_hit = True
                        c = src[sy, sx]
                        if prev is not None:
                            st = float(np.max(np.abs(c - prev)))
                            if st > max_step:
                                max_step = st
                        prev = c
                    if gs is not None:
                        gs["step_sum"] += max_step
                        if max_step > gs["step_max"]:
                            gs["step_max"] = max_step
                    if not fg_ok:
                        accept, reject = False, "oob"
                    elif gradient_max_delta > 0 and (d_end > gradient_max_delta
                                                     or d_end_jit > gradient_max_delta):
                        accept, reject = False, "delta"
                    elif gradient_edge_delta > 0 and max_step > gradient_edge_delta:
                        accept, reject = False, "edge_color"
                    elif gradient_edge_map is not None and edge_hit:
                        accept, reject = False, "edge_grad"
                if accept:
                    stroke["gradient"] = {
                        "start": np.clip(raw0 + j0, 0, 1).tolist(),
                        "end": np.clip(raw1 + j1, 0, 1).tolist(),
                    }
                    stroke["color"] = np.clip((raw0 + j0 + raw1 + j1) * 0.5, 0, 1).tolist()
                    if gs is not None:
                        gs["accepted"] += 1
                        gs["accepted_delta_sum"] += d_end
                else:
                    # Cross-boundary / ambiguous stroke: one flat local colour
                    # (mean of the valid samples) instead of a colour smear.
                    # The mean stays close to the old gradient midpoint, so
                    # gating removes the interpolation itself and keeps later
                    # error masks close to the ungated run.
                    cols = np.stack([raw0, rawm, raw1])
                    valid = np.array([bool(fg[yi[0], xi[0]]), bool(fg[yi[1], xi[1]]),
                                      bool(fg[yi[-1], xi[-1]])])
                    if valid.any():
                        flat = cols[valid].mean(axis=0)
                    else:
                        flat = rawm
                    stroke["color"] = np.clip(flat + 0.5 * (j0 + j1), 0, 1).tolist()
                    if gs is not None:
                        if reject == "edge_color":
                            gs["rejected_edge_color"] += 1
                            gs["rejected_edge"] += 1
                        elif reject == "edge_grad":
                            gs["rejected_edge_grad"] += 1
                            gs["rejected_edge"] += 1
                        else:
                            gs["rejected_" + reject] += 1
            out.append(stroke)
            if max_strokes and len(out) >= max_strokes:
                return out
    return out


def error_mask(rgb, ren, fg, percentile, grass=None):
    err = np.abs(ren - rgb).mean(axis=2)
    err = hg.gaussian_blur(err.astype(np.float32), 3.0).astype(np.float64)
    if grass is not None:
        err = err * (0.65 + 0.35 * np.clip(grass / max(float(np.percentile(grass[fg], 90)), 1e-6), 0, 1))
    err[~fg] = 0
    thr = float(np.percentile(err[fg], float(percentile)))
    mask = fg & (err >= max(thr, 1e-6))
    return ndi.binary_dilation(mask, iterations=2), err


def _parse_base_color(spec, rgb):
    """Parse ``--base-color``: auto | #RRGGBB | "r,g,b" (0..1 or 0..255).

    ``auto`` is the median colour of the WHOLE image (not the foreground): a
    line-art sheet is mostly white paper, so the human-like tone is near white;
    a painting keeps its overall dominant tone.
    """
    s = str(spec).strip()
    if (not s) or s.lower() == "auto":
        return np.median(rgb.reshape(-1, 3), axis=0)
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            raise SystemExit(f"--base-color: bad hex colour {spec!r}")
        return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float64) / 255.0
    parts = [p for p in s.replace(";", ",").split(",") if p.strip() != ""]
    try:
        vals = np.array([float(p) for p in parts], dtype=np.float64)
    except ValueError:
        raise SystemExit(f"--base-color: cannot parse {spec!r} (auto|#RRGGBB|r,g,b)")
    if vals.size != 3:
        raise SystemExit(f"--base-color: need 3 components, got {spec!r}")
    if vals.max() > 1.0:
        vals = vals / 255.0
    return np.clip(vals, 0.0, 1.0)


def base_coat_pass(fg, angle_deg, spacing, width_factor, jitter, color, rng):
    """Human-like single base coat: one flat-colour pass, each area once.

    A painter tones the canvas with a single thin, flat colour coat -- a few
    long parallel sweep strokes, no colour sampling from the picture, no
    gradients, no per-stroke colour variation, no overlapping patchwork.

    Row bands are placed so that the *first and last* band still cover the
    foreground boundary rows (``perpendicular_min .. perpendicular_max``), and
    the pitch is kept at ``spacing`` with width ``width_factor * spacing``:
    every fg pixel is assigned to its nearest band and every band is painted by
    exactly one long sweep stroke -- one coat, no double painting except the
    intended ``width - spacing`` overlap (<= 5%).  The jitter moves the sweep
    midpoint *along* the stroke only, so it can never open a gap between rows.
    Coverage/overlap in the report come from a real raster recount (see
    ``_base_raster_metrics``), not from these geometric values.

    Returns ``(strokes, info)``.
    """
    H, W = fg.shape
    L = float(max(H, W))
    sp = max(2.0, float(spacing))
    width = max(1.0, float(width_factor) * sp)
    half = 0.5 * width
    a = math.radians(float(angle_deg))
    ca, sa = math.cos(a), math.sin(a)
    ys, xs = np.nonzero(fg)
    # The renderer works in pixel-centre coordinates (pixel i centre = i+0.5),
    # so the coat geometry must use the same origin; otherwise the last row /
    # column of the subject can stay uncovered by half a pixel.
    X = xs.astype(np.float64) + 0.5
    Y = ys.astype(np.float64) + 0.5
    info = {"coverage": None, "overlap_frac": None, "strokes": 0,
            "spacing": round(sp, 4), "width": round(width, 4),
            "angle": float(angle_deg), "color": None,
            "overlap_analytic": round(max(0.0, (width - sp) / sp), 5),
            "coverage_analytic": round(min(1.0, width / sp), 5)}
    if len(xs) == 0:
        return [], info
    col = np.clip(np.asarray(color, dtype=np.float64), 0.0, 1.0)
    cx, cy = float(X.mean()), float(Y.mean())
    dx = X - cx
    dy = Y - cy
    perp = -sa * dx + ca * dy
    along = ca * dx + sa * dy
    p0, p1 = float(perp.min()), float(perp.max())

    def _build_centers(half_):
        # Half-pixel safety: the renderer's coverage ramps over one pixel, so a
        # row whose centreline sits exactly `half` away from the boundary pixel
        # gives it alpha 0.5 (counted as uncovered); pull first/last row inward.
        edge = 0.5
        if p1 - p0 <= 2.0 * half_ - 2.0 * edge:
            return np.array([0.5 * (p0 + p1)], dtype=np.float64)
        cs = [p0 + half_ - edge]
        c = cs[0]
        while c + half_ - edge < p1 - 1e-9:
            c += sp
            cs.append(c)
        return np.asarray(cs, dtype=np.float64)

    def _predicted_overlap(centers_, half_):
        """Raster overlap fraction predicted from where fg pixels sit against
        the row boundaries (no rendering needed; used only to pick a safe
        width factor).  The renderer's coverage ramps over one pixel, so the
        raster overlap band is ``2*half + 1 - spacing`` wide."""
        if len(centers_) < 2:
            return 0.0
        over = max(0.0, 2.0 * half_ + 1.0 - sp)
        if over <= 0.0:
            return 0.0
        bounds = centers_[:-1] + 0.5 * sp
        dist = np.abs(perp[:, None] - bounds[None, :]).min(axis=1)
        return float((dist < 0.5 * over).mean())

    # Adaptive width factor: keep the requested width unless the foreground
    # happens to put much of its area exactly on the row-overlap bands (sparse
    # line-art like fg) -- then shave the overlap so the real-raster gate
    # (overlap <= 5% of fg) still holds; coverage stays 1.0 because the pitch
    # never changes.
    eff_factor = float(width_factor)
    half = 0.5 * eff_factor * sp
    centers = _build_centers(half)
    pred_ovl = _predicted_overlap(centers, half)
    if pred_ovl > 0.045:
        for cand in (1.03, 1.02, 1.01, 1.0):
            if cand >= eff_factor:
                continue
            h2 = 0.5 * cand * sp
            cs2 = _build_centers(h2)
            p2 = _predicted_overlap(cs2, h2)
            eff_factor, half, centers, pred_ovl = cand, h2, cs2, p2
            if p2 <= 0.045:
                break
    width = 2.0 * half
    info.update({
        "width": round(width, 4),
        "width_factor_effective": round(eff_factor, 4),
        "overlap_predicted": round(pred_ovl, 4),
        "coverage_analytic": round(min(1.0, width / sp), 5),
        "overlap_analytic": round(max(0.0, (width - sp) / sp), 5),
    })
    nb = len(centers)
    band = (np.rint((perp - centers[0]) / sp).astype(np.int32)
            if nb > 1 else np.zeros(len(xs), dtype=np.int32))
    band = np.clip(band, 0, nb - 1)
    order = np.lexsort((along, band))
    bs = band[order]
    als = along[order]
    out = []
    i = 0
    n = len(bs)
    jj = max(0.0, float(jitter))
    while i < n:
        b = int(bs[i])
        j = i + 1
        while j < n and int(bs[j]) == b:
            j += 1
        vals = als[i:j]
        a0 = float(vals[0]) - half
        a1 = float(vals[-1]) + half
        am = 0.5 * (a0 + a1) + (rng.normal(0.0, jj) if jj > 0 else 0.0)
        off = float(centers[b])

        def _xy(av):
            return [cx + (-sa) * off + ca * av, cy + ca * off + sa * av]

        pts = np.array([_xy(a0), _xy(am), _xy(a1)], dtype=np.float64)
        pts[:, 0] = np.clip(pts[:, 0], 0.5, W - 0.5)
        pts[:, 1] = np.clip(pts[:, 1], 0.5, H - 0.5)
        out.append({
            "points": pts / L,
            "widths": np.full(3, width / L),
            "color": np.round(col, 6).tolist(),
            "alpha": 1.0,
            "layer": 0,
            "kind": "base_toned",
            "closed": False,
        })
        i = j
    info["strokes"] = len(out)
    return out, info


def _base_coverage_counts(strokes, H, W):
    """Per-pixel hit counts of base strokes using the real renderer (magic colour).

    Each stroke is rendered on its own small bounding-box canvas (identical
    renderer geometry, translated + re-normalised) so the full-canvas cost is
    O(total stroke area) instead of O(n_strokes * H * W).
    """
    counts = np.zeros((H, W), dtype=np.uint16)
    L = float(max(H, W))
    for s in strokes:
        pts = np.asarray(s["points"], dtype=np.float64) * L
        wds = np.asarray(s["widths"], dtype=np.float64) * L
        hw = float(wds.max()) * 0.5 + 2.0
        x0 = int(max(0, math.floor(float(pts[:, 0].min()) - hw)))
        x1 = int(min(W, math.ceil(float(pts[:, 0].max()) + hw) + 1))
        y0 = int(max(0, math.floor(float(pts[:, 1].min()) - hw)))
        y1 = int(min(H, math.ceil(float(pts[:, 1].max()) + hw) + 1))
        h = y1 - y0
        w = x1 - x0
        if h <= 0 or w <= 0:
            continue
        Ls = float(max(h, w, 1))
        probe = {
            "points": (pts - np.array([x0, y0], dtype=np.float64)) / Ls,
            "widths": np.clip(wds, 0.5, None) / Ls,
            "color": [1.0, 0.0, 1.0],
            "alpha": 1.0,
            "layer": 0,
            "closed": False,
        }
        rendered = hgr._render_strokes([probe], h, w, ss=1)
        counts[y0:y1, x0:x1] += (rendered[:, :, 1] < 0.5).astype(np.uint16)
    return counts


def _add_stroke_footprint(counts, stroke, H, W):
    """Add one stroke's raster footprint (bbox-local render) to a count image."""
    L = float(max(H, W))
    pts = np.asarray(stroke["points"], dtype=np.float64) * L
    wds = np.asarray(stroke["widths"], dtype=np.float64) * L
    hw = float(wds.max()) * 0.5 + 2.0
    x0 = int(max(0, math.floor(float(pts[:, 0].min()) - hw)))
    x1 = int(min(W, math.ceil(float(pts[:, 0].max()) + hw) + 1))
    y0 = int(max(0, math.floor(float(pts[:, 1].min()) - hw)))
    y1 = int(min(H, math.ceil(float(pts[:, 1].max()) + hw) + 1))
    h = y1 - y0
    w = x1 - x0
    if h <= 0 or w <= 0:
        return
    Ls = float(max(h, w, 1))
    probe = {
        "points": (pts - np.array([x0, y0], dtype=np.float64)) / Ls,
        "widths": np.clip(wds, 0.5, None) / Ls,
        "color": [1.0, 0.0, 1.0], "alpha": 1.0, "layer": 0, "closed": False,
    }
    rendered = hgr._render_strokes([probe], h, w, ss=1)
    counts[y0:y1, x0:x1] += (rendered[:, :, 1] < 0.5).astype(np.uint16)


def _metrics_from_counts(counts, fg, H, W):
    n_fg = max(int(fg.sum()), 1)
    coverage = float(int(((counts > 0) & fg).sum()) / n_fg)
    overlap = float(int(((counts > 1) & fg).sum()) / n_fg)
    over2 = int(((counts > 2) & fg).sum())
    canvas_frac = float(int((counts > 1).sum()) / max(H * W, 1))
    return (round(coverage, 5), round(overlap, 5), over2,
            int(counts.max()), round(canvas_frac, 6))


def _base_coverage_counts_scaled(strokes, H, W, max_side=512):
    """Fast base-coat hit counts on a downsampled canvas.

    Wide wash sweeps have bboxes close to the full canvas, so rendering them
    one by one with the full renderer is O(n * H * W).  Here the strokes are
    scaled to ``max_side`` and rasterised with PIL lines + round caps, which
    keeps the coverage/overlap statistics objective but cheap (< 1s at 1536).
    Returns ``(counts, scale)``.
    """
    from PIL import Image, ImageDraw
    s = min(1.0, float(max_side) / float(max(H, W)))
    h = max(1, int(round(H * s)))
    w = max(1, int(round(W * s)))
    L = float(max(H, W))
    counts = np.zeros((h, w), dtype=np.uint16)
    for st in strokes:
        pts = np.asarray(st["points"], dtype=np.float64) * L * s
        wds = np.asarray(st["widths"], dtype=np.float64) * L * s
        if len(pts) < 2:
            continue
        n = len(pts)
        if len(wds) < n:
            wds = np.full(n, float(np.mean(wds)))
        im = Image.new("L", (w, h), 0)
        dr = ImageDraw.Draw(im)
        # variable-width polyline: one segment per control-point pair (width
        # interpolated) plus a round cap at every control point
        for i in range(n - 1):
            wseg = max(1, int(math.floor(0.5 * (float(wds[i]) + float(wds[i + 1])))))
            dr.line([(float(pts[i, 0]), float(pts[i, 1])),
                     (float(pts[i + 1, 0]), float(pts[i + 1, 1]))],
                    fill=255, width=wseg)
        for i in range(n):
            r = 0.5 * max(1.0, float(math.floor(float(wds[i]))))
            cx_, cy_ = float(pts[i, 0]), float(pts[i, 1])
            dr.ellipse([cx_ - r, cy_ - r, cx_ + r, cy_ + r], fill=255)
        counts += (np.asarray(im) > 0).astype(np.uint16)
    return counts, float(s)


def _downsample_fg(fg, s):
    from PIL import Image
    h = max(1, int(round(fg.shape[0] * s)))
    w = max(1, int(round(fg.shape[1] * s)))
    im = Image.fromarray((fg.astype(np.uint8)) * 255)
    im = im.resize((w, h), Image.BOX)
    return np.asarray(im) > 127


def _base_metrics_auto(fg, strokes, H, W):
    """Base-coat coverage/overlap.

    * ``<=384px``: exact renderer raster (matches the skill renderer exactly,
      used by the small-canvas test assertions);
    * ``384..768px``: renderer raster downsampled to 512px with PIL;
    * ``>768px``: analytic geometry (coverage = tiled once = 1.0, overlap from
      the painted-area / foreground-area ratio).  Rendering every wide wash
      sweep at 1536 is what used to make the run minutes slow.
    """
    if max(H, W) <= 384:
        counts = _base_coverage_counts(strokes, H, W)
        cov, ovl, ov2, mx, ovc = _metrics_from_counts(counts, fg, H, W)
        return (cov, ovl, ov2, mx, ovc, "raster", 1.0, counts, fg)
    if max(H, W) <= 768:
        counts, s = _base_coverage_counts_scaled(strokes, H, W)
        fgm = _downsample_fg(fg, s)
        cov, ovl, ov2, mx, ovc = _metrics_from_counts(counts, fgm,
                                                      counts.shape[0], counts.shape[1])
        return (cov, ovl, ov2, mx, ovc, "raster@512", s, counts, fgm)
    L = float(max(H, W))
    painted = 0.0
    for st in strokes:
        pts = np.asarray(st["points"], dtype=np.float64) * L
        wds = np.asarray(st["widths"], dtype=np.float64) * L
        if len(pts) < 2:
            continue
        length = float(np.hypot(np.diff(pts[:, 0]), np.diff(pts[:, 1])).sum())
        wmean = float(np.mean(wds))
        painted += length * wmean + math.pi * (0.5 * wmean) ** 2
    fg_area = float(max(int(fg.sum()), 1))
    canvas_area = float(max(H * W, 1))
    ovl = max(0.0, painted / fg_area - 1.0)
    return (1.0, round(ovl, 5), 0, 2, round(painted / canvas_area, 6),
            "analytic", None, None, None)


def _base_fill_gaps_auto(fg, strokes, color, angle_deg, H, W, counts, fgm, s,
                         rounds=3):
    """Fill small uncovered base-coat spots, exact raster or raster@512.

    Returns ``(extra_strokes, updated_counts)``; patches are fitted inside the
    subject (width from the distance transform) so margins stay clean.
    """
    L = float(max(H, W))
    a = math.radians(float(angle_deg))
    ca, sa = math.cos(a), math.sin(a)
    col = np.clip(np.asarray(color, dtype=np.float64), 0.0, 1.0)
    dist = ndi.distance_transform_edt(fg)
    brush = 0.026 * L
    out = []
    for _ in range(int(rounds)):
        gaps = (counts == 0) & fgm
        if not int(gaps.sum()):
            break
        lab, n = ndi.label(gaps)
        new = []
        for cid in range(1, n + 1):
            m = lab == cid
            if int(m.sum()) < 2:
                continue
            yy, xx = np.nonzero(m)
            gx = (float(xx.mean()) + 0.5) / max(s, 1e-9)
            gy = (float(yy.mean()) + 0.5) / max(s, 1e-9)
            rad = (0.5 * math.hypot(xx.max() - xx.min() + 1,
                                    yy.max() - yy.min() + 1) / max(s, 1e-9) + 1.0)
            px = int(np.clip(round(gx), 0, W - 1))
            py = int(np.clip(round(gy), 0, H - 1))
            fit = max(2.0, 2.0 * float(dist[py, px]) - 2.0)
            wpx = min(2.0 * rad, fit, max(2.0, 0.6 * brush))
            pts = np.array([[gx - ca * rad, gy - sa * rad],
                            [gx, gy],
                            [gx + ca * rad, gy + sa * rad]])
            pts[:, 0] = np.clip(pts[:, 0], 0.5, W - 0.5)
            pts[:, 1] = np.clip(pts[:, 1], 0.5, H - 0.5)
            new.append({
                "points": pts / L,
                "widths": np.full(3, max(2.0, wpx) / L),
                "color": np.round(col, 6).tolist(),
                "alpha": 1.0, "layer": 0, "kind": "base_toned",
                "closed": False,
            })
            if len(new) >= 200:
                break
        if not new:
            break
        if s >= 1.0:
            for st in new:
                _add_stroke_footprint(counts, st, H, W)
        else:
            c2, _ = _base_coverage_counts_scaled(new, H, W)
            if c2.shape == counts.shape:
                counts += c2
        out.extend(new)
    return out, counts


def _base_gap_patches(fg, strokes, color, angle_deg, H, W, max_iter=3,
                      counts=None):
    """Patch only the small uncovered spots of the ground (never repaint the
    whole canvas).  ``counts`` may be a precomputed hit-count image; it is then
    updated incrementally so the raster cost stays near one full pass."""
    L = float(max(H, W))
    a = math.radians(float(angle_deg))
    ca, sa = math.cos(a), math.sin(a)
    col = np.clip(np.asarray(color, dtype=np.float64), 0.0, 1.0)
    dist = ndi.distance_transform_edt(fg)
    extra = []
    if counts is None:
        counts = _base_coverage_counts(strokes, H, W)
    for _ in range(int(max_iter)):
        gaps = (counts == 0) & fg
        if not int(gaps.sum()):
            break
        lab, n = ndi.label(gaps)
        added = 0
        for cid in range(1, n + 1):
            m = lab == cid
            npx = int(m.sum())
            if npx < 2:
                continue
            yy, xx = np.nonzero(m)
            gx = float(xx.mean()) + 0.5
            gy = float(yy.mean()) + 0.5
            rad = max(2.0, 0.5 * math.hypot(xx.max() - xx.min() + 1,
                                             yy.max() - yy.min() + 1) + 1.0)
            pts = np.array([[gx - ca * rad, gy - sa * rad],
                            [gx, gy],
                            [gx + ca * rad, gy + sa * rad]])
            pts[:, 0] = np.clip(pts[:, 0], 0.5, W - 0.5)
            pts[:, 1] = np.clip(pts[:, 1], 0.5, H - 0.5)
            pwx = int(np.clip(round(gx), 0, W - 1))
            pwy = int(np.clip(round(gy), 0, H - 1))
            fit = max(2.0, 2.0 * float(dist[pwy, pwx]) - 2.0)
            extra.append({
                "points": pts / L,
                "widths": np.full(3, min(2.0 * rad, fit, 0.5 * 0.085 * L) / L),
                "color": np.round(col, 6).tolist(),
                "alpha": 1.0, "layer": 0, "kind": "base_toned", "closed": False,
            })
            _add_stroke_footprint(counts, extra[-1], H, W)
            added += 1
            if added >= 200:
                break
        if added == 0:
            break
    return extra


def _base_raster_metrics(fg, strokes, H, W):
    """Real-raster coverage/overlap of the base coat.

    Each base stroke is re-rendered alone with a magic colour ``(1, 0, 1)``
    (green channel 0) exactly like the skill's renderer does for the final
    image; a pixel counts as covered when its green channel drops below 0.5.
    Returns ``(coverage, overlap_frac, over2_px, max_count, canvas_overlap_frac)``
    where coverage and overlap are measured on foreground pixels only.
    """
    if not strokes:
        return None, None, 0, 0, None
    counts = _base_coverage_counts(strokes, H, W)
    n_fg = max(int(fg.sum()), 1)
    coverage = float(int(((counts > 0) & fg).sum()) / n_fg)
    overlap = float(int(((counts > 1) & fg).sum()) / n_fg)
    over2 = int(((counts > 2) & fg).sum())
    canvas_frac = float(int((counts > 1).sum()) / max(H * W, 1))
    return (round(coverage, 5), round(overlap, 5), over2, int(counts.max()),
            round(canvas_frac, 6))


# =============================================================== v1.36 helpers
# Human painting style: wash ground, form-following formal strokes, and a
# region-grouped painting order.  Everything below is only active when the
# corresponding v1.36 option is used; legacy recipes keep the old code paths.

def _hilbert_d(x, y, order):
    """Hilbert curve distance of integer grid point (x, y) with 2**order cells."""
    d = 0
    s = 1 << (order - 1)
    while s > 0:
        rx = 1 if (x & s) else 0
        ry = 1 if (y & s) else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = (1 << order) - 1 - x
                y = (1 << order) - 1 - y
            x, y = y, x
        s >>= 1
    return int(d)


def _region_maps(fg, count, seed=7):
    """Spatial k-means over foreground pixels -> label map, centroids, visit order.

    ``visit_order`` is a greedy nearest-neighbour path over the region
    centroids (starting from the top-left region): a painter finishes one
    region and moves to a neighbouring one instead of rescanning the canvas.
    """
    H, W = fg.shape
    ys, xs = np.nonzero(fg)
    if len(xs) == 0:
        return (np.zeros((H, W), np.int32), np.zeros((0, 2)),
                np.zeros(0, np.int32))
    K = int(max(2, min(int(count), len(xs))))
    pix = np.stack([xs.astype(np.float64) + 0.5,
                    ys.astype(np.float64) + 0.5], axis=1)
    step = max(1, len(pix) // 20000)
    sample = pix[::step]
    rng = np.random.default_rng(int(seed))
    cent = sample[rng.choice(len(sample), K, replace=False)].copy()
    for _ in range(12):
        d = ((sample[:, None, :] - cent[None, :, :]) ** 2).sum(axis=2)
        lab = d.argmin(axis=1)
        for k in range(K):
            m = lab == k
            if m.any():
                cent[k] = sample[m].mean(axis=0)
    labels = np.full((H, W), -1, dtype=np.int32)
    d2 = ((pix[:, None, :] - cent[None, :, :]) ** 2).sum(axis=2)
    labels[ys, xs] = d2.argmin(axis=1).astype(np.int32)
    # Hilbert space-filling order: the painter moves to a neighbouring area but
    # the overall path is NOT a top-to-bottom scan (low y correlation).
    bits = 10
    scale = float((1 << bits) - 1)
    hx = np.clip((cent[:, 0] / max(float(W), 1.0) * scale).astype(np.int64),
                 0, (1 << bits) - 1)
    hy = np.clip((cent[:, 1] / max(float(H), 1.0) * scale).astype(np.int64),
                 0, (1 << bits) - 1)
    hd = np.asarray([_hilbert_d(int(x), int(y), bits) for x, y in zip(hx, hy)])
    order = np.argsort(hd, kind="stable")
    return labels, cent, np.asarray(order, dtype=np.int32)


def _region_directions(theta, coh, labels, K):
    """Dominant stroke direction per region (circular mean of tensor tangents)."""
    dirs = np.zeros(K)
    for k in range(K):
        m = labels == k
        if not m.any():
            continue
        t = theta[m]
        w = np.ones_like(t) if coh is None else np.clip(coh[m], 1e-6, None)
        dirs[k] = 0.5 * math.atan2(float((w * np.sin(2 * t)).sum()),
                                   float((w * np.cos(2 * t)).sum()))
    return dirs


def _stroke_dir_angle(stroke):
    pts = np.asarray(stroke["points"], dtype=np.float64)
    if len(pts) < 2:
        return None
    dx = float(pts[-1, 0] - pts[0, 0])
    dy = float(pts[-1, 1] - pts[0, 1])
    if abs(dx) + abs(dy) < 1e-9:
        return None
    return math.atan2(dy, dx)


def _angle_diff(a, b):
    d = abs((a - b + math.pi) % (2.0 * math.pi) - math.pi)
    return d


def flow_direction_metrics(strokes, theta, H, W, kinds=("base_pass2", "base_pass3",
                                                        "base_pass4", "thick_dab")):
    """Stroke-direction statistics: how many strokes follow the form tangent and
    how many are still axis-aligned (the old scan-grid look)."""
    L = float(max(H, W))
    n = 0
    flow_ok = 0
    axis_ok = 0
    for s in strokes:
        if str(s.get("kind", "")) not in kinds:
            continue
        ang = _stroke_dir_angle(s)
        if ang is None:
            continue
        pts = np.asarray(s["points"], dtype=np.float64) * L
        cx = int(np.clip(round(0.5 * (pts[0, 0] + pts[-1, 0])), 0, W - 1))
        cy = int(np.clip(round(0.5 * (pts[0, 1] + pts[-1, 1])), 0, H - 1))
        t = float(theta[cy, cx])
        n += 1
        if _angle_diff(ang, t) <= math.radians(20.0):
            flow_ok += 1
        ax = abs((ang + math.pi) % math.pi)
        if ax <= math.radians(10.0) or ax >= math.pi - math.radians(10.0) \
                or abs(ax - 0.5 * math.pi) <= math.radians(10.0):
            axis_ok += 1
    if n == 0:
        return {"flow_orientation_frac": None, "axis_aligned_frac": None,
                "direction_strokes": 0}
    return {"flow_orientation_frac": round(flow_ok / n, 4),
            "axis_aligned_frac": round(axis_ok / n, 4),
            "direction_strokes": n}


def _ranks(values):
    order = np.argsort(np.asarray(values, dtype=np.float64))
    ranks = np.empty(len(order), dtype=np.float64)
    ranks[order] = np.arange(len(order), dtype=np.float64)
    return ranks


def paint_order_indices(strokes, labels, region_order, mode, H, W,
                        region_areas=None):
    """Return (order_indices, metrics) for the requested painting order.

    region = region-grouped (region visit path -> layer -> large-to-small);
    scan = old top-to-bottom canvas order; legacy = original generation order.
    """
    L = float(max(H, W))
    n = len(strokes)
    if n == 0:
        return list(range(n)), {}
    if mode == "legacy":
        return list(range(n)), {"paint_order_effective": "legacy",
                                "order_y_spearman": None,
                                "order_adjacent_median_px": None,
                                "order_region_run_frac": None}
    centres = np.zeros((n, 2))
    regions = np.full(n, -1, dtype=np.int32)
    layers = np.zeros(n, dtype=np.int64)
    sizes = np.zeros(n)
    is_base = np.zeros(n, dtype=bool)
    rank_of_region = {int(r): i for i, r in enumerate(region_order)}
    for i, s in enumerate(strokes):
        pts = np.asarray(s["points"], dtype=np.float64) * L
        cx = int(np.clip(round(0.5 * (pts[0, 0] + pts[-1, 0])), 0, W - 1))
        cy = int(np.clip(round(0.5 * (pts[0, 1] + pts[-1, 1])), 0, H - 1))
        centres[i] = (cx, cy)
        regions[i] = labels[cy, cx]
        layers[i] = int(s.get("layer", 0))
        is_base[i] = str(s.get("kind", "")) in ("base_toned", "base_pass1")
        sizes[i] = float(np.mean(s["widths"])) * float(
            np.hypot(pts[-1, 0] - pts[0, 0], pts[-1, 1] - pts[0, 1]))
    if mode == "scan":
        keys = [(layers[i], centres[i, 1], centres[i, 0]) for i in range(n)]
    else:
        # base ground first, then big/most significant regions first (background
        # -> flower heads -> vase/table), so several dispersed areas progress in
        # parallel instead of a single advancing front.
        if region_areas is not None and len(region_areas) == len(region_order):
            # Hilbert spatial path, but inside every 4-region window paint the
            # bigger/more significant area first: multiple dispersed areas are
            # worked in parallel (no single advancing front) while the overall
            # path keeps low y-correlation.
            base = [int(r) for r in region_order]
            win = 4
            for k in range(0, len(base), win):
                base[k:k + win] = sorted(
                    base[k:k + win], key=lambda r: -float(region_areas[r]))
            rank_of_region = {r: i for i, r in enumerate(base)}
        keys = [(0 if is_base[i] else 1 + rank_of_region.get(int(regions[i]),
                                                           10 ** 6),
                 layers[i], -sizes[i]) for i in range(n)]
    idx = sorted(range(n), key=lambda i: keys[i])
    # order metrics: is the sequence a y-scan or a region-grouped walk?
    ys = centres[idx, 1]
    rk = _ranks(ys)
    if len(ys) > 1 and float(np.std(rk)) > 1e-9:
        y_spear = float(np.corrcoef(rk, np.arange(len(rk)))[0, 1])
    else:
        y_spear = 0.0
    diffs = np.hypot(np.diff(centres[idx, 0]), np.diff(centres[idx, 1]))
    same_region = float(np.mean(regions[idx][1:] == regions[idx][:-1])) if n > 1 else 1.0
    return idx, {"paint_order_effective": mode,
                 "order_y_spearman": round(float(y_spear), 4),
                 "order_adjacent_median_px": round(float(np.median(diffs)), 2)
                 if len(diffs) else 0.0,
                 "order_region_run_frac": round(same_region, 4)}


def wash_coat_pass(fg, color, rng, angle_deg=-22.0, width_ratio=0.026,
                   jitter_deg=25.0, bow=0.16, band_overlap=0.10):
    """Human-like wash ground: big diagonal/curved sweeps, one flat colour.

    The ground is brushed in wide bands laid across a main sweep direction
    (default -22 deg, i.e. diagonal).  Inside each band the brush travels along
    the band and may tilt up to +-25 deg; a tilted stroke is kept inside a narrow
    corridor (``length <= 0.06*brush / |sin(tilt)|``) so the band still covers
    once and the neighbouring-band overlap stays modest.  Single colour, no
    gradient, no per-stroke colour variation: a painter's wash, not a scan.
    """
    H, W = fg.shape
    L = float(max(H, W))
    brush = max(8.0, float(width_ratio) * L)
    pitch = brush * (1.0 - float(band_overlap))
    a = math.radians(float(angle_deg))
    ca, sa = math.cos(a), math.sin(a)
    ys, xs = np.nonzero(fg)
    info = {"pattern": "wash", "angle": float(angle_deg),
            "brush_px": round(brush, 2), "bands": 0,
            "coverage": None, "overlap_frac": None,
            "overlap_canvas_frac": None, "coverage_analytic": None,
            "overlap_analytic": None}
    if len(xs) == 0:
        return [], info
    col = np.clip(np.asarray(color, dtype=np.float64), 0.0, 1.0)
    X = xs.astype(np.float64) + 0.5
    Y = ys.astype(np.float64) + 0.5
    cx, cy = float(X.mean()), float(Y.mean())
    perp = -sa * (X - cx) + ca * (Y - cy)
    along = ca * (X - cx) + sa * (Y - cy)
    p0, p1 = float(perp.min()), float(perp.max())
    nbands = int(max(1, math.ceil((p1 - p0) / pitch)))
    real_pitch = (p1 - p0) / nbands
    info["bands"] = nbands
    pitch = brush
    edge = 0.5
    if p1 - p0 <= brush:
        centers = np.array([0.5 * (p0 + p1)])
    else:
        cs = [p0 + 0.5 * brush - edge]
        c = cs[0]
        while c + 0.5 * brush - edge < p1 - 1e-9:
            c += pitch
            cs.append(c)
        if cs[-1] + 0.5 * brush - edge < p1:
            cs.append(cs[-1] + pitch)
        centers = np.asarray(cs, dtype=np.float64)
    info["bands"] = len(centers)
    nrm = np.array([-sa, ca])
    half = 0.5 * brush * 1.02
    out = []
    for b, off in enumerate(centers):
        centre = np.array([cx, cy]) + nrm * float(off)
        sel = np.abs(perp - off) <= max(0.75 * pitch, brush * 0.6)
        if not sel.any():
            continue
        # ONE broad sweep per band: 5 control points (cheap to render), both ends
        # inset by half a brush so a round cap cannot spill onto the white paper.
        a0 = float(along[sel].min()) + half
        a1 = float(along[sel].max()) - half
        if a1 <= a0:
            a0 = float(along[sel].min())
            a1 = float(along[sel].max())
        w_band = brush * 1.02
        ts = np.linspace(0.0, 1.0, 5)
        pts = []
        for k, t in enumerate(ts):
            base = centre + np.array([ca, sa]) * (a0 + (a1 - a0) * t)
            bow = math.sin(math.pi * t) * float(rng.uniform(-0.03, 0.03)) * brush
            wob = float(rng.uniform(-0.10, 0.10))
            pts.append(base + nrm * (bow + wob))
        pts = np.asarray(pts, dtype=np.float64)
        pts[:, 0] = np.clip(pts[:, 0], 0.5, W - 0.5)
        pts[:, 1] = np.clip(pts[:, 1], 0.5, H - 0.5)
        if np.hypot(*(pts[-1] - pts[0])) > brush:
            out.append({
                "points": pts / L,
                "widths": np.full(5, w_band / L),
                "color": np.round(col, 6).tolist(),
                "alpha": 1.0, "layer": 0, "kind": "base_toned", "closed": False,
            })
        # one short tilted accent per other band, well inside the subject
        if b % 2 == 0:
            tt = float(rng.uniform(0.25, 0.75))
            p = centre + np.array([ca, sa]) * (a0 + (a1 - a0) * tt)
            delta = math.radians(float(rng.uniform(-jitter_deg, jitter_deg)))
            ln = brush * float(rng.uniform(0.6, 1.0))
            dd = np.array([math.cos(a + delta), math.sin(a + delta)])
            start = p - dd * (ln * 0.5)
            end = p + dd * (ln * 0.5)
            mid = p + nrm * float(rng.uniform(-0.2, 0.2)) * brush * 0.3
            ap = np.vstack([start, mid, end])
            ap[:, 0] = np.clip(ap[:, 0], 0.5, W - 0.5)
            ap[:, 1] = np.clip(ap[:, 1], 0.5, H - 0.5)
            out.append({
                "points": ap / L,
                "widths": np.full(3, brush * 0.40 / L),
                "color": np.round(col, 6).tolist(),
                "alpha": 1.0, "layer": 0, "kind": "base_toned",
                "closed": False,
            })
    info["strokes"] = len(out)
    return out, info


def thick_dab_pass(rgb, fg, rng, count, theta, coh, labels, region_dirs,
                   color_map, H, W, layer=4):
    """Thick short dabs ("loaded brush" block-in layer, v1.36 demo option)."""
    n = int(max(0, count))
    info = {"count": n, "emitted": 0}
    if n == 0:
        return [], info
    ys_all, xs_all = np.nonzero(fg)
    if len(xs_all) == 0:
        return [], info
    L = float(max(H, W))
    src = rgb if color_map is None else color_map
    scale = L / 1536.0
    out = []
    picks = rng.integers(0, len(xs_all), size=n)
    for p in picks:
        iy = int(ys_all[p]); ix = int(xs_all[p])
        x = float(ix) + 0.5 + float(rng.uniform(-2.0, 2.0)) * scale
        y = float(iy) + 0.5 + float(rng.uniform(-2.0, 2.0)) * scale
        c = float(coh[iy, ix]) if coh is not None else 0.0
        if c >= 0.06:
            th = float(theta[iy, ix] + rng.normal(0.0, 0.15))
        else:
            th = float(region_dirs[labels[iy, ix]] + rng.uniform(-0.3, 0.3)) \
                if labels is not None else float(theta[iy, ix] + rng.normal(0.0, 0.4))
        wd = float(rng.uniform(4.0, 6.0)) * scale
        ln = wd * float(rng.uniform(1.5, 2.0))
        ca, sa = math.cos(th), math.sin(th)
        bow = float(rng.uniform(-0.18, 0.18)) * ln
        pts = np.array([
            [x - ca * ln / 2, y - sa * ln / 2],
            [x - sa * bow, y + ca * bow],
            [x + ca * ln / 2, y + sa * ln / 2],
        ])
        pts[:, 0] = np.clip(pts[:, 0], 0.5, W - 0.5)
        pts[:, 1] = np.clip(pts[:, 1], 0.5, H - 0.5)
        xi = np.clip(pts[:, 0].astype(int), 0, W - 1)
        yi = np.clip(pts[:, 1].astype(int), 0, H - 1)
        col = np.median(src[yi, xi], axis=0) + rng.normal(0.0, 0.012, 3)
        out.append({
            "points": pts / L,
            "widths": np.full(3, wd / L),
            "color": np.clip(col, 0, 1).tolist(),
            "alpha": 1.0, "layer": int(layer), "kind": "thick_dab", "closed": False,
        })
    info["emitted"] = len(out)
    return out, info


def _build_silhouette(fg, shrink_px):
    """Eroded painting silhouette: removes the soft AI halo / antialias fringe.

    ``sil = distance_transform_edt(fg) > shrink_px`` (small/thin features are
    protected by a fallback to the raw foreground if the silhouette shrinks
    below 20% of it).
    """
    dist = ndi.distance_transform_edt(fg)
    sil = dist > float(shrink_px)
    fallback = False
    if int(sil.sum()) < 0.2 * max(int(fg.sum()), 1):
        sil = fg.copy()
        fallback = True
    area_frac = float(int(sil.sum())) / max(int(fg.sum()), 1)
    return sil, area_frac, fallback


def _clip_strokes_to_silhouette(strokes, sil, H, W, margin_px, min_width_px):
    """Generation-time geometry clipping of every non-ground stroke.

    For each stroke: (1) pull the two endpoints back along the stroke until a
    round cap cannot leave the silhouette; (2) clamp every point's width to
    ``2*max(dist_sil - margin, 0)``; (3) drop strokes whose clamped width is
    below ``min_width_px``.  Colours and gradients are untouched: this changes
    stroke geometry at generation time, not the rendered image afterwards.
    """
    L = float(max(H, W))
    dist_sil = ndi.distance_transform_edt(sil.astype(np.uint8))
    m = float(margin_px)
    min_w = float(min_width_px)
    out = []
    clipped = 0
    trimmed = 0
    dropped = 0
    over_before = 0.0
    over_after = -m
    skip = ("base_toned", "base_pass1")
    for s in strokes:
        if str(s.get("kind", "")) in skip:
            out.append(s)
            continue
        pts = np.asarray(s["points"], dtype=np.float64) * L
        wds = np.asarray(s["widths"], dtype=np.float64) * L
        if len(pts) == 0:
            dropped += 1
            continue
        n = len(pts)
        xi = np.clip(pts[:, 0].astype(np.int64), 0, W - 1)
        yi = np.clip(pts[:, 1].astype(np.int64), 0, H - 1)
        d = dist_sil[yi, xi]
        ov = float(np.max(wds * 0.5 - d)) if n else 0.0
        if ov > over_before:
            over_before = ov
        i0, i1 = 0, n - 1
        while i1 - i0 + 1 >= 2:
            need = max(0.5, 0.5 * float(wds[i0]) - 0.5)
            if float(d[i0]) < need:
                i0 += 1
                continue
            break
        while i1 - i0 + 1 >= 2:
            need = max(0.5, 0.5 * float(wds[i1]) - 0.5)
            if float(d[i1]) < need:
                i1 -= 1
                continue
            break
        keep = i1 - i0 + 1
        if keep < 2:
            dropped += 1
            continue
        cut = n - keep
        pts = pts[i0:i1 + 1]
        wds = wds[i0:i1 + 1]
        d = d[i0:i1 + 1]
        new_wds = np.minimum(wds, 2.0 * np.maximum(d - m, 0.0))
        if float(np.max(new_wds)) < min_w:
            dropped += 1
            continue
        changed = cut > 0 or bool(np.any(new_wds < wds - 1e-9))
        if changed:
            clipped += 1
            trimmed += int(cut)
        ov2 = float(np.max(new_wds * 0.5 - d))
        if ov2 > over_after:
            over_after = ov2
        ns = dict(s)
        ns["points"] = pts / L
        ns["widths"] = np.maximum(new_wds, 0.5) / L
        out.append(ns)
    stats = {"clipped": clipped, "trimmed": trimmed, "dropped": dropped,
             "over_before": round(over_before, 3),
             "over_after": round(max(0.0, over_after), 3)}
    return out, stats


def _composite_silhouette(ren, sil, feather_px):
    """Render composite: feather the subject alpha by the silhouette distance.

    This is part of the rendering compositing (the layer the strokes are painted
    onto), not a post-process that edits the finished image.
    """
    m = ndi.distance_transform_edt(~sil.astype(bool))
    feather = max(float(feather_px), 1e-6)
    alpha = np.clip(1.0 - m / feather, 0.0, 1.0)
    return ren * alpha[..., None] + (1.0 - alpha[..., None])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--detail", default=None)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--work-res", type=int, default=1536)
    ap.add_argument("--coarse", type=float, default=24.0)
    ap.add_argument("--mid", type=float, default=12.0)
    ap.add_argument("--fine", type=float, default=6.0)
    ap.add_argument("--mid-err-pct", type=float, default=60.0)
    ap.add_argument("--fine-err-pct", type=float, default=45.0)
    ap.add_argument("--fine-grad-pct", type=float, default=0.0,
                    help="if >0, fine pass only in high-gradient detail regions")
    ap.add_argument("--base-color-mode",
                    choices=("toned", "none", "single", "quantized", "source"),
                    default="toned",
                    help="pass1 ground style: toned = ONE flat colour, single thin "
                         "coat, each area painted once (default, human-like); "
                         "none = no ground at all (line art / white paper); "
                         "single/quantized/source = legacy multi-colour grounds "
                         "(kept for byte-identical old commands, not recommended)")
    ap.add_argument("--base-color", default="auto",
                    help="toned-mode coat colour: auto (whole-image median tone) | "
                         "#RRGGBB | \"r,g,b\" (0..1 or 0..255)")
    ap.add_argument("--base-coat-angle", type=float, default=0.0,
                    help="sweep angle of the single base coat in degrees "
                         "(0 = horizontal rows, default)")
    ap.add_argument("--base-coat-spacing", type=float, default=0.0,
                    help="row pitch of the base coat in px (0 = use --coarse)")
    ap.add_argument("--base-coat-width-factor", type=float, default=1.04,
                    help="coat stroke width = factor x row pitch (default 1.04: "
                         "seamless raster coverage with about 4%% row overlap, <=5%%)")
    ap.add_argument("--base-coat-jitter", type=float, default=0.5,
                    help="midline jitter along the sweep direction in px "
                         "(default 0.5; never opens a row gap)")
    ap.add_argument("--base-coat-pattern", choices=("wash", "rows"), default="wash",
                    help="toned ground geometry: wash = a few big diagonal/curved "
                         "brush sweeps (v1.36 default, non-scan); rows = the old "
                         "horizontal row sweep (v1.35 behaviour, still available)")
    ap.add_argument("--wash-angle", type=float, default=-22.0,
                    help="main sweep direction of the wash ground in degrees "
                         "(default -22 = diagonal)")
    ap.add_argument("--flow-strokes", choices=("on", "off"), default="on",
                    help="v1.36 formal-stroke geometry: jittered (non-axis-aligned) "
                         "samples, form-tangent directions from the structure tensor, "
                         "length = 1.5-3x width (default on; legacy base modes leave "
                         "this off unless explicitly passed, keeping old commands "
                         "byte-identical)")
    ap.add_argument("--paint-order", choices=("region", "scan", "legacy"),
                    default="region",
                    help="stroke painting order: region = finish one area at a time "
                         "along a neighbour path (v1.36 default, human-like); "
                         "scan = old top-to-bottom order; legacy = original generation "
                         "order (legacy base modes auto-use legacy unless set)")
    ap.add_argument("--region-count", type=int, default=12,
                    help="number of spatial regions for the region paint order "
                         "(default 12)")
    ap.add_argument("--thick-dabs", type=int, default=0,
                    help="optional thick loaded-brush dab layer count (default 0; "
                         "demo uses 14000)")
    ap.add_argument("--clip-to-fg", choices=("auto", "on", "off"), default="auto",
                    help="clip the final render to the subject: pixels outside the "
                         "foreground stay white paper (auto = on for the v1.36 "
                         "wash+flow+region modes, never for legacy commands; "
                         "default auto)")
    ap.add_argument("--silhouette", choices=("auto", "on", "off"), default="auto",
                    help="v1.37 silhouette-aware stroke generation: clip stroke "
                         "geometry (endpoints+widths) to the eroded subject "
                         "silhouette at generation time and feather the render "
                         "composite (auto = on for v1.36 wash+flow+region modes "
                         "with a white background; never for legacy commands)")
    ap.add_argument("--silhouette-shrink-px", type=float, default=1.0,
                    help="erode the foreground by this many px to get the drawing "
                         "silhouette (removes the soft halo; default 1.0)")
    ap.add_argument("--silhouette-margin-px", type=float, default=0.25,
                    help="keep stroke width within 2*(distance-to-silhouette-margin) "
                         "(default 0.25)")
    ap.add_argument("--silhouette-min-width-px", type=float, default=0.6,
                    help="drop a stroke when its clamped width max is below this "
                         "(default 0.6)")
    ap.add_argument("--silhouette-feather-px", type=float, default=0.8,
                    help="edge feather of the render composite (default 0.8)")
    ap.add_argument("--base-k", type=int, default=8)
    ap.add_argument("--mid-k", type=int, default=10)
    ap.add_argument("--fine-k", type=int, default=24)
    ap.add_argument("--base-jitter", type=float, default=0.006)
    ap.add_argument("--base-blur", type=float, default=4.0,
                    help="blur sigma of the pass1 colour source for --base-color-mode "
                         "source (default 4.0; larger = softer/flatter ground)")
    ap.add_argument("--base-gradient", action="store_true",
                    help="give pass1 base strokes an endpoint gradient too (default off); "
                         "the same gradient safety gate applies when --gradient-gate on")
    ap.add_argument("--base-width-factor", type=float, default=1.5,
                    help="pass1 base stroke width factor x --coarse (default 1.5 = old "
                         "behaviour)")
    ap.add_argument("--base-length-factor", type=float, default=2.0,
                    help="pass1 base stroke length factor x --coarse (default 2.0 = old "
                         "behaviour)")
    ap.add_argument("--texture-flat", action="store_true",
                    help="v1.34 opt-in: dense fine-texture pass over locally flat areas "
                         "(small reference-coloured strokes that break up colour-band "
                         "blocks; default off = old behaviour)")
    ap.add_argument("--texture-flat-count", type=int, default=8000,
                    help="max texture-flat strokes (0 = auto by canvas area, ~6000 at "
                         "1536; total strokes are hard-capped at 100000)")
    ap.add_argument("--texture-flat-spacing", type=float, default=2.5,
                    help="candidate grid spacing in px at a 1280 long side, scaled by "
                         "max(H,W)/1280 (default 2.5)")
    ap.add_argument("--texture-flat-len-min", type=float, default=5.0,
                    help="min texture stroke length in px at a 1280 long side "
                         "(scaled by max(H,W)/1280; default 5.0)")
    ap.add_argument("--texture-flat-len-max", type=float, default=9.0,
                    help="max texture stroke length in px at a 1280 long side "
                         "(scaled by max(H,W)/1280; default 9.0)")
    ap.add_argument("--texture-flat-width-min", type=float, default=2.5,
                    help="min texture stroke width in px at a 1280 long side "
                         "(scaled by max(H,W)/1280; default 2.5)")
    ap.add_argument("--texture-flat-width-max", type=float, default=3.5,
                    help="max texture stroke width in px at a 1280 long side "
                         "(scaled by max(H,W)/1280; default 3.5)")
    ap.add_argument("--texture-flat-lstd", type=float, default=0.01,
                    help="3x3 local luma std below which a pixel counts as flat "
                         "(default 0.01)")
    ap.add_argument("--texture-flat-ref-std", type=float, default=0.0,
                    help="candidate also needs reference 3x3 local luma std >= this "
                         "(0 = off; recommended 0.02 -- only paint where the source "
                         "really has texture; candidates are ranked by this value)")
    ap.add_argument("--texture-flat-layer", type=int, default=19)
    ap.add_argument("--texture-flat-jitter", type=float, default=0.01,
                    help="per-stroke colour jitter of the texture pass (default 0.01)")
    ap.add_argument("--mid-jitter", type=float, default=0.010)
    ap.add_argument("--fine-jitter", type=float, default=0.015)
    ap.add_argument("--local-color", action="store_true",
                    help="use per-tile local palettes for pass2/3/4")
    ap.add_argument("--upper-color-mode", choices=("palette", "source"), default="palette",
                    help="source = local blurred-source colour + small jitter (finer colour)")
    ap.add_argument("--upper-blur", type=float, default=2.5)
    ap.add_argument("--upper-gradient", action="store_true",
                    help="let pass2/3/4 strokes carry a smooth endpoint gradient")
    ap.add_argument("--gradient-gate", choices=("on", "off"), default="on",
                    help="safety gate for --upper-gradient strokes: reject a gradient "
                         "whose samples leave the foreground or cross a colour boundary, "
                         "and fall back to one flat local colour (default on; off = old "
                         "pre-v1.33 behaviour, useful for A/B)")
    ap.add_argument("--gradient-max-delta", type=float, default=0.12,
                    help="reject a gradient stroke when max_c |start_c-end_c| exceeds this "
                         "(0..1 scale, default 0.12). Checked on both the source colours "
                         "and the jittered colours that are actually saved, so saved "
                         "gradients never exceed it; <=0 disables this sub-gate")
    ap.add_argument("--gradient-edge-delta", type=float, default=0.10,
                    help="reject a gradient stroke when the largest colour step between "
                         "adjacent path samples (9 samples along the stroke) exceeds this "
                         "(max-abs channel, default 0.10; <=0 disables this sub-gate)")
    ap.add_argument("--gradient-edge-pct", type=float, default=100.0,
                    help="reject a gradient stroke whose path samples a high local-gradient "
                         "pixel above this percentile of fg gradient magnitude "
                         "(default 100 = sub-gate disabled; enabled when 0 < pct < 100)")
    ap.add_argument("--pass2-tile", type=int, default=64)
    ap.add_argument("--pass2-k", type=int, default=6)
    ap.add_argument("--pass3-tile", type=int, default=32)
    ap.add_argument("--pass3-k", type=int, default=8)
    ap.add_argument("--pass4", action="store_true",
                    help="extra fine local-colour layer for high-error/detail areas")
    ap.add_argument("--pass4-tile", type=int, default=16)
    ap.add_argument("--pass4-k", type=int, default=8)
    ap.add_argument("--pass4-err-pct", type=float, default=45.0)
    ap.add_argument("--pass4-spacing", type=float, default=4.0)
    ap.add_argument("--max-curves", type=int, default=100000,
                    help="hard cap on the number of curves (4 GiB delivery contract, "
                         "default 100000).  If the generated recipe exceeds it, the "
                         "stroke lists are deterministically thinned (pass2 -> pass3 -> "
                         "pass4 -> texture) and the report records capped_100k / "
                         "cap_adjustments")
    ap.add_argument("--seed", type=int, default=20261103)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    def _stage(msg):
        print(f"[stage] {msg}  t={time.time() - t0:.1f}s", flush=True)

    t0 = time.time()
    rgb = hg.load_image(args.input, args.work_res)
    H, W = rgb.shape[:2]
    fg = ~np.all(rgb > 0.945, axis=2)
    white_frac = float((~fg).mean())  # white paper fraction (v1.37 auto rule)
    jxx, jyy, jxy = hg.orientation_field(rgb)
    theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy) + math.pi / 2.0
    gray = rgb @ np.array([0.2126, 0.7152, 0.0722])
    gs = hg.gaussian_blur(gray.astype(np.float32), 0.8).astype(np.float64)
    gy, gx = np.gradient(gs)
    grad = np.hypot(gx, gy)
    rng = np.random.default_rng(args.seed)

    # --- v1.36 human-paint mode resolution --------------------------------
    # Legacy base modes without the new flags keep the old scan-grid /
    # generation-order behaviour so v1.33/v1.34 commands stay byte-identical.
    _argv = list(sys.argv[1:])

    def _flag_given(name):
        return any(a == name or a.startswith(name + "=") for a in _argv)

    _legacy_base = args.base_color_mode in ("single", "quantized", "source")
    flow_on = (args.flow_strokes == "on") and (not _legacy_base
                                               or _flag_given("--flow-strokes"))
    paint_order_mode = args.paint_order
    if _legacy_base and not _flag_given("--paint-order"):
        paint_order_mode = "legacy"
    coh_map = np.sqrt((jxx - jyy) ** 2 + 4.0 * jxy ** 2) \
        / np.maximum(jxx + jyy, 1e-12)
    labels = cent = region_order = region_dirs = None
    region_count_used = 0
    if flow_on or paint_order_mode == "region" or int(args.thick_dabs) > 0:
        labels, cent, region_order = _region_maps(fg, int(args.region_count))
        region_count_used = int(len(cent))
        if len(cent):
            region_dirs = _region_directions(theta, coh_map, labels, len(cent))
    _stage("region maps / config resolved")
    print(f"[layered] human-paint: flow={'on' if flow_on else 'off'} "
          f"paint_order={paint_order_mode} regions={region_count_used} "
          f"base={args.base_color_mode}/{args.base_coat_pattern}")

    # Gradient safety gate: one shared threshold image (computed once, not per
    # stroke).  ``grad_wide`` is a 5x5 max-filter so a pixel on the stroke band
    # (not only its centreline) can flag a crossing.
    gradient_gate = args.gradient_gate == "on"
    gradient_edge_map = None
    gradient_edge_frac = 0.0
    if gradient_gate and 0.0 < float(args.gradient_edge_pct) < 100.0 and fg.any():
        edge_thr = float(np.percentile(grad[fg], float(args.gradient_edge_pct)))
        grad_wide = ndi.maximum_filter(grad, size=5)
        gradient_edge_map = fg & (grad_wide > edge_thr)
        gradient_edge_frac = round(float(gradient_edge_map.mean()), 5)
    gradient_stats = _new_gradient_stats()
    pass_stats = {}

    def _gradient_kwargs():
        return dict(gradient_gate=gradient_gate,
                    gradient_max_delta=args.gradient_max_delta,
                    gradient_edge_delta=args.gradient_edge_delta,
                    gradient_edge_map=gradient_edge_map)

    # Pass 1: the ground.  v1.35 default is a human-like single flat-colour
    # coat: ONE tone, ONE thin sweep pass, every area touched exactly once.
    # The legacy multi-colour grounds (single/quantized/source) stay available
    # for byte-identical old commands but are not the recommended way to paint.
    base_col = None
    base_info = None
    if args.base_color_mode == "toned":
        base_col = _parse_base_color(args.base_color, rgb)
        base_info = None
        if args.base_coat_pattern == "wash":
            strokes, base_info = wash_coat_pass(fg, base_col, rng,
                                                angle_deg=args.wash_angle)
            (cov, ovl, ov2, mx, ovc, _bmode, _bscale,
             _counts, _fgm) = _base_metrics_auto(fg, strokes, H, W)
            base_info.update({"coverage": cov, "overlap_frac": ovl,
                              "overlap_px_over2": ov2, "max_count": mx,
                              "overlap_canvas_frac": ovc,
                              "spacing": None,
                              "width": base_info.get("brush_px"),
                              "angle": float(args.wash_angle),
                              "metrics_mode": _bmode,
                              "metrics_scale": (None if _bscale is None
                                                 else round(float(_bscale), 4))})
            if cov is not None and cov < 0.99 and _counts is not None:
                # patch only the small uncovered spots (never repaint a second
                # coat); works on both the exact and the downsampled raster
                _patches, _counts = _base_fill_gaps_auto(
                    fg, strokes, base_col, float(args.wash_angle), H, W,
                    _counts, _fgm, _bscale)
                if _patches:
                    strokes = strokes + _patches
                    cov, ovl, ov2, mx, ovc = _metrics_from_counts(
                        _counts, _fgm, _counts.shape[0], _counts.shape[1])
                    base_info.update({"coverage": cov, "overlap_frac": ovl,
                                      "overlap_px_over2": ov2, "max_count": mx,
                                      "overlap_canvas_frac": ovc,
                                      "gap_patches": len(_patches),
                                      "strokes": len(strokes)})
            if cov is not None and cov < 0.90 and _bmode != "analytic":
                # awkward foreground: last-resort row coat, report the fallback
                st2, info2 = base_coat_pass(
                    fg, args.base_coat_angle, max(float(args.coarse), 12.0),
                    max(1.04, float(args.base_coat_width_factor)),
                    args.base_coat_jitter, base_col, rng)
                cov2, ovl2, ov22, mx2, ovc2 = _base_raster_metrics(fg, st2, H, W)
                if cov2 is not None and cov2 >= (cov or 0.0):
                    strokes = st2
                    base_info = {"pattern": "wash(rows-fallback)",
                                 "angle": float(args.base_coat_angle),
                                 "brush_px": None, "bands": None,
                                 "strokes": len(st2), "coverage": cov2,
                                 "overlap_frac": ovl2,
                                 "overlap_canvas_frac": ovc2,
                                 "overlap_px_over2": ov22, "max_count": mx2,
                                 "spacing": None, "width": None}
        else:
            coat_spacing = (float(args.base_coat_spacing)
                            if float(args.base_coat_spacing) > 0 else float(args.coarse))
            requested_wf = float(args.base_coat_width_factor)
            # Real-raster gate for the row pattern: coverage >= 99%, overlap <= 5%.
            best = None
            for wf in (requested_wf, min(1.02, requested_wf), 1.0):
                if wf <= 0.0:
                    continue
                st, info = base_coat_pass(fg, args.base_coat_angle, coat_spacing,
                                          wf, args.base_coat_jitter, base_col, rng)
                cov, ovl, ov2, mx, ovc = _base_raster_metrics(fg, st, H, W)
                info.update({"coverage": cov, "overlap_frac": ovl,
                             "overlap_px_over2": ov2, "max_count": mx,
                             "overlap_canvas_frac": ovc,
                             "width_factor_effective": round(float(wf), 4),
                             "width_factor_requested": round(requested_wf, 4)})
                if best is None or cov is None or cov >= 0.99:
                    best = (st, info)
                if cov is not None and cov >= 0.99 and ovl is not None and ovl <= 0.05:
                    break
            strokes, base_info = best
            base_info["pattern"] = "rows"
        base_info["color"] = [round(float(c), 5) for c in base_col]
        p1 = len(strokes)
        ren = hgr._render_strokes(strokes, H, W, ss=1)
        print(f"[layered] pass1 base coat (toned/{base_info.get('pattern')}): "
              f"{p1} strokes color={base_info['color']} "
              f"coverage={base_info['coverage']} overlap={base_info['overlap_frac']}")
        if args.base_gradient:
            print("[layered] NOTE --base-gradient is ignored in toned/none mode "
                  "(the ground is one flat colour by design)")
    elif args.base_color_mode == "none":
        strokes = []
        base_info = {"color": None, "coverage": None, "overlap_frac": None,
                     "strokes": 0, "spacing": None, "width": None, "angle": None}
        p1 = 0
        ren = hgr._render_strokes(strokes, H, W, ss=1)
        print("[layered] pass1 base coat (none): 0 strokes (bare canvas)")
    else:
        # legacy grounds (v1.32-v1.34): multi-colour / local-colour underpainting
        if args.base_color_mode == "single":
            base_col = np.median(rgb[fg], axis=0) if fg.any() else np.array([0.7, 0.65, 0.55])
            base_map = np.broadcast_to(base_col, rgb.shape).copy()
        elif args.base_color_mode == "source":
            base_map = hg.gaussian_blur(rgb.astype(np.float32),
                                        float(args.base_blur)).astype(np.float64)
        else:
            base_map, _ = palette_map(rgb, fg, args.base_k, 8.0, rng)
        pass1_stats = _new_gradient_stats() if args.base_gradient else None
        p1_kwargs = dict(color_map=base_map, color_jitter=args.base_jitter,
                         gradient=args.base_gradient)
        if args.base_gradient:
            p1_kwargs.update(_gradient_kwargs())
            p1_kwargs["gradient_stats"] = pass1_stats
        strokes = tile_pass(rgb, fg, fg, theta, args.coarse,
                            args.base_width_factor, args.base_length_factor, 0,
                            "base_pass1", rng, **p1_kwargs)
        if pass1_stats is not None:
            _merge_gradient_stats(gradient_stats, pass1_stats)
            pass_stats["base_pass1"] = pass1_stats
        ren = hgr._render_strokes(strokes, H, W, ss=1)
        p1 = len(strokes)
        print(f"[layered] pass1 full cover (legacy {args.base_color_mode}): {p1}"
              f"{' gradient' if args.base_gradient else ''}")

    _stage("pass1 ground done")

    # Pass 2: only high-error regions, medium spacing
    mask2, _ = error_mask(rgb, ren, fg, args.mid_err_pct, grad)
    if args.upper_color_mode == "source":
        mid_map = hg.gaussian_blur(rgb.astype(np.float32), float(args.upper_blur)).astype(np.float64)
    elif args.local_color:
        mid_map = local_palette_map(rgb, fg, args.pass2_tile, args.pass2_k, 4.0, rng)
    else:
        mid_map, _ = palette_map(rgb, fg, args.mid_k, 4.0, rng)
    pass2_stats = _new_gradient_stats()
    pass2 = tile_pass(rgb, fg, mask2, theta, args.mid, 1.45, 1.8, 1,
                      "base_pass2", rng, color_map=mid_map,
                      color_jitter=args.mid_jitter, gradient=args.upper_gradient,
                      gradient_stats=pass2_stats, flow=flow_on, coh=coh_map,
                      labels=labels, region_dirs=region_dirs,
                      **_gradient_kwargs())
    _merge_gradient_stats(gradient_stats, pass2_stats)
    pass_stats["base_pass2"] = pass2_stats
    strokes += pass2
    ren = hgr._render_strokes(strokes, H, W, ss=1)
    p2 = len(pass2)
    print(f"[layered] pass2 local cover: {p2} mask={int(mask2.sum())}")

    _stage("pass2 done")

    # Pass 3: remaining high-error / high-detail pixels, fine spacing
    mask3, _ = error_mask(rgb, ren, fg, args.fine_err_pct, grad)
    if args.fine_grad_pct > 0:
        detail_gate = fg & (grad > float(np.percentile(grad[fg], args.fine_grad_pct)))
        detail_gate = ndi.binary_dilation(detail_gate, iterations=4)
        mask3 = mask3 & detail_gate
    if args.upper_color_mode == "source":
        fine_map = hg.gaussian_blur(rgb.astype(np.float32), max(1.0, args.upper_blur * 0.7)).astype(np.float64)
    elif args.local_color:
        fine_map = local_palette_map(rgb, fg, args.pass3_tile, args.pass3_k, 2.0, rng)
    else:
        fine_map, _ = palette_map(rgb, fg, args.fine_k, 2.0, rng)
    pass3_stats = _new_gradient_stats()
    pass3 = tile_pass(rgb, fg, mask3, theta, args.fine, 1.3, 1.55, 2,
                      "base_pass3", rng, color_map=fine_map,
                      color_jitter=args.fine_jitter, gradient=args.upper_gradient,
                      gradient_stats=pass3_stats, flow=flow_on, coh=coh_map,
                      labels=labels, region_dirs=region_dirs,
                      **_gradient_kwargs())
    _merge_gradient_stats(gradient_stats, pass3_stats)
    pass_stats["base_pass3"] = pass3_stats
    strokes += pass3
    print(f"[layered] pass3 local cover: {len(pass3)} mask={int(mask3.sum())}")

    _stage("pass3 done")
    if args.pass4:
        mask4, _ = error_mask(rgb, ren, fg, args.pass4_err_pct, grad)
        if args.fine_grad_pct > 0:
            mask4 = mask4 & detail_gate
        if args.upper_color_mode == "source":
            map4 = hg.gaussian_blur(rgb.astype(np.float32), max(0.8, args.upper_blur * 0.45)).astype(np.float64)
        elif args.local_color:
            map4 = local_palette_map(rgb, fg, args.pass4_tile, args.pass4_k, 1.2, rng)
        else:
            map4, _ = palette_map(rgb, fg, args.fine_k, 1.2, rng)
        pass4_stats = _new_gradient_stats()
        pass4 = tile_pass(rgb, fg, mask4, theta, args.pass4_spacing, 1.2, 1.4, 3,
                          "base_pass4", rng, color_map=map4,
                          color_jitter=args.fine_jitter * 0.6,
                          gradient=args.upper_gradient,
                          gradient_stats=pass4_stats, flow=flow_on, coh=coh_map,
                          labels=labels, region_dirs=region_dirs,
                          **_gradient_kwargs())
        _merge_gradient_stats(gradient_stats, pass4_stats)
        pass_stats["base_pass4"] = pass4_stats
        strokes += pass4
        ren = hgr._render_strokes(strokes, H, W, ss=1)
        print(f"[layered] pass4 local colour: {len(pass4)} mask={int(mask4.sum())}")
    else:
        pass4 = []
        pass_stats["base_pass4"] = _new_gradient_stats()

    _stage("pass4 done")

    # v1.36 opt-in: thick loaded-brush dabs (block-in accents) on top of pass4.
    thick_dab_add = []
    thick_dab_info = None
    if int(args.thick_dabs) > 0:
        thick_src = mid_map if args.upper_color_mode == "source" else None
        thick_dab_add, thick_dab_info = thick_dab_pass(
            rgb, fg, rng, int(args.thick_dabs), theta, coh_map, labels,
            region_dirs, thick_src, H, W, layer=4)
        strokes += thick_dab_add
        ren = hgr._render_strokes(strokes, H, W, ss=1)
        print(f"[layered] thick dabs: {len(thick_dab_add)}")

    _stage("thick dabs done")

    # Reference detail lines, but never old base/fix/translucent layers
    detail = (load_strokes(args.detail) if args.detail else [])
    if detail:
        strokes += detail
        ren = hgr._render_strokes(strokes, H, W, ss=1)

    # v1.34 opt-in: dense fine-texture pass over the areas the render left flat.
    # The flat mask comes from the *current* render, so the pass adapts to the
    # image instead of using a hard-coded region.
    texture_flat_add = []
    texture_flat_info = None
    if args.texture_flat:
        hard_cap = max(0, int(args.max_curves) - len(strokes))
        if args.texture_flat_count > 0 and args.texture_flat_count > hard_cap:
            print(f"[layered] WARNING texture-flat count {args.texture_flat_count} "
                  f"> remaining budget {hard_cap}; truncating to keep <= {args.max_curves}")
        texture_flat_add, texture_flat_info = texture_flat_pass(
            rgb, fg, ren, rng,
            count=args.texture_flat_count,
            spacing=args.texture_flat_spacing,
            lstd_thr=args.texture_flat_lstd,
            ref_std_thr=args.texture_flat_ref_std,
            len_min=args.texture_flat_len_min,
            len_max=args.texture_flat_len_max,
            width_min=args.texture_flat_width_min,
            width_max=args.texture_flat_width_max,
            layer=args.texture_flat_layer,
            jitter=args.texture_flat_jitter,
            hard_cap=hard_cap)
        if texture_flat_add:
            strokes += texture_flat_add
            ren = hgr._render_strokes(strokes, H, W, ss=1)
        print(f"[layered] texture-flat: {len(texture_flat_add)} strokes "
              f"(candidates {texture_flat_info['candidates']}, "
              f"flat_frac {texture_flat_info['flat_frac_fg']})")

    _stage("detail + texture-flat done")

    # --- v1.37 silhouette-aware stroke generation --------------------------
    # Generation-time geometry clipping: every non-ground stroke is pulled
    # inside the eroded subject silhouette (endpoints + per-point widths) BEFORE
    # the hard cap and the paint order.  No post-processing of the image.
    silhouette_on = (args.silhouette == "on"
                     or (args.silhouette == "auto"
                         and args.base_coat_pattern == "wash"
                         and paint_order_mode != "legacy"
                         and args.clip_to_fg != "off"
                         and white_frac > 0.02))
    sil_mask = None
    sil_area_frac = None
    sil_fallback = False
    sil_stats = {"clipped": 0, "trimmed": 0, "dropped": 0,
                 "over_before": 0.0, "over_after": 0.0}
    if silhouette_on:
        sil_mask, sil_area_frac, sil_fallback = _build_silhouette(
            fg, args.silhouette_shrink_px)
        strokes, sil_stats = _clip_strokes_to_silhouette(
            strokes, sil_mask, H, W, args.silhouette_margin_px,
            args.silhouette_min_width_px)
        if paint_order_mode == "legacy":
            ren = hgr._render_strokes(strokes, H, W, ss=1)
        print(f"[layered] silhouette: on area_frac={sil_area_frac:.4f} "
              f"fallback={sil_fallback} clipped={sil_stats['clipped']} "
              f"trimmed={sil_stats['trimmed']} dropped={sil_stats['dropped']} "
              f"overshoot {sil_stats['over_before']}px -> "
              f"{sil_stats['over_after']}px")
        _stage("silhouette clipping done")

    # --- 4 GiB delivery contract hard cap ---------------------------------
    # Default recipes can exceed 100k curves on some images.  Never crash and
    # never silently break the contract: deterministically thin the *finest*
    # layers first -- optional fine texture, then pass4 fine colour, then
    # pass3, and only as the last resort the coarse pass2 colour layer -- so
    # the formal drawing keeps its structure.  Strokes are removed evenly
    # across the canvas (no spatial hole); base ground and user-provided detail
    # strokes are never dropped unless the cap is below base+detail itself.
    pre_cap_total = len(strokes)
    capped_100k = False
    cap_adjustments = {}
    cap = int(args.max_curves)
    if cap > 0 and pre_cap_total > cap:
        capped_100k = True
        need = pre_cap_total - cap
        drop = set()
        for kind in ("oil_flat_texture", "thick_dab", "base_pass4",
                     "base_pass3", "base_pass2"):
            if need <= 0:
                break
            idx = [i for i, s in enumerate(strokes) if str(s.get("kind", "")) == kind]
            if not idx:
                continue
            n_before = len(idx)
            n_rm = min(need, n_before)
            keep_n = n_before - n_rm
            if keep_n <= 0:
                keep = set()
            else:
                keep = set(np.unique(np.round(
                    np.linspace(0, n_before - 1, keep_n)).astype(np.int64)).tolist())
            removed = 0
            for j, i in enumerate(idx):
                if j not in keep:
                    drop.add(i)
                    removed += 1
            cap_adjustments[kind] = {
                "removed": int(removed),
                "frac": round(float(removed) / max(n_before, 1), 4),
            }
            need -= removed
        if need > 0:
            # Last resort (only if the cap is smaller than base+detail itself):
            # thin whatever is left, evenly.  Keeps the contract hard.
            rest = [i for i in range(pre_cap_total) if i not in drop]
            if rest:
                n_before = len(rest)
                n_rm = min(need, n_before)
                keep_n = n_before - n_rm
                if keep_n <= 0:
                    keep = set()
                else:
                    keep = set(np.unique(np.round(
                        np.linspace(0, n_before - 1, keep_n)).astype(np.int64)).tolist())
                removed = 0
                for j, i in enumerate(rest):
                    if j not in keep:
                        drop.add(i)
                        removed += 1
                cap_adjustments["remaining_layers"] = {
                    "removed": int(removed),
                    "frac": round(float(removed) / max(n_before, 1), 4),
                }
                need -= removed
        if drop:
            strokes = [s for i, s in enumerate(strokes) if i not in drop]
        ren = hgr._render_strokes(strokes, H, W, ss=1)
        pass2 = [s for s in strokes if str(s.get("kind", "")) == "base_pass2"]
        pass3 = [s for s in strokes if str(s.get("kind", "")) == "base_pass3"]
        pass4 = [s for s in strokes if str(s.get("kind", "")) == "base_pass4"]
        texture_flat_add = [s for s in strokes
                            if str(s.get("kind", "")) == "oil_flat_texture"]
        p2 = len(pass2)
        print(f"[layered] WARNING over --max-curves ({cap}): "
              f"{pre_cap_total} -> {len(strokes)} strokes  {cap_adjustments}")

    _stage("hard cap done")

    # --- v1.36 region-grouped painting order ------------------------------
    # Base ground first (the wash prepares the whole canvas), then finish one
    # region at a time: block-in -> fix -> detail -> texture -> next region.
    paint_metrics = {"paint_order_effective": "legacy",
                     "order_y_spearman": None,
                     "order_adjacent_median_px": None,
                     "order_region_run_frac": None}
    if paint_order_mode != "legacy" and labels is not None:
        _region_areas = None
        if labels is not None and len(cent) > 0:
            _flat = labels[fg].ravel()
            _region_areas = np.bincount(_flat, minlength=len(cent)).astype(np.float64)
        order_idx, paint_metrics = paint_order_indices(
            strokes, labels, region_order, paint_order_mode, H, W,
            region_areas=_region_areas)
        strokes = [strokes[i] for i in order_idx]
        ranked = []
        for i, s in enumerate(strokes):
            d = dict(s)
            d["layer"] = int(i)  # render in painting order, save original layers
            ranked.append(d)
        ren = hgr._render_strokes(ranked, H, W, ss=1)
        print(f"[layered] paint order {paint_order_mode}: "
              f"y_spearman={paint_metrics.get('order_y_spearman')} "
              f"adjacent_median={paint_metrics.get('order_adjacent_median_px')}px "
              f"region_run_frac={paint_metrics.get('order_region_run_frac')}")
    flow_metrics_d = flow_direction_metrics(strokes, theta, H, W)
    print(f"[layered] flow_orientation_frac={flow_metrics_d['flow_orientation_frac']} "
          f"axis_aligned_frac={flow_metrics_d['axis_aligned_frac']}")

    clip_fg = (args.clip_to_fg == "on"
               or (args.clip_to_fg == "auto"
                   and args.base_coat_pattern == "wash"
                   and paint_order_mode != "legacy"))
    if silhouette_on and sil_mask is not None:
        ren = _composite_silhouette(ren, sil_mask, args.silhouette_feather_px)
    elif clip_fg:
        ren = np.where(fg[..., None], ren, 1.0)
    _stage("paint order + final render done")

    psnr = hgr._psnr(rgb, ren)
    ssim = hgr._ssim(rgb, ren)
    white = np.all(ren > 0.99, axis=2)
    white_in_fg = int((white & fg).sum())
    stray_color_px, stray_color_frac = _stray_color_metrics(rgb, ren, fg)
    gsum = _finalize_gradient_stats(gradient_stats)
    by_pass = {name: _finalize_gradient_stats(st) for name, st in pass_stats.items()}
    # --- v1.35 base-ground verification data -----------------------------
    _base_kinds = ("base_pass1", "base_toned")
    _base_cols = np.asarray([s["color"] for s in strokes
                             if str(s.get("kind", "")) in _base_kinds],
                            dtype=np.float64)
    base_color_std = (round(float(_base_cols.std(axis=0).max()), 8)
                      if len(_base_cols) else 0.0)
    base_gradient_strokes = sum(1 for s in strokes
                                if str(s.get("kind", "")) in _base_kinds
                                and s.get("gradient") is not None)
    base_report = {
        "base_coat_pattern": (None if base_info is None
                              else base_info.get("pattern")),
        "base_metrics_mode": (None if base_info is None
                              else base_info.get("metrics_mode")),
        "base_metrics_scale": (None if base_info is None
                               else base_info.get("metrics_scale")),
        "base_color": (None if base_info is None else base_info.get("color")),
        "base_coat_angle": (None if base_info is None else base_info.get("angle")),
        "base_coat_spacing": (None if base_info is None else base_info.get("spacing")),
        "base_coat_width": (None if base_info is None else base_info.get("width")),
        "base_coat_width_factor": (None if base_info is None
                                   else base_info.get("width_factor_effective")),
        "base_coat_width_factor_requested": (float(args.base_coat_width_factor)
                                             if args.base_color_mode == "toned"
                                             else None),
        "base_coat_jitter": (float(args.base_coat_jitter)
                             if args.base_color_mode == "toned" else None),
        "base_coverage": (None if base_info is None else base_info.get("coverage")),
        "base_overlap_frac": (None if base_info is None
                              else base_info.get("overlap_frac")),
        "base_overlap_canvas_frac": (None if base_info is None
                                     else base_info.get("overlap_canvas_frac")),
        "base_coverage_analytic": (None if base_info is None
                                   else base_info.get("coverage_analytic")),
        "base_overlap_analytic": (None if base_info is None
                                  else base_info.get("overlap_analytic")),
        "base_overlap_px_over2": (None if base_info is None
                                  else base_info.get("overlap_px_over2")),
        "base_max_count": (None if base_info is None
                           else base_info.get("max_count")),
        "base_color_std": base_color_std,
        "base_gradient_strokes": int(base_gradient_strokes),
        "base_strokes": int(p1),
    }
    png = os.path.join(args.outdir, f"layered_base_R{args.work_res}.png")
    Image.fromarray((np.clip(ren, 0, 1) * 255).astype(np.uint8)).save(png)
    js = os.path.join(args.outdir, "layered_base_strokes.json")
    save_strokes(js, strokes)
    report = {
        "input": os.path.abspath(args.input), "work_res": args.work_res,
        "size": [W, H], "strokes_out": len(strokes),
        "max_curves": int(args.max_curves),
        "capped_100k": bool(capped_100k),
        "cap_adjustments": (cap_adjustments if capped_100k else None),
        "pre_cap_strokes": int(pre_cap_total),
        "pass1_full": p1, "pass2_local": p2, "pass3_local": len(pass3),
        "pass4_local": len(pass4),
        "detail": len(detail), "white_px_in_fg": white_in_fg,
        # --- pass1 ground colouring (new in v1.34) ------------------------
        "base_color_mode": args.base_color_mode,
        "base_blur": args.base_blur,
        "base_gradient": bool(args.base_gradient),
        "base_width_factor": args.base_width_factor,
        "base_length_factor": args.base_length_factor,
        **base_report,
        # --- v1.36 human painting style ----------------------------------
        "wash_angle": float(args.wash_angle),
        "flow_strokes": bool(flow_on),
        "flow_orientation_frac": flow_metrics_d.get("flow_orientation_frac"),
        "axis_aligned_frac": flow_metrics_d.get("axis_aligned_frac"),
        "direction_strokes": flow_metrics_d.get("direction_strokes"),
        "paint_order": args.paint_order,
        **paint_metrics,
        "region_count_used": int(region_count_used),
        "clip_to_fg": bool(clip_fg),
        "silhouette_mode": args.silhouette,
        "silhouette_on": bool(silhouette_on),
        "silhouette_area_frac": (None if sil_area_frac is None
                                 else round(float(sil_area_frac), 5)),
        "silhouette_fallback": bool(sil_fallback),
        "silhouette_shrink_px": float(args.silhouette_shrink_px),
        "silhouette_margin_px": float(args.silhouette_margin_px),
        "silhouette_min_width_px": float(args.silhouette_min_width_px),
        "silhouette_feather_px": float(args.silhouette_feather_px),
        "silhouette_clipped_strokes": int(sil_stats["clipped"]),
        "silhouette_points_trimmed": int(sil_stats["trimmed"]),
        "silhouette_dropped_strokes": int(sil_stats["dropped"]),
        "silhouette_overshoot_before_max_px": float(sil_stats["over_before"]),
        "silhouette_overshoot_after_max_px": float(sil_stats["over_after"]),
        "white_background_frac": round(float(white_frac), 5),
        "fill_count": 0,
        "thick_dabs": int(args.thick_dabs),
        "thick_dab_strokes": len(thick_dab_add),
        "texture_flat_strokes": len(texture_flat_add),
        "texture_flat_candidates": (None if texture_flat_info is None
                                    else texture_flat_info["candidates"]),
        "texture_flat_params": (None if texture_flat_info is None else {
            "count": args.texture_flat_count,
            "spacing": args.texture_flat_spacing,
            "lstd": args.texture_flat_lstd,
            "ref_std": args.texture_flat_ref_std,
            "len_px": [args.texture_flat_len_min, args.texture_flat_len_max],
            "width_px": [args.texture_flat_width_min, args.texture_flat_width_max],
            "layer": args.texture_flat_layer,
            "jitter": args.texture_flat_jitter,
            **texture_flat_info,
        }),
        "psnr": round(psnr, 4), "ssim": round(ssim, 5),
        # --- gradient safety gate (new in v1.33) -------------------------
        "gradient_gate_params": {
            "gate": args.gradient_gate,
            "max_delta": args.gradient_max_delta,
            "edge_delta": args.gradient_edge_delta,
            "edge_pct": args.gradient_edge_pct,
        },
        "gradient_candidates": gsum["candidates"],
        "gradient_strokes": gsum["accepted"],
        "gradient_rejected_delta": gsum["rejected_delta"],
        "gradient_rejected_edge": gsum["rejected_edge"],
        "gradient_rejected_edge_color": gsum["rejected_edge_color"],
        "gradient_rejected_edge_grad": gsum["rejected_edge_grad"],
        "gradient_rejected_oob": gsum["rejected_oob"],
        "gradient_edge_frac": gradient_edge_frac,
        "gradient_mean_delta": gsum["mean_delta"],
        "gradient_max_delta": gsum["max_delta"],
        "gradient_mean_step": gsum["mean_step"],
        "gradient_max_step": gsum["max_step"],
        "gradient_accepted_mean_delta": gsum["accepted_mean_delta"],
        "gradient_rejected_delta_frac": gsum["rejected_delta_frac"],
        "gradient_rejected_edge_frac": gsum["rejected_edge_frac"],
        "gradient_rejected_oob_frac": gsum["rejected_oob_frac"],
        "gradient_by_pass": by_pass,
        # --- defect metric (new in v1.33) --------------------------------
        "stray_color_px": stray_color_px,
        "stray_color_frac": round(stray_color_frac, 6),
        "stray_color_thr": 0.25,
        "png": os.path.abspath(png), "strokes": os.path.abspath(js),
        "seconds": round(time.time() - t0, 1),
    }
    json.dump(report, open(os.path.join(args.outdir, "layered_base_report.json"), "w",
                           encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
