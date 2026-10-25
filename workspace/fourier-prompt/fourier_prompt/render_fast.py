"""Fast raster backend for ``.fbs`` v2 documents (WP-F).

The exact backend lives in the read-only ``fourier-brushstroke`` skill
(``fbs_core.render``).  This module **replaces only the rasterisation layer**:
geometry/width/colour/alpha/shape sampling is delegated to the skill's
``sample_stroke`` / ``eval_*`` code, and the document ordering / fill semantics
mirror ``_render_per_stroke``.  No FFT code and no document assembly is copied.

Rasterisation
-------------
Every ink stroke is sampled by the skill to a polyline with per-sample radius,
RGB and alpha.  Each polyline is turned into a *quad strip* (left/right offsets
along the local normal) and drawn with :meth:`PIL.ImageDraw.polygon` onto a
supersampled ``L`` mask (in chunks, so self-intersecting ribbons cannot punch
holes with an even-odd fill).  The mask is then composited per colour block into
a float32 supersampled canvas which is box-downsampled and rounded to uint8.

Fills are drawn with the same polygon + Gaussian-soft-edge recipe the skill's
``FloatCanvas.fill_polygon`` uses, so flat fills stay pixel-close to exact.

Supported / fallback
--------------------
``fast_supported(doc)`` returns ``(ok, reasons)``.  Shapes
``round/soft/ink/marker/watercolor/bristle`` are supported (``soft``,
``watercolor``/``bristle`` and ``marker`` are approximations; the texture
kernels of watercolour/bristle are omitted and recorded in
``info["approximations"]``).  ``chalk/spatter/pencil/airbrush``, one-stroke
documents, gradient-mesh documents, colour-field fills and text layers fall back
to the skill's exact renderer with an explicit ``fallback_reason`` — never
silently.

Determinism
-----------
No randomness anywhere; identical input and version produce identical pixels.

Parity
------
``compare_images(a, b)`` returns ``max_abs_diff / mean_abs_diff / psnr_db /
within1_pct / within2_pct / parity_pass`` with the gate
``max|Δ| <= 2 and PSNR >= 42 dB``.  The prescribed quad-strip method cannot
reach the max-diff clause on anti-aliased silhouettes (binary supersampling vs.
the skill's analytic per-subsample coverage); :mod:`reports.WP-F` records the
measured values honestly instead of masking the failure.
"""

from __future__ import annotations

import copy
import math
import time
import tracemalloc
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

__all__ = [
    "FAST_SHAPES",
    "APPROX_SHAPES",
    "UNSUPPORTED_SHAPES",
    "PARITY_MAX_ABS_DIFF",
    "PARITY_PSNR_DB",
    "fast_supported",
    "render",
    "render_with_info",
    "render_auto",
    "compare_images",
    "bench",
    "load_image",
    "truncate_doc",
    "LAST_INFO",
]

#: shapes the fast path can rasterise
FAST_SHAPES = frozenset({"round", "soft", "ink", "marker", "watercolor", "bristle"})
#: supported shapes whose skill-side texture kernel is approximated (not copied)
APPROX_SHAPES = frozenset({"soft", "marker", "watercolor", "bristle"})
#: shapes that require the exact backend
UNSUPPORTED_SHAPES = frozenset({"chalk", "spatter", "pencil", "airbrush"})

PARITY_MAX_ABS_DIFF = 2
PARITY_PSNR_DB = 42.0

#: minimum internal supersampling used for rasterisation (quality floor);
#: ``supersample`` above this value is honoured, below it is raised.
_MIN_RASTER_SS = 4
_MAX_RASTER_SS = 8
#: supersampled canvas memory cap (bytes); above it the SS is reduced to 2
_CANVAS_BYTES_CAP = 600 * 1024 * 1024

#: one quad-strip chunk = at most this many samples (2px at 4 samples/px)
_CHUNK_SAMPLES = 10
#: prefilter radius (supersample px) applied to sub-pixel ink masks
_INK_BLUR = 0.8

LAST_INFO: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# skill access (lazy, read-only)
# ---------------------------------------------------------------------------
def _api():
    from .compat.skill_api import import_skill

    return import_skill()


def _warn_log():
    api = _api()
    return api.util.WarningLog()


def _exact_render(doc: dict, *, resolution: int, supersample: int, background, width_scale):
    """Call the skill exact renderer with the same width/background contract."""
    from .compat.skill_api import render as skill_render

    target = doc
    if background is not None:
        target = copy.copy(doc)
        target["base_color"] = background
    return skill_render(
        target,
        resolution=int(resolution),
        supersample=int(supersample),
        width_scale=float(width_scale),
    )


# ---------------------------------------------------------------------------
# document inspection
# ---------------------------------------------------------------------------
def _style_shapes(doc: dict, stroke: dict) -> list[str]:
    api = _api()
    style = api.render._resolve_style(doc, stroke, None)
    curve = style.get("shape") or {}
    out: list[str] = []
    keys = curve.get("keys") if isinstance(curve, dict) else None
    if isinstance(keys, list):
        for entry in keys:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                out.append(str(entry[1]))
    if isinstance(curve, dict) and curve.get("preset"):
        out.append(str(curve["preset"]))
    if not out:
        out.append("round")
    return out


def _has_color_field(stroke: dict) -> bool:
    cf = stroke.get("color_field")
    return isinstance(cf, dict) and bool(cf)


def fast_supported(doc: dict) -> tuple[bool, list[str]]:
    """Return ``(can_use_fast, reasons)``; reasons are empty when supported."""
    if not isinstance(doc, dict):
        return False, ["document is not a dict"]
    reasons: list[str] = []
    mode = doc.get("mode")
    if mode not in (None, "per_stroke"):
        reasons.append(f"mode={mode!r}（仅支持 per_stroke）")
    method = str((doc.get("decomposition") or {}).get("method") or "")
    if method == "adaptive_gradient_mesh" or doc.get("mesh") or doc.get("gradient_mesh"):
        reasons.append(f"decomposition.method={method or 'mesh'}（网格/3D 不支持）")
    for key in ("texts", "text_elements", "font_path", "fonts_embedded"):
        if doc.get(key):
            reasons.append(f"document.{key} 存在（文本不支持，需 exact）")
    strokes = doc.get("strokes") or []
    if not isinstance(strokes, list):
        return False, reasons + ["strokes 不是列表"]
    seen_shapes: dict[str, int] = {}
    for idx, st in enumerate(strokes):
        if not isinstance(st, dict):
            continue
        if st.get("fill_role") == "region_fill":
            if _has_color_field(st):
                reasons.append(f"stroke[{idx}].color_field（颜色场填充不支持）")
            continue
        if st.get("mesh") or st.get("fill_role") == "region_mesh":
            reasons.append(f"stroke[{idx}] 是网格元素（不支持）")
            continue
        if not st.get("coef"):
            continue
        for shape in _style_shapes(doc, st):
            seen_shapes[shape] = seen_shapes.get(shape, 0) + 1
            if shape in UNSUPPORTED_SHAPES:
                reasons.append(f"shape={shape}（stroke id={st.get('id')!r}，需 exact）")
            elif shape not in FAST_SHAPES:
                reasons.append(f"shape={shape}（stroke id={st.get('id')!r} 未知笔形）")
    # de-duplicate while preserving order
    uniq: list[str] = []
    for reason in reasons:
        if reason not in uniq:
            uniq.append(reason)
    return (not uniq), uniq


def _resolve_width_scale(doc: dict, width_scale: float | None) -> float:
    """explicit arg > ``doc.render_contract.width_scale`` > 1.0 (KICKOFF §2.2)."""
    if width_scale is not None:
        value = float(width_scale)
    else:
        contract = doc.get("render_contract")
        value = 1.0
        if isinstance(contract, dict) and contract.get("width_scale") is not None:
            value = float(contract["width_scale"])
    if not math.isfinite(value) or value < 0.0:
        raise ValueError(f"width_scale 非法: {width_scale!r}")
    return value


def _effective_ss(doc: dict, resolution: int, supersample: int) -> int:
    api = _api()
    W, H, _ = api.render._output_size(doc, int(resolution))
    ss = int(supersample)
    if ss < 1 or ss > _MAX_RASTER_SS:
        raise ValueError(f"supersample 非法（应为 1..{_MAX_RASTER_SS}）: {supersample!r}")
    ss = max(ss, _MIN_RASTER_SS)
    while ss > 1 and (W * ss) * (H * ss) * 3 * 4 > _CANVAS_BYTES_CAP:
        ss //= 2
    return max(1, ss)


def _shape_histogram(doc: dict) -> dict[str, int]:
    hist: dict[str, int] = {}
    for st in doc.get("strokes") or []:
        if not isinstance(st, dict) or not st.get("coef"):
            continue
        for shape in _style_shapes(doc, st):
            hist[shape] = hist.get(shape, 0) + 1
    return hist


# ---------------------------------------------------------------------------
# raster helpers
# ---------------------------------------------------------------------------
def _color_blocks(rgb: np.ndarray, alpha: np.ndarray) -> list[tuple[int, int]]:
    """Split [0,n-1] into inclusive blocks of near-constant colour/alpha."""
    n = len(rgb)
    if n == 0:
        return []
    tol_c = 2.5 / 255.0
    tol_a = 0.01
    starts = [0]
    base = 0
    for i in range(1, n):
        dc = float(np.abs(rgb[i] - rgb[base]).sum())
        da = abs(float(alpha[i]) - float(alpha[base]))
        if dc > tol_c or da > tol_a:
            starts.append(i)
            base = i
    starts.append(n)
    return [(starts[i], starts[i + 1] - 1) for i in range(len(starts) - 1)]


def _ribbon_polygon(pts: np.ndarray, radii: np.ndarray) -> np.ndarray:
    """Closed polygon of the left/right offset curves of a short polyline."""
    n = len(pts)
    if n < 2:
        return np.empty((0, 2), dtype=np.float64)
    tang = np.empty_like(pts)
    tang[0] = pts[1] - pts[0]
    tang[-1] = pts[-1] - pts[-2]
    if n > 2:
        tang[1:-1] = pts[2:] - pts[:-2]
    norm = np.linalg.norm(tang, axis=1)
    bad = norm < 1e-12
    if bad.any():
        good = np.where(~bad)[0]
        fallback = tang[good[0]] if len(good) else np.asarray([1.0, 0.0])
        tang[bad] = fallback
        norm[bad] = max(float(np.linalg.norm(fallback)), 1e-12)
    nx = -tang[:, 1] / norm
    ny = tang[:, 0] / norm
    left = np.stack([pts[:, 0] + nx * radii, pts[:, 1] + ny * radii], axis=1)
    right = np.stack([pts[:, 0] - nx * radii, pts[:, 1] - ny * radii], axis=1)
    return np.vstack([left, right[::-1]])


def _draw_ribbon(draw: ImageDraw.ImageDraw, pts: np.ndarray, radii: np.ndarray) -> None:
    """Draw a variable-width strip as overlapping quad-strip chunks (union)."""
    n = len(pts)
    if n == 1:
        r = float(radii[0])
        if r > 0.0:
            x, y = float(pts[0, 0]), float(pts[0, 1])
            draw.ellipse([x - r, y - r, x + r, y + r], fill=255)
        return
    i = 0
    while i < n - 1:
        j = min(i + _CHUNK_SAMPLES - 1, n - 1)
        poly = _ribbon_polygon(pts[i : j + 1], radii[i : j + 1])
        if len(poly):
            draw.polygon([(float(x), float(y)) for x, y in poly], fill=255)
        if j >= n - 1:
            break
        i = j - 1  # one shared segment: no seams between chunks
    # round caps at both ends of the run (the skill's capsule semantics)
    for k in (0, n - 1):
        r = float(radii[k])
        if r <= 0.0:
            continue
        x, y = float(pts[k, 0]), float(pts[k, 1])
        draw.ellipse([x - r, y - r, x + r, y + r], fill=255)


def _draw_discs(
    draw: ImageDraw.ImageDraw,
    pts: np.ndarray,
    radii: np.ndarray,
    *,
    aspect_x: float = 1.0,
) -> None:
    """Draw a union of discs/ellipses (marker approximation / fallback path)."""
    n = len(pts)
    if n == 0:
        return
    rm = float(np.max(radii)) if n else 0.0
    stride = 1
    if rm > 2.0:
        # spacing <= 0.35 * radius keeps the union scallop-free
        spacing = max(1.0, 0.35 * rm)
        arc = float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1))) if n > 1 else 0.0
        if arc > 0.0:
            stride = max(1, int(round(spacing / max(arc / max(n - 1, 1), 1e-9))))
    for k in range(0, n, stride):
        r = float(radii[k])
        if r <= 0.0:
            continue
        x, y = float(pts[k, 0]), float(pts[k, 1])
        draw.ellipse([x - r * aspect_x, y - r, x + r * aspect_x, y + r], fill=255)


def _mask_bbox(pts: np.ndarray, radii: np.ndarray, width: int, height: int, pad: int = 2):
    rmax = float(np.max(radii)) if len(radii) else 0.0
    rmax = max(rmax, 0.0) + pad
    x0 = max(0, int(math.floor(float(pts[:, 0].min()) - rmax)))
    x1 = min(width, int(math.ceil(float(pts[:, 0].max()) + rmax)) + 1)
    y0 = max(0, int(math.floor(float(pts[:, 1].min()) - rmax)))
    y1 = min(height, int(math.ceil(float(pts[:, 1].max()) + rmax)) + 1)
    return x0, x1, y0, y1


def _composite_mask(
    canvas: np.ndarray,
    mask: np.ndarray,
    bbox: tuple[int, int, int, int],
    color: np.ndarray,
    alpha: float,
) -> None:
    x0, x1, y0, y1 = bbox
    if x1 <= x0 or y1 <= y0 or alpha <= 0.0:
        return
    cov = mask.astype(np.float32) / 255.0
    eff = (cov * float(alpha))[..., None]
    sub = canvas[y0:y1, x0:x1]
    sub *= 1.0 - eff
    sub += np.asarray(color, dtype=np.float32).reshape(1, 1, 3) * eff


def _fill_mask(
    poly_px: np.ndarray,
    bbox: tuple[int, int, int, int],
    soft_ss: float,
) -> np.ndarray:
    x0, x1, y0, y1 = bbox
    mask = Image.new("L", (x1 - x0, y1 - y0), 0)
    ImageDraw.Draw(mask).polygon(
        [(float(px) - x0, float(py) - y0) for px, py in poly_px], fill=255
    )
    if soft_ss > 0.0:
        mask = mask.filter(ImageFilter.GaussianBlur(max(soft_ss * 0.5, 0.1)))
    return np.asarray(mask, dtype=np.uint8)


def _paint_fill(
    canvas: np.ndarray,
    poly_px: np.ndarray,
    color: np.ndarray,
    alpha: float,
    fill_soft_ss: float,
) -> None:
    if len(poly_px) < 3 or alpha <= 0.0:
        return
    margin = int(math.ceil(fill_soft_ss * 3.0)) + 2
    h, w = canvas.shape[:2]
    x0 = max(0, int(math.floor(float(poly_px[:, 0].min()))) - margin)
    x1 = min(w, int(math.ceil(float(poly_px[:, 0].max()))) + margin + 1)
    y0 = max(0, int(math.floor(float(poly_px[:, 1].min()))) - margin)
    y1 = min(h, int(math.ceil(float(poly_px[:, 1].max()))) + margin + 1)
    if x1 <= x0 or y1 <= y0:
        return
    mask = _fill_mask(poly_px, (x0, x1, y0, y1), fill_soft_ss)
    _composite_mask(canvas, mask, (x0, x1, y0, y1), color, alpha)


# ---------------------------------------------------------------------------
# fast document renderer
# ---------------------------------------------------------------------------
def _render_fast(
    doc: dict,
    *,
    resolution: int,
    supersample: int,
    background,
    width_scale: float,
) -> tuple[np.ndarray, dict[str, Any]]:
    api = _api()
    render_mod = api.render
    W, H, R = render_mod._output_size(doc, int(resolution))
    ss = _effective_ss(doc, resolution, supersample)
    Ws, Hs = W * ss, H * ss
    bg = render_mod._as_rgb01(background if background is not None else doc.get("base_color"))
    canvas = np.empty((Hs, Ws, 3), dtype=np.float32)
    canvas[...] = bg
    strokes = [st for st in (doc.get("strokes") or []) if isinstance(st, dict)]
    fill_soft = float(
        ((doc.get("decomposition") or {}).get("params") or {}).get("fill_soft_px", 2.0) or 0.0
    )
    fill_soft_ss = fill_soft * ss
    order = sorted(
        range(len(strokes)),
        key=lambda i: (
            int(strokes[i].get("layer", 0) or 0),
            0 if strokes[i].get("fill_role") == "region_fill" else 1,
            i,
        ),
    )
    approximations: list[str] = []
    counts = {"strokes": 0, "fills": 0, "skipped": 0}
    for i in order:
        st = strokes[i]
        if st.get("fill_role") == "region_fill":
            poly = st.get("poly")
            if poly is None or len(np.asarray(poly, dtype=np.float64).reshape(-1, 2)) < 3:
                counts["skipped"] += 1
                continue
            color, alpha = render_mod._fill_rgba(doc, st, None)
            poly_px = np.asarray(poly, dtype=np.float64).reshape(-1, 2) * R * ss
            _paint_fill(canvas, poly_px, color, alpha, fill_soft_ss)
            counts["fills"] += 1
            holes = render_mod._parse_fill_holes(st.get("fill_holes"))
            for ring in holes:
                _paint_fill(
                    canvas,
                    np.asarray(ring, dtype=np.float64).reshape(-1, 2) * R * ss,
                    bg,
                    1.0,
                    fill_soft_ss,
                )
            continue
        if not st.get("coef"):
            counts["skipped"] += 1
            continue
        sampled = render_mod.sample_stroke(doc, st, R, width_scale, None)
        pts = np.asarray(sampled["points_world"], dtype=np.float64) * R * ss
        radii = np.asarray(sampled["radii_world"], dtype=np.float64) * R * ss
        rgb = np.asarray(sampled["rgb"], dtype=np.float64).reshape(-1, 3)
        alpha = np.asarray(sampled["alpha"], dtype=np.float64).reshape(-1)
        shapes = [str(v) for v in (sampled.get("shapes") or [])]
        if sampled["closed"] and len(pts):
            pts = np.vstack([pts, pts[:1]])
            radii = np.concatenate([radii, radii[:1]])
            rgb = np.vstack([rgb, rgb[:1]])
            alpha = np.concatenate([alpha, alpha[:1]])
            if shapes:
                shapes = shapes + shapes[:1]
        ink_runs = render_mod._ink_runs(radii)
        if not ink_runs:
            counts["skipped"] += 1
            continue
        blocks = _color_blocks(rgb, alpha)
        for a, b in ink_runs:
            seg_pts = pts[a : b + 1]
            seg_rad = radii[a : b + 1]
            if len(seg_pts) == 0:
                continue
            # shape of the run (first non-round sample wins; per-point changes are rare)
            run_shapes = [shapes[k] for k in range(a, b + 1) if k < len(shapes)] or ["round"]
            shape = next((sh for sh in run_shapes if sh != "round"), "round")
            if shape in APPROX_SHAPES and shape not in approximations:
                approximations.append(shape)
            for ba, bb in blocks:
                lo, hi = max(a, ba), min(b, bb)
                if hi < lo:
                    continue
                block_pts = pts[lo : hi + 1]
                block_rad = radii[lo : hi + 1]
                if len(block_pts) == 0:
                    continue
                x0, x1, y0, y1 = _mask_bbox(block_pts, block_rad, Ws, Hs)
                if x1 <= x0 or y1 <= y0:
                    continue
                mask = Image.new("L", (x1 - x0, y1 - y0), 0)
                draw = ImageDraw.Draw(mask)
                local = block_pts.copy()
                local[:, 0] -= x0
                local[:, 1] -= y0
                if shape == "marker":
                    _draw_discs(draw, local, block_rad, aspect_x=render_mod.MARKER_ASPECT)
                else:
                    _draw_ribbon(draw, local, block_rad)
                mean_r = float(np.mean(block_rad)) if len(block_rad) else 0.0
                if _INK_BLUR > 0.0 and mean_r < 2.0 * ss:
                    # sub-pixel strokes: prefilter the binary mask so the
                    # box-downsample approximates analytic area coverage
                    mask = mask.filter(ImageFilter.GaussianBlur(_INK_BLUR))
                if shape == "soft":
                    if mean_r > 0.0:
                        mask = mask.filter(ImageFilter.GaussianBlur(max(mean_r * 0.25, 0.25)))
                elif shape in ("watercolor", "bristle"):
                    # texture kernel omitted on purpose (documented approximation)
                    pass
                color = rgb[lo : hi + 1].mean(axis=0)
                a_mean = float(np.mean(alpha[lo : hi + 1]))
                _composite_mask(canvas, np.asarray(mask), (x0, x1, y0, y1), color, a_mean)
        counts["strokes"] += 1

    if ss == 2:
        out = canvas[0::2, 0::2].copy()
        out += canvas[0::2, 1::2]
        out += canvas[1::2, 0::2]
        out += canvas[1::2, 1::2]
        out *= 0.25
    else:
        out = canvas.reshape(H, ss, W, ss, 3).mean(axis=(1, 3))
    out = np.clip(out * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)
    info = {
        "quality": "fast",
        "fallback_reason": [],
        "approximations": sorted(approximations),
        "supersample": ss,
        "supersample_requested": int(supersample),
        "width_scale": float(width_scale),
        "resolution": int(R),
        "size": [int(W), int(H)],
        "shapes": _shape_histogram(doc),
        **counts,
    }
    return out, info


def render_with_info(
    doc: dict,
    *,
    resolution: int,
    supersample: int = 2,
    background=None,
    width_scale: float | None = None,
    allow_fallback: bool = True,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Render and return ``(image, info)``; info contains ``fallback_reason``."""
    global LAST_INFO
    started = time.perf_counter()
    ok, reasons = fast_supported(doc)
    ws = _resolve_width_scale(doc, width_scale)
    if not ok:
        if not allow_fallback:
            raise ValueError("fast backend unsupported: " + "; ".join(reasons))
        img = _exact_render(
            doc,
            resolution=resolution,
            supersample=max(1, min(8, int(supersample))),
            background=background,
            width_scale=ws,
        )
        info = {
            "quality": "exact",
            "fallback_reason": list(reasons),
            "approximations": [],
            "supersample": max(1, min(8, int(supersample))),
            "width_scale": ws,
            "resolution": int(resolution),
            "size": [int(img.shape[1]), int(img.shape[0])],
            "shapes": _shape_histogram(doc) if isinstance(doc, dict) else {},
            "strokes": None,
            "fills": None,
            "skipped": None,
        }
    else:
        img, info = _render_fast(
            doc,
            resolution=resolution,
            supersample=supersample,
            background=background,
            width_scale=ws,
        )
        info["fallback_reason"] = []
    info["elapsed_s"] = time.perf_counter() - started
    LAST_INFO = info
    return img, info


def render(
    doc: dict,
    *,
    resolution: int,
    supersample: int = 2,
    background=None,
    width_scale: float | None = None,
) -> np.ndarray:
    """Render ``doc`` to ``(H, W, 3) uint8`` RGB; unsupported docs fall back to exact."""
    img, _info = render_with_info(
        doc,
        resolution=resolution,
        supersample=supersample,
        background=background,
        width_scale=width_scale,
        allow_fallback=True,
    )
    return img


def render_auto(
    doc: dict,
    *,
    resolution: int,
    quality: str = "fast",
    return_info: bool = False,
    **kwargs: Any,
):
    """Render with an explicit quality choice (``fast`` may fall back to exact)."""
    quality = str(quality or "fast").lower()
    if quality not in ("fast", "exact"):
        raise ValueError(f"quality 非法（fast|exact）: {quality!r}")
    if quality == "exact":
        ws = _resolve_width_scale(doc, kwargs.get("width_scale"))
        started = time.perf_counter()
        img = _exact_render(
            doc,
            resolution=resolution,
            supersample=max(1, min(8, int(kwargs.get("supersample", 2)))),
            background=kwargs.get("background"),
            width_scale=ws,
        )
        info = {
            "quality": "exact",
            "fallback_reason": [],
            "approximations": [],
            "supersample": max(1, min(8, int(kwargs.get("supersample", 2)))),
            "width_scale": ws,
            "resolution": int(resolution),
            "size": [int(img.shape[1]), int(img.shape[0])],
            "shapes": _shape_histogram(doc),
            "strokes": None,
            "fills": None,
            "skipped": None,
            "elapsed_s": time.perf_counter() - started,
        }
        global LAST_INFO
        LAST_INFO = info
        return (img, info) if return_info else img
    img, info = render_with_info(
        doc,
        resolution=resolution,
        supersample=int(kwargs.get("supersample", 2)),
        background=kwargs.get("background"),
        width_scale=kwargs.get("width_scale"),
        allow_fallback=True,
    )
    return (img, info) if return_info else img


# ---------------------------------------------------------------------------
# comparison / bench
# ---------------------------------------------------------------------------
def load_image(value) -> np.ndarray:
    """Accept an ndarray or a path-like PNG/JPEG and return an ``(H,W,3)`` uint8 array."""
    if isinstance(value, np.ndarray):
        arr = value
    else:
        with Image.open(value) as img:
            arr = np.asarray(img.convert("RGB"))
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    if arr.ndim != 3:
        raise ValueError(f"图像维度非法: {arr.shape!r}")
    if arr.shape[2] == 4:
        arr = arr[..., :3]
    if arr.shape[2] != 3:
        raise ValueError(f"需要 RGB/RGBA 图像，收到 {arr.shape!r}")
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(arr)


def compare_images(a, b) -> dict:
    """Compare two RGB images (arrays or paths); see module docstring for the gate."""
    ia = load_image(a)
    ib = load_image(b)
    if ia.shape != ib.shape:
        raise ValueError(f"图像尺寸不一致: {ia.shape} vs {ib.shape}")
    d = np.abs(ia.astype(np.int16) - ib.astype(np.int16))
    mse = float(np.mean((ia.astype(np.float64) - ib.astype(np.float64)) ** 2))
    psnr = 99.0 if mse <= 1e-12 else float(10.0 * math.log10(255.0 * 255.0 / mse))
    max_abs = int(d.max()) if d.size else 0
    return {
        "max_abs_diff": max_abs,
        "mean_abs_diff": float(d.mean()) if d.size else 0.0,
        "psnr_db": round(psnr, 3),
        "within1_pct": round(float((d <= 1).mean() * 100.0), 4) if d.size else 100.0,
        "within2_pct": round(float((d <= 2).mean() * 100.0), 4) if d.size else 100.0,
        "parity_pass": bool(max_abs <= PARITY_MAX_ABS_DIFF and psnr >= PARITY_PSNR_DB),
    }


def bench(
    doc: dict,
    *,
    resolution: int = 512,
    repeats: int = 2,
    include_exact: bool = False,
    supersample: int = 2,
    width_scale: float | None = None,
) -> dict:
    """Measure fast render wall time / peak allocation; optionally parity vs exact."""
    repeats = max(1, int(repeats))
    fast_times: list[float] = []
    img = None
    info: dict[str, Any] = {}
    # wall-clock timings are measured *without* tracemalloc (it distorts hot
    # numpy/PIL allocation paths by 2-3x); memory is probed in a separate pass.
    for _ in range(repeats):
        t0 = time.perf_counter()
        img, info = render_with_info(
            doc,
            resolution=resolution,
            supersample=supersample,
            width_scale=width_scale,
            allow_fallback=True,
        )
        fast_times.append(time.perf_counter() - t0)
    tracemalloc.start()
    render_with_info(
        doc,
        resolution=resolution,
        supersample=supersample,
        width_scale=width_scale,
        allow_fallback=True,
    )
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    ordered = sorted(fast_times)
    result: dict[str, Any] = {
        "resolution": int(resolution),
        "repeats": repeats,
        "quality": info.get("quality"),
        "fallback_reason": info.get("fallback_reason") or [],
        "size": info.get("size"),
        "fast_s": [round(v, 4) for v in fast_times],
        "fast_p50_s": round(ordered[(len(ordered) - 1) // 2], 4),
        "fast_min_s": round(ordered[0], 4),
        "peak_mem_mb": round(peak / (1024 * 1024), 2),
    }
    if include_exact and img is not None:
        t0 = time.perf_counter()
        exact = render_auto(
            doc,
            resolution=resolution,
            quality="exact",
            supersample=supersample,
            width_scale=width_scale,
        )
        exact_dt = time.perf_counter() - t0
        result["exact_s"] = round(exact_dt, 4)
        result["parity"] = compare_images(img, exact)
    return result


# ---------------------------------------------------------------------------
# preview/frame truncation (shared by the CLI and the tools package)
# ---------------------------------------------------------------------------
def _render_order(strokes: list[dict]) -> list[int]:
    return sorted(
        range(len(strokes)),
        key=lambda i: (
            int(strokes[i].get("layer", 0) or 0),
            0 if strokes[i].get("fill_role") == "region_fill" else 1,
            i,
        ),
    )


def truncate_doc(doc: dict, *, fraction: float | None = None, at: int | None = None) -> dict:
    """Copy ``doc`` keeping only the first ``at`` elements (or the first ``fraction``).

    Elements are selected in render order (layers/stroke order), so a truncated
    render contains exactly the prefix the full render would have drawn first.
    The source document is never mutated.
    """
    strokes = [st for st in (doc.get("strokes") or []) if isinstance(st, dict)]
    total = len(strokes)
    if fraction is not None:
        frac = float(fraction)
        if not (0.0 <= frac <= 1.0) or not math.isfinite(frac):
            raise ValueError(f"fraction 非法（0..1）: {fraction!r}")
        keep = int(math.ceil(frac * total))
    elif at is not None:
        keep = max(0, int(at))
    else:
        raise ValueError("truncate_doc 需要 fraction 或 at")
    keep = min(keep, total)
    selected = set(_render_order(strokes)[:keep])
    out = copy.copy(doc)
    out["strokes"] = [st for i, st in enumerate(strokes) if i in selected]
    return out
