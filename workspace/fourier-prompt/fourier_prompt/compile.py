"""FDSL ``fdsl/2.1`` plan → skill document compiler (WP-D).

The compiler turns semantic plan elements into the skill's element records and
delegates *all* Fourier/brush/document assembly to WP-B's
``fbs_core.document.assemble_document`` through
:mod:`fourier_prompt.compat.skill_api`.  It never re-implements Fourier
fitting, brush curves, style de-duplication or `.fbs` assembly.

Element mapping
---------------
* ``stroke``        → open/closed polyline → Fourier stroke (``fit_curve``);
* ``fourier_gen``   → explicit contour; closed (default) → **filled polygon**,
  ``closed: false`` → Fourier stroke;
* ``fill``          → explicit polygon (+ optional holes);
* ``text``          → glyph outlines baked to fills (WP-K, outline mode).

Text compilation is additive: batch-2/3 plans used the old built-in font catalog
(Inter, Caveat, ...) which is not shipped, and integration fixtures assert
``text_skipped``.  ``text_mode="auto"`` therefore compiles text with a batch-5
font id, no font id, or an explicit ``as`` field, while legacy unshipped built-in
ids keep the skip-with-warning path.  ``text_mode="outline"`` compiles every text
element / ``texts[]`` entry; ``text_mode="legacy"`` skips all text.  When
compilation is requested, a missing font raises ``FP_E_TEXT_FONT`` and an
uncovered character raises ``FP_E_TEXT_GLYPH`` -- never a silent skip and never a
system-font fallback.  ``font=`` optionally overrides the packaged
``assets/fonts/NotoSansSC-VF.ttf``.

Transforms (``center``/``anchor``/``scale``/``rotate``) are applied to points
in canvas pixels before the world-unit conversion.  Errors are fail-loud:
unknown gen/brush/kind, invalid parameters and degenerate geometry raise
:class:`CompileError`.
"""

from __future__ import annotations

import copy
import math
import time

import numpy as np

from . import generators
from .compat import skill_api
from .generators import GenError
from .legacy import from_legacy, is_legacy
from .schema import (
    FDSL_SCHEMA,
    FDSchemaError,
    validate_plan_detailed,
)

__all__ = ["CompileError", "compile_plan", "normalized_fbs"]

_EPSILON_PX = 0.5
_DEFAULT_COLOR = "#000000"
_DEFAULT_WIDTH = 2.0
_HEX = "0123456789abcdef"


class CompileError(FDSchemaError):
    """Structured compile failure (``FP_E_COMPILE_*`` codes)."""


#: ``compile_plan(text_mode=...)`` values (see module docstring).
_TEXT_MODES = ("auto", "outline", "legacy")
#: Font ids that resolve to the packaged Noto Sans SC subset (batch 5).
_TEXT_FONT_IDS = {"noto_sans_sc", "noto_sans_cjk_sc", "source_han_sans_sc"}


def _text_handle(font_override, path: str):
    """Load the packaged font (or the explicit override); map errors to CompileError."""
    from . import fonts

    try:
        return fonts.load_font(font_override)
    except fonts.FontError as exc:
        raise CompileError(exc.code, exc.message, f"{path}.font") from exc


def _text_handle_for(element: dict, font_override, path: str):
    """Resolve the font for one text element, rejecting unshipped font ids."""
    if font_override is not None:
        return _text_handle(font_override, path)
    spec = element.get("font") if isinstance(element.get("font"), dict) else {}
    font_id = spec.get("id")
    if font_id is not None and str(font_id) not in _TEXT_FONT_IDS:
        raise CompileError(
            "FP_E_TEXT_FONT",
            f"font id {font_id!r} is not shipped in batch 5 (available: "
            f"{sorted(_TEXT_FONT_IDS)}); pass compile_plan(font=PATH) to use another OFL font",
            f"{path}.font.id",
        )
    return _text_handle(None, path)


def _text_enabled(element: dict, text_mode: str) -> bool:
    """Auto mode: compile explicit outline text, shipped fonts and default fonts.

    Batch-2/3 plans may omit ``as``; those used the old built-in font catalog
    (Inter, Caveat, ...) which batch 5 does not ship.  Such elements keep the
    legacy skip-with-warning behaviour so existing fixtures stay valid.  Text
    with a batch-5 font id (or no font id at all) compiles without needing an
    explicit ``as`` field.
    """
    if text_mode == "legacy":
        return False
    if text_mode == "outline":
        return True
    if "as" in element:
        return True
    spec = element.get("font") if isinstance(element.get("font"), dict) else {}
    font_id = spec.get("id")
    if font_id is None or str(font_id) in _TEXT_FONT_IDS:
        return True
    from .schema import BUILTIN_FONTS

    return str(font_id) not in BUILTIN_FONTS


def _bake_text_fills(element: dict, handle, canvas: dict, path: str, warnings: list) -> list:
    """Call ``text.text_fills`` and re-raise text failures as :class:`CompileError`."""
    from . import text as text_mod

    mode = element.get("as", "outline")
    if mode == "glyph":
        raise CompileError(
            "FP_E_TEXT_MODE",
            "as='glyph' (embedded font rasterisation) is not implemented in batch 5; "
            "use as='outline'",
            f"{path}.as",
        )
    try:
        return text_mod.text_fills(element, handle, canvas=canvas, warnings=warnings)
    except text_mod.TextError as exc:
        raise CompileError(exc.code, exc.message, exc.path or path) from exc


def _text_fill_records(fills, *, long_side, layer, path, palette, region_id):
    """Feed baked text fills through the existing fill-record path; returns (records, next_id)."""
    records = []
    for index, fill in enumerate(fills):
        points = _points_array(fill.get("poly"), f"{path}.fills[{index}].poly", 3)
        holes = []
        for hole_index, hole in enumerate(fill.get("holes") or []):
            holes.append(_points_array(hole, f"{path}.fills[{index}].holes[{hole_index}]", 3))
        records.append(
            _fill_record(
                points,
                holes,
                fill,
                long_side=long_side,
                layer=layer,
                region_id=region_id,
                path=f"{path}.fills[{index}]",
                palette=palette,
            )
        )
        region_id += 1
    return records, region_id



# ---------------------------------------------------------------------------
# Colour / palette
# ---------------------------------------------------------------------------


def _palette_map(palette) -> dict:
    if isinstance(palette, dict):
        return {str(k): v for k, v in palette.items() if isinstance(v, str)}
    return {}


def _hex_rgb(value: str) -> tuple[np.ndarray, float]:
    text = value.strip().lower()
    if not text.startswith("#"):
        raise ValueError(f"not a hex colour: {value!r}")
    body = text[1:]
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    if len(body) not in (6, 8) or any(ch not in _HEX for ch in body):
        raise ValueError(f"not a hex colour: {value!r}")
    rgb = np.asarray([int(body[i : i + 2], 16) / 255.0 for i in (0, 2, 4)], dtype=np.float64)
    alpha = int(body[6:8], 16) / 255.0 if len(body) == 8 else 1.0
    return np.clip(rgb, 0.0, 1.0), float(alpha)


def _sequence_rgb(value) -> tuple[np.ndarray, float]:
    if not isinstance(value, (list, tuple)) or len(value) not in (3, 4):
        raise ValueError("colour list must be [r, g, b] or [r, g, b, a]")
    values = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            raise ValueError(f"invalid colour component {item!r}")
        values.append(float(item))
    rgb = np.asarray(values[:3], dtype=np.float64)
    if np.max(np.abs(rgb)) > 1.5:
        rgb = rgb / 255.0
    alpha = values[3] / 255.0 if len(values) == 4 and values[3] > 1.5 else (values[3] if len(values) == 4 else 1.0)
    return np.clip(rgb, 0.0, 1.0), float(min(max(alpha, 0.0), 1.0))


def _resolve_color(spec, palette: dict, path: str) -> tuple[np.ndarray, float]:
    """FDSL colour (palette name / hex / RGB / RGBA) → ``(rgb 0..1, alpha)``."""
    if spec is None:
        spec = "ink" if "ink" in palette else _DEFAULT_COLOR
    if isinstance(spec, str):
        text = spec.strip()
        if text in palette:
            text = str(palette[text]).strip()
        try:
            return _hex_rgb(text)
        except ValueError as exc:
            raise CompileError(
                "FP_E_COLOR",
                f"unknown colour {spec!r} (not a palette name or hex colour)",
                path,
            ) from exc
    try:
        return _sequence_rgb(spec)
    except ValueError as exc:
        raise CompileError("FP_E_COLOR", str(exc), path) from exc


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _points_array(raw, path: str, minimum: int) -> np.ndarray:
    try:
        points = np.asarray(raw, dtype=np.float64).reshape(-1, 2)
    except (TypeError, ValueError) as exc:
        raise CompileError("FP_E_COMPILE_GEOMETRY", f"invalid point list: {exc}", path) from exc
    if len(points) < minimum:
        raise CompileError(
            "FP_E_COMPILE_GEOMETRY", f"at least {minimum} points required (got {len(points)})", path
        )
    if not np.all(np.isfinite(points)):
        raise CompileError("FP_E_COMPILE_GEOMETRY", "points contain non-finite values", path)
    return points


def _apply_transform(points: np.ndarray, transform: dict | None, path: str) -> np.ndarray:
    if not transform:
        return points
    skew = transform.get("skew")
    if skew is not None and abs(float(skew)) > 1e-12:
        raise CompileError(
            "FP_E_COMPILE_TRANSFORM", "transform.skew is not supported by the batch-2 compiler", path
        )
    pivot = transform.get("center", transform.get("anchor"))
    if pivot is None:
        pivot = points.mean(axis=0)
    try:
        pivot = np.asarray(pivot, dtype=np.float64).reshape(2)
    except (TypeError, ValueError) as exc:
        raise CompileError("FP_E_COMPILE_TRANSFORM", "transform.center must be [x, y]", path) from exc
    scale = transform.get("scale", 1.0)
    if isinstance(scale, (list, tuple)):
        if len(scale) != 2:
            raise CompileError("FP_E_COMPILE_TRANSFORM", "transform.scale pair must have 2 entries", path)
        scale_arr = np.asarray([float(scale[0]), float(scale[1])], dtype=np.float64)
    else:
        scale_arr = np.asarray([float(scale), float(scale)], dtype=np.float64)
    if not np.all(np.isfinite(scale_arr)) or np.any(scale_arr <= 0.0):
        raise CompileError("FP_E_COMPILE_TRANSFORM", "transform.scale must be > 0", path)
    rotate = math.radians(float(transform.get("rotate", 0.0)))
    cos_r, sin_r = math.cos(rotate), math.sin(rotate)
    delta = (points - pivot) * scale_arr
    rotated = np.stack(
        [
            delta[:, 0] * cos_r - delta[:, 1] * sin_r,
            delta[:, 0] * sin_r + delta[:, 1] * cos_r,
        ],
        axis=1,
    )
    return pivot + rotated


def _arclen_s(points: np.ndarray, closed: bool) -> np.ndarray:
    count = len(points)
    if closed:
        nxt = np.roll(points, -1, axis=0)
    else:
        nxt = points[1:]
    segment = np.linalg.norm(nxt - points[: len(nxt)], axis=1)
    cumulative = np.concatenate([[0.0], np.cumsum(segment)])
    total = float(cumulative[-1])
    if total <= 1e-15:
        return np.linspace(0.0, 1.0, count)
    return cumulative[:-1] / total if closed else cumulative / total


def _widths_world(spec, s: np.ndarray, long_side: float, path: str) -> np.ndarray:
    if spec is None:
        spec = _DEFAULT_WIDTH
    if isinstance(spec, bool):
        raise CompileError("FP_E_COMPILE_BRUSH", "brush.width must be a number or [[t, w], ...]", path)
    if isinstance(spec, (int, float)):
        width_px = np.full(len(s), float(spec))
    else:
        try:
            pairs = np.asarray(spec, dtype=np.float64).reshape(-1, 2)
        except (TypeError, ValueError) as exc:
            raise CompileError(
                "FP_E_COMPILE_BRUSH", "brush.width curve must be [[t, w], ...]", path
            ) from exc
        if len(pairs) < 2:
            raise CompileError("FP_E_COMPILE_BRUSH", "brush.width curve needs >= 2 points", path)
        order = np.argsort(pairs[:, 0], kind="stable")
        width_px = np.interp(s, pairs[order, 0], pairs[order, 1])
    if not np.all(np.isfinite(width_px)):
        raise CompileError("FP_E_COMPILE_BRUSH", "brush.width contains non-finite values", path)
    return np.clip(width_px, 0.0, None) / float(long_side)


def _polygon_spread(points: np.ndarray) -> float:
    """2-D covariance determinant: > 0 only when the polygon is not collinear.

    Shoelace area cannot be used as a validity test: self-intersecting curves
    (e.g. a Lissajous fill) may have cancelling signed lobes while still
    enclosing a large 2-D region.
    """
    if len(points) < 3:
        return 0.0
    centered = points - points.mean(axis=0)
    cov = centered.T @ centered / float(len(points))
    return float(cov[0, 0] * cov[1, 1] - cov[0, 1] * cov[1, 0])


def _layer_index(ref, layers: list) -> int:
    if isinstance(ref, bool):
        return 0
    if isinstance(ref, int):
        return int(ref)
    if isinstance(ref, str):
        for index, layer in enumerate(layers):
            if isinstance(layer, dict) and layer.get("id") == ref:
                return index
    return 0


# ---------------------------------------------------------------------------
# Element records
# ---------------------------------------------------------------------------


def _base_style(element: dict, path: str, palette: dict):
    brush = element.get("brush") or {}
    color_spec = brush.get("color", element.get("color"))
    rgb, color_alpha = _resolve_color(color_spec, palette, f"{path}.brush.color")
    alpha_value = brush.get("alpha", 1.0)
    if isinstance(alpha_value, bool) or not isinstance(alpha_value, (int, float)):
        raise CompileError("FP_E_COMPILE_BRUSH", "brush.alpha must be a number", f"{path}.brush.alpha")
    alpha = float(alpha_value) * color_alpha
    opacity = element.get("opacity", 1.0)
    if isinstance(opacity, (int, float)) and not isinstance(opacity, bool):
        alpha *= float(opacity)
    alpha = float(min(max(alpha, 0.0), 1.0))
    preset = brush.get("preset")
    if preset is not None:
        try:
            shape = brush.get("shape") or skill_api.preset_shape(str(preset))
        except skill_api.SkillApiError as exc:
            raise CompileError("FP_E_COMPILE_BRUSH", str(exc), f"{path}.brush.preset") from exc
    else:
        shape = brush.get("shape") or "round"
    width = brush.get("width", _DEFAULT_WIDTH)
    return rgb, alpha, str(shape), width


def _fill_record(points: np.ndarray, holes, element, *, long_side, layer, region_id, path, palette):
    rgb, alpha, _shape, _width = _base_style(element, path, palette)
    world = points / long_side
    if _polygon_spread(world) <= 1e-16:
        raise CompileError("FP_E_COMPILE_GEOMETRY", "fill polygon is degenerate (zero 2-D spread)", path)
    count = len(world)
    s = _arclen_s(world, True)
    world_holes = None
    if holes:
        world_holes = [np.asarray(hole, dtype=np.float64).reshape(-1, 2) / long_side for hole in holes]
        for index, hole in enumerate(world_holes):
            if len(hole) < 3:
                raise CompileError(
                    "FP_E_COMPILE_GEOMETRY", "fill hole needs >= 3 points", f"{path}.holes[{index}]"
                )
    return {
        "points": world,
        "s": s,
        "closed": True,
        "widths": np.zeros(count),
        "colors": np.tile(rgb.reshape(1, 3), (count, 1)),
        "alphas": np.full(count, alpha),
        "shapes": ["round"] * count,
        "fill_role": "region_fill",
        "fill_poly": world,
        "fill_holes": world_holes,
        "region_id": int(region_id),
        "layer": int(layer),
    }


def _stroke_record(points: np.ndarray, element, *, long_side, layer, path, palette, closed):
    rgb, alpha, shape, width_spec = _base_style(element, path, palette)
    world = points / long_side
    count = len(world)
    if count < 2:
        raise CompileError("FP_E_COMPILE_GEOMETRY", "stroke needs >= 2 points", path)
    s = _arclen_s(world, closed)
    widths = _widths_world(width_spec, s, long_side, f"{path}.brush.width")
    return {
        "points": world,
        "s": s,
        "closed": bool(closed),
        "widths": widths,
        "colors": np.tile(rgb.reshape(1, 3), (count, 1)),
        "alphas": np.full(count, alpha),
        "shapes": [shape] * count,
        "fill_role": "medial_branch",
        "fill_poly": None,
        "fill_holes": None,
        "region_id": -1,
        "layer": int(layer),
    }


def _contour(gen: str, params: dict, samples: int, path: str) -> np.ndarray:
    try:
        points = generators.contour(gen, params, samples=samples)
    except GenError as exc:
        code = "FP_E_GEN_UNKNOWN" if "unknown generator" in str(exc) else "FP_E_GEN_PARAMS"
        raise CompileError(code, str(exc), f"{path}.gen") from exc
    return np.asarray(points, dtype=np.float64).reshape(-1, 2)


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------


def compile_plan(
    plan: dict,
    *,
    resolution: int = 256,
    base_color=None,
    fill_soft_px=None,
    method=None,
    legacy: bool | None = None,
    samples: int = 256,
    font=None,
    text_mode: str = "auto",
) -> dict:
    """Compile a canonical (or early draft) plan into a skill ``.fbs`` document.

    Returns ``{"fbs": dict, "stats": {...}, "warnings": [...], "ledger": {...}}``.
    The returned document has been validated by ``io_format.write_fbs``; it is
    renderable by ``render.render`` at ``resolution``.

    ``font`` overrides the packaged text font (path or ``fonts.FontHandle``);
    ``text_mode`` is ``"auto"`` (default: compile shipped/default fonts and
    explicit ``as`` text; legacy unshipped built-in ids are skipped with a
    warning), ``"outline"`` (all text) or ``"legacy"`` (skip all text).
    """
    started = time.perf_counter()
    if not isinstance(plan, dict):
        raise CompileError("FP_E_PLAN_TYPE", "plan must be a JSON object", "$")
    if text_mode not in _TEXT_MODES:
        raise CompileError(
            "FP_E_COMPILE_TEXT", f"text_mode must be one of {list(_TEXT_MODES)}", "$"
        )
    if isinstance(resolution, bool) or not isinstance(resolution, int):
        raise CompileError("FP_E_COMPILE_RESOLUTION", "resolution must be an integer", "$")
    if not 16 <= resolution <= 4096:
        raise CompileError("FP_E_COMPILE_RESOLUTION", "resolution must be in [16, 4096]", "$")
    if isinstance(samples, bool) or not isinstance(samples, int) or not 8 <= samples <= 8192:
        raise CompileError("FP_E_COMPILE_SAMPLES", "samples must be an integer in [8, 8192]", "$")

    warnings: list[dict] = []
    use_legacy = is_legacy(plan) if legacy is None else bool(legacy)
    if use_legacy:
        plan21, legacy_warnings = from_legacy(plan, samples=samples)
        warnings.extend(legacy_warnings)
    else:
        plan21 = copy.deepcopy(plan)

    report = validate_plan_detailed(plan21)
    warnings.extend(report["warnings"])
    if not report["ok"]:
        first = report["errors"][0]
        raise CompileError(
            first.get("code", "FP_E_COMPILE_SCHEMA"),
            f"{first.get('message', 'plan is invalid')} "
            f"(and {len(report['errors']) - 1} more schema error(s))",
            first.get("path", "$"),
        )

    canvas = plan21.get("canvas") or {}
    width = int(canvas.get("width", 0))
    height = int(canvas.get("height", 0))
    long_side = float(max(width, height))
    if long_side <= 0:  # pragma: no cover - schema guarantees this
        raise CompileError("FP_E_COMPILE_CANVAS", "canvas has no usable size", "$.canvas")
    palette = _palette_map(plan21.get("palette"))
    layers = plan21.get("layers") or []
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        if layer.get("visible") is False:
            warnings.append(
                {
                    "severity": "warning",
                    "code": "FP_W_LAYER_VISIBLE_UNSUPPORTED",
                    "path": "$.layers",
                    "message": f"layer {layer.get('id')!r} is marked invisible; the .fbs stroke model has no per-layer visibility, so its elements are kept",
                }
            )
        opacity = layer.get("opacity", 1.0)
        if isinstance(opacity, (int, float)) and not isinstance(opacity, bool) and abs(float(opacity) - 1.0) > 1e-12:
            warnings.append(
                {
                    "severity": "warning",
                    "code": "FP_W_LAYER_OPACITY_UNSUPPORTED",
                    "path": "$.layers",
                    "message": f"layer {layer.get('id')!r} opacity {opacity} is not applied (baked per-element alpha only)",
                }
            )
    meta = plan21.get("meta") if isinstance(plan21.get("meta"), dict) else {}
    legacy_render = meta.get("legacy_render") if isinstance(meta.get("legacy_render"), dict) else {}
    if fill_soft_px is None:
        fill_soft_px = legacy_render.get("fill_soft_px", 0.5)
    fill_soft_px = float(fill_soft_px)
    if not math.isfinite(fill_soft_px) or fill_soft_px < 0.0:
        raise CompileError("FP_E_COMPILE_FILL", "fill_soft_px must be a finite number >= 0", "$")
    chosen_method = str(method or "fdsl_compiler")

    elements: list[dict] = []
    skipped: list[dict] = []
    region_id = 0
    counts = {
        "gen_elements": 0,
        "gen_instances": 0,
        "stroke": 0,
        "fill": 0,
        "text": 0,
        "text_fills": 0,
        "text_skipped": 0,
    }

    for index, element in enumerate(plan21.get("elements") or []):
        path = f"$.elements[{index}]"
        kind = element.get("kind", element.get("type"))
        if element.get("visible") is False:
            skipped.append({"index": index, "kind": str(kind), "reason": "visible=false"})
            warnings.append(
                {
                    "severity": "warning",
                    "code": "FP_W_ELEMENT_HIDDEN",
                    "path": path,
                    "message": "element.visible is false; skipped by the compiler",
                }
            )
            continue
        layer = _layer_index(element.get("layer", 0), layers)
        if kind == "text":
            if not _text_enabled(element, text_mode):
                counts["text_skipped"] += 1
                skipped.append(
                    {"index": index, "kind": "text", "reason": "text rendering not requested"}
                )
                warnings.append(
                    {
                        "severity": "warning",
                        "code": "FP_W_TEXT_SKIPPED",
                        "path": path,
                        "message": (
                            "text element uses a legacy built-in font id that batch 5 does not "
                            "ship; set as='outline' with a shipped font id (or no font.id), or "
                            "compile_plan(text_mode='outline', font=PATH)"
                        ),
                    }
                )
                continue
            handle = _text_handle_for(element, font, path)
            text_fills = _bake_text_fills(element, handle, canvas, path, warnings)
            records, region_id = _text_fill_records(
                text_fills,
                long_side=long_side,
                layer=layer,
                path=path,
                palette=palette,
                region_id=region_id,
            )
            elements.extend(records)
            counts["text"] += 1
            counts["text_fills"] += len(records)
            counts["fill"] += len(records)
            continue
        if kind == "stroke":
            points = _points_array(element.get("points"), f"{path}.points", 2)
            if element.get("transform"):
                points = _apply_transform(points, element.get("transform"), f"{path}.transform")
            closed = bool(element.get("closed", False))
            elements.append(
                _stroke_record(
                    points,
                    element,
                    long_side=long_side,
                    layer=layer,
                    path=path,
                    palette=palette,
                    closed=closed,
                )
            )
            counts["stroke"] += 1
            continue
        if kind == "fill":
            points = _points_array(element.get("poly"), f"{path}.poly", 3)
            if element.get("transform"):
                points = _apply_transform(points, element.get("transform"), f"{path}.transform")
            holes = []
            if element.get("holes"):
                if not isinstance(element["holes"], list):
                    raise CompileError("FP_E_COMPILE_GEOMETRY", "holes must be a list", f"{path}.holes")
                for hole_index, hole in enumerate(element["holes"]):
                    hole_points = _points_array(
                        hole, f"{path}.holes[{hole_index}]", 3
                    )
                    if element.get("transform"):
                        hole_points = _apply_transform(
                            hole_points, element.get("transform"), f"{path}.transform"
                        )
                    holes.append(hole_points)
            elements.append(
                _fill_record(
                    points,
                    holes,
                    element,
                    long_side=long_side,
                    layer=layer,
                    region_id=region_id,
                    path=path,
                    palette=palette,
                )
            )
            region_id += 1
            counts["fill"] += 1
            continue
        if kind == "fourier_gen":
            counts["gen_elements"] += 1
            gen = str(element.get("gen", ""))
            params = element.get("params") or {}
            points = _contour(gen, params, samples, path)
            if element.get("transform"):
                points = _apply_transform(points, element.get("transform"), f"{path}.transform")
            closed = element.get("closed", None)
            if closed is None:
                try:
                    closed = not generators.is_open(gen)
                except GenError as exc:  # pragma: no cover - _contour already validated
                    raise CompileError("FP_E_GEN_UNKNOWN", str(exc), f"{path}.gen") from exc
            closed = bool(closed)
            if closed:
                elements.append(
                    _fill_record(
                        points,
                        None,
                        element,
                        long_side=long_side,
                        layer=layer,
                        region_id=region_id,
                        path=path,
                        palette=palette,
                    )
                )
                region_id += 1
                counts["fill"] += 1
            else:
                elements.append(
                    _stroke_record(
                        points,
                        element,
                        long_side=long_side,
                        layer=layer,
                        path=path,
                        palette=palette,
                        closed=False,
                    )
                )
                counts["stroke"] += 1
            counts["gen_instances"] += 1
            continue
        raise CompileError(
            "FP_E_ELEMENT_KIND",
            f"unsupported element kind {kind!r}; expected fourier_gen/stroke/fill/text",
            f"{path}.kind",
        )

    # Plan-level texts[] (D6): the explicit text layer uses the same outline path.
    for index, entry in enumerate(plan21.get("texts") or []):
        path = f"$.texts[{index}]"
        if not isinstance(entry, dict):  # schema already rejected non-objects
            continue
        if entry.get("visible") is False:
            warnings.append(
                {
                    "severity": "warning",
                    "code": "FP_W_ELEMENT_HIDDEN",
                    "path": path,
                    "message": "text.visible is false; skipped by the compiler",
                }
            )
            continue
        if text_mode == "legacy":
            counts["text_skipped"] += 1
            skipped.append({"index": index, "kind": "text", "reason": "text rendering not requested"})
            warnings.append(
                {
                    "severity": "warning",
                    "code": "FP_W_TEXT_SKIPPED",
                    "path": path,
                    "message": "texts[] entry was not compiled (text_mode='legacy')",
                }
            )
            continue
        if entry.get("as") == "glyph":
            raise CompileError(
                "FP_E_TEXT_MODE",
                "as='glyph' (embedded font rasterisation) is not implemented in batch 5; "
                "use as='outline'",
                f"{path}.as",
            )
        handle = _text_handle_for(entry, font, path)
        text_fills = _bake_text_fills(entry, handle, canvas, path, warnings)
        records, region_id = _text_fill_records(
            text_fills,
            long_side=long_side,
            layer=_layer_index(entry.get("layer", 0), layers),
            path=path,
            palette=palette,
            region_id=region_id,
        )
        elements.extend(records)
        counts["text"] += 1
        counts["text_fills"] += len(records)
        counts["fill"] += len(records)

    if not elements:
        raise CompileError(
            "FP_E_COMPILE_EMPTY",
            "plan produced no drawable elements (all skipped or empty)",
            "$.elements",
        )

    # Background colour (canvas.background or explicit override).
    background_spec = base_color if base_color is not None else canvas.get("background")
    if background_spec is None:
        background_rgb = np.asarray([1.0, 1.0, 1.0])
        background_alpha = 1.0
    else:
        background_rgb, background_alpha = _resolve_color(background_spec, palette, "$.canvas.background")
    base = [int(round(v * 255.0)) for v in background_rgb] + [int(round(background_alpha * 255.0))]

    warn = skill_api.warning_log()
    source = {
        "path": "",
        "sha256": "",
        "width": width,
        "height": height,
        "mode": "fdsl",
        "scale_applied": 1.0,
        "plan_schema": str(plan21.get("schema", "")),
        "plan_name": str(plan21.get("name", "")),
    }
    decomposition = {
        "method": chosen_method,
        "params": {
            "fill_soft_px": float(fill_soft_px),
            "source": FDSL_SCHEMA,
            "samples": int(samples),
        },
    }
    try:
        doc, info = skill_api.assemble_document(
            elements,
            cfg=skill_api.fbs_config(resolution, _EPSILON_PX),
            warn=warn,
            source=source,
            image_class={"klass": "text_plan", "evidence": {}},
            canvas={
                "width": float(width / long_side),
                "height": float(height / long_side),
                "long_side_units": 1.0,
                "origin": "top-left",
                "y_axis": "down",
            },
            decomposition=decomposition,
            base_color=base,
            fill_soft_px=float(fill_soft_px),
            method=chosen_method,
            record_ids="sequential",
        )
    except Exception as exc:  # noqa: BLE001 - structured skill errors are contract failures
        if skill_api.is_skill_error(exc):
            raise CompileError("FP_E_COMPILE_SKILL", f"skill assembly failed: {exc}") from exc
        raise
    # Pin the render width scale for compiled FDSL. Without this, the skill renderer
    # auto-applies 0.95 for complex lineart/flat_art docs (evidence-based), making
    # .fbs/.fdraw output renderer-dependent. FDSL compiles explicit widths, so 1.0 is
    # the correct, reproducible contract.
    doc.setdefault("render_contract", {})["width_scale"] = 1.0
    # Additive layer metadata (WP-I): names/visibility/opacity from the plan.
    # Rendering semantics are unchanged; the .fbs writer preserves this extra
    # top-level field so exports can name PSD/KRA layers.
    from .layers import LayerError, add_layer_metadata

    try:
        add_layer_metadata(doc, plan21)
    except LayerError as exc:  # fail-loud, never drop a declared layer silently
        raise CompileError(
            "FP_E_COMPILE_LAYER", f"layer metadata rejected: {exc.message}", "$.layers"
        ) from exc
    for entry in warn.to_list():
        warnings.append(
            {
                "severity": entry.get("severity", "warn"),
                "code": entry.get("code", "W_UNKNOWN"),
                "path": "$",
                "message": entry.get("message", ""),
            }
        )

    # Validate through the skill writer (quantize + structural validation).
    try:
        fbs_bytes = skill_api.write_fbs(doc)
    except Exception as exc:  # noqa: BLE001
        if skill_api.is_skill_error(exc):
            raise CompileError("FP_E_COMPILE_FBS", f".fbs validation failed: {exc}") from exc
        raise

    coef_rows = sum(len(stroke.get("coef") or []) for stroke in doc.get("strokes") or [])
    coef_nonzero = sum(
        1
        for stroke in doc.get("strokes") or []
        for row in stroke.get("coef") or []
        if abs(float(row[1])) + abs(float(row[2])) > 0.0
    )
    n_max = max((int(stroke.get("N", 0)) for stroke in doc.get("strokes") or []), default=0)
    ledger = {
        "gen": counts["gen_elements"],
        "gen_instances": counts["gen_instances"],
        "stroke": counts["stroke"],
        "fill": counts["fill"],
        "text": counts["text"],
        "text_fills": counts["text_fills"],
        "text_skipped": counts["text_skipped"],
        "records": len(doc.get("strokes") or []),
        "styles": len(doc.get("brush_styles") or []),
        "skipped": len(skipped),
        "coef_rows": coef_rows,
        "coef_nonzero": coef_nonzero,
        "N_max": n_max,
        "fft_passes": int(doc.get("stats", {}).get("fft_passes", 0)),
    }
    stats = {
        "name": str(plan21.get("name", "")),
        "schema": str(plan21.get("schema", "")),
        "legacy": bool(use_legacy),
        "resolution": int(resolution),
        "canvas": {"width": width, "height": height},
        "elements": len(plan21.get("elements") or []),
        "records": ledger["records"],
        "gen": counts["gen_elements"],
        "gen_instances": counts["gen_instances"],
        "stroke": counts["stroke"],
        "fill": counts["fill"],
        "text": counts["text"],
        "text_fills": counts["text_fills"],
        "text_skipped": counts["text_skipped"],
        "styles": ledger["styles"],
        "skipped": skipped,
        "warnings": len(warnings),
        "fft_passes": ledger["fft_passes"],
        "coef_rows": coef_rows,
        "fbs_bytes": len(fbs_bytes),
        "compile_seconds": round(time.perf_counter() - started, 4),
        "assemble_seconds": round(float(info.get("timings", {}).get("stage2_3_fourier_brush", 0.0)), 4),
    }
    return {"fbs": doc, "stats": stats, "warnings": warnings, "ledger": ledger}


# ---------------------------------------------------------------------------
# Determinism helper
# ---------------------------------------------------------------------------


def normalized_fbs(doc: dict) -> str:
    """Canonical JSON text with volatile fields removed (for determinism hashes).

    ``created`` is wall-clock metadata written by the skill; it carries no
    rendering meaning, so it is excluded.  Everything else (including stroke
    order, styles, coefficients and fingerprints) stays in the hash.
    """
    import json

    payload = copy.deepcopy(doc)
    payload.pop("created", None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
