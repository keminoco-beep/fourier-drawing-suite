"""Early ``fdsl/2`` drafts → canonical ``fdsl/2.1`` (WP-D).

The six drafts in ``poc/fdsl/examples/`` predate the WP-A schema.  They use
``canvas.w/h``, ``canvas.bg``, element ``type`` and ``fill.gen`` and express
generator parameters, brush parameters and ``repeat`` directly on the element.
This module converts them without changing the canonical schema.

Rules
-----
* known draft idioms are normalised deterministically;
* ``repeat`` is expanded into explicit copies carrying a ``transform``
  (rotation around ``around``), so canonical plans stay simple;
* legacy ``type:"gen"`` means "painted with a brush": it is expanded into a
  canonical ``stroke`` polyline (closed for circle-like generators, open for
  arc/wave/spiral).  A canonical ``fourier_gen`` that is closed is a *filled
  region* in ``fdsl/2.1``, so mapping gen→fourier_gen would silently turn every
  legacy outline into a fill;
* legacy ``type:"fill"`` with ``gen`` → explicit ``poly`` via
  :mod:`fourier_prompt.generators`;
* legacy ``bezier`` strokes → explicit ``points``;
* legacy ``render``/``render_contract`` → ``meta.legacy_render*`` (the canonical
  schema has no render block; the compiler reads ``fill_soft_px`` from meta);
* **unknown fields are reported as warnings and dropped**, never guessed; an
  unknown element type/gen is an error.
"""

from __future__ import annotations

import copy

from . import generators
from .generators import GenError
from .schema import (
    FDSL_SCHEMA,
    FDSchemaError,
    KNOWN_TOP_KEYS,
)

__all__ = ["LegacyError", "is_legacy", "from_legacy"]

BEZIER_SEGMENT_SAMPLES = 16
DEFAULT_FILL_SAMPLES = 256

#: Element keys consumed by the converter itself (never generator parameters).
_ELEMENT_CONTROL = {
    "type",
    "kind",
    "layer",
    "color",
    "alpha",
    "brush",
    "shape",
    "width",
    "repeat",
    "closed",
    "opacity",
    "visible",
    "id",
    "tags",
    "group",
    "transform",
    "name",
    "style",
    "params",
}
_STROKE_CONTROL = _ELEMENT_CONTROL | {"points", "bezier"}
_FILL_CONTROL = _ELEMENT_CONTROL | {"gen", "poly", "holes"}
_TEXT_CONTROL = {
    "type",
    "kind",
    "layer",
    "text",
    "font",
    "as",
    "box",
    "lines",
    "id",
    "tags",
    "group",
    "visible",
    "opacity",
    "name",
    "style",
}
_CANVAS_KEYS = {"w", "h", "width", "height", "bg", "background", "dpi", "unit"}
_LEGACY_TOP = {
    "schema",
    "name",
    "canvas",
    "palette",
    "layers",
    "elements",
    "texts",
    "constraints",
    "render",
    "render_contract",
    "seed",
    "meta",
    "description",
    "style",
    "tags",
    "id",
}

#: Draft brush aliases found in ``poc/fdsl/examples`` (short name → preset id).
_BRUSH_ALIASES = {
    "airbrush": "basic_airbrush",
    "round_hard": "basic_round_hard",
    "round_soft": "basic_round_soft",
    "fineliner": "ink_fineliner",
}


class LegacyError(FDSchemaError):
    """Structured conversion failure (``FP_E_LEGACY_*`` codes)."""


def is_legacy(plan: dict) -> bool:
    """Heuristically detect an early ``fdsl/2`` draft (no guessing downstream)."""
    if not isinstance(plan, dict):
        return False
    if plan.get("schema") == "fdsl/2":
        return True
    canvas = plan.get("canvas")
    if isinstance(canvas, dict) and ("w" in canvas or "h" in canvas or "bg" in canvas):
        return True
    elements = plan.get("elements")
    if isinstance(elements, list):
        for element in elements:
            if isinstance(element, dict) and "kind" not in element and "type" in element:
                return True
    return False


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _warn(warnings: list[dict], code: str, path: str, message: str) -> None:
    warnings.append({"severity": "warning", "code": code, "path": path, "message": message})


def _as_number(value, default):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _bezier_points(beziers) -> list[list[float]]:
    """``[[p0, c1, c2, p1], ...]`` cubic Beziers → polyline points."""
    if not isinstance(beziers, list) or not beziers:
        raise LegacyError("FP_E_LEGACY_BEZIER", "bezier must be a non-empty list of 4-point segments", "$.elements")
    points: list[list[float]] = []
    for index, segment in enumerate(beziers):
        if not (
            isinstance(segment, list)
            and len(segment) == 4
            and all(
                isinstance(p, (list, tuple))
                and len(p) == 2
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p)
                for p in segment
            )
        ):
            raise LegacyError(
                "FP_E_LEGACY_BEZIER",
                "each bezier segment must be four [x, y] points",
                f"$.elements[].bezier[{index}]",
            )
        p0, c1, c2, p1 = [[float(v) for v in p] for p in segment]
        if not points:
            points.append(p0)
        for step in range(1, BEZIER_SEGMENT_SAMPLES + 1):
            t = step / BEZIER_SEGMENT_SAMPLES
            mt = 1.0 - t
            x = mt**3 * p0[0] + 3 * mt**2 * t * c1[0] + 3 * mt * t**2 * c2[0] + t**3 * p1[0]
            y = mt**3 * p0[1] + 3 * mt**2 * t * c1[1] + 3 * mt * t**2 * c2[1] + t**3 * p1[1]
            points.append([x, y])
    return points


def _gen_params(element: dict, path: str, warnings: list[dict]):
    """Extract generator parameters from a draft element; reject unknown keys."""
    gen = element.get("gen")
    if not isinstance(gen, str) or not gen:
        raise LegacyError("FP_E_LEGACY_GEN", "element needs a string 'gen'", f"{path}.gen")
    explicit = element.get("params")
    if explicit is not None and not isinstance(explicit, dict):
        raise LegacyError("FP_E_LEGACY_PARAMS", "'params' must be an object", f"{path}.params")
    params: dict = {}
    for key, value in element.items():
        if key in _ELEMENT_CONTROL or key in ("gen", "points", "bezier", "poly", "holes"):
            continue
        params[key] = value
    if explicit:
        params.update(explicit)
    try:
        allowed = set(generators.param_names(gen))
    except GenError as exc:
        raise LegacyError("FP_E_LEGACY_GEN", str(exc), f"{path}.gen") from exc
    for key in list(params):
        if key not in allowed:
            _warn(
                warnings,
                "FP_W_LEGACY_UNKNOWN_FIELD",
                f"{path}.{key}",
                f"unknown field {key!r} dropped (not a known {gen} parameter)",
            )
            del params[key]
    return gen, params


def _transforms(element: dict, path: str, warnings: list[dict]) -> list[dict | None]:
    """Expand legacy ``repeat`` into a deterministic list of transforms."""
    repeat = element.get("repeat")
    if repeat is None:
        transform = element.get("transform")
        if transform is None:
            return [None]
        if not isinstance(transform, dict):
            raise LegacyError("FP_E_LEGACY_TRANSFORM", "transform must be an object", f"{path}.transform")
        return [copy.deepcopy(transform)]
    if not isinstance(repeat, dict):
        raise LegacyError("FP_E_LEGACY_REPEAT", "repeat must be an object", f"{path}.repeat")
    n = repeat.get("n", 6)
    if isinstance(n, bool) or not isinstance(n, (int, float)) or not float(n).is_integer():
        raise LegacyError("FP_E_LEGACY_REPEAT", "repeat.n must be an integer", f"{path}.repeat.n")
    n = int(n)
    if not 1 <= n <= 256:
        raise LegacyError("FP_E_LEGACY_REPEAT", "repeat.n must be in [1, 256]", f"{path}.repeat.n")
    around = repeat.get("around", [0, 0])
    if not (
        isinstance(around, (list, tuple))
        and len(around) == 2
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in around)
    ):
        raise LegacyError("FP_E_LEGACY_REPEAT", "repeat.around must be [x, y]", f"{path}.repeat.around")
    start = _as_number(repeat.get("from", 0.0), 0.0)
    sweep = _as_number(repeat.get("sweep", 360.0), 360.0)
    scale = _as_number(repeat.get("scale", 1.0), 1.0)
    if scale <= 0.0:
        raise LegacyError("FP_E_LEGACY_REPEAT", "repeat.scale must be > 0", f"{path}.repeat.scale")
    step = sweep / n
    transforms: list[dict | None] = []
    for index in range(n):
        transform: dict = {"center": [float(around[0]), float(around[1])], "rotate": start + index * step}
        if abs(scale - 1.0) > 1e-12:
            transform["scale"] = scale
        transforms.append(transform)
    return transforms


def _brush(element: dict, *, default_width: float, warnings: list[dict] | None = None, path: str = "$") -> dict:
    brush: dict = {}
    preset = element.get("brush")
    if isinstance(preset, str) and preset:
        alias = _BRUSH_ALIASES.get(preset)
        if alias is not None:
            brush["preset"] = alias
            if warnings is not None:
                _warn(
                    warnings,
                    "FP_W_LEGACY_BRUSH_ALIAS",
                    f"{path}.brush",
                    f"brush alias {preset!r} -> preset {alias!r}",
                )
        else:
            brush["preset"] = preset
    elif isinstance(preset, dict):
        brush.update({k: v for k, v in preset.items() if k in {"preset", "width", "color", "alpha", "shape"}})
    width = element.get("width", brush.get("width", default_width))
    if width is not None:
        brush["width"] = width
    color = element.get("color", brush.get("color"))
    if color is not None:
        brush["color"] = color
    alpha = element.get("alpha", brush.get("alpha"))
    if alpha is not None:
        brush["alpha"] = alpha
    shape = element.get("shape", brush.get("shape"))
    if shape is not None:
        brush["shape"] = shape
    return brush


def _copy_common(source: dict, target: dict) -> None:
    for key in ("id", "tags", "group", "visible", "opacity", "name", "style"):
        if key in source:
            target[key] = copy.deepcopy(source[key])


# ---------------------------------------------------------------------------
# Element conversion
# ---------------------------------------------------------------------------


def _convert_gen(element: dict, path: str, warnings: list[dict], *, samples: int = DEFAULT_FILL_SAMPLES):
    gen, params = _gen_params(element, path, warnings)
    try:
        open_by_default = generators.is_open(gen)
        points = generators.contour(gen, params, samples=samples)
    except GenError as exc:
        raise LegacyError("FP_E_LEGACY_GEN", str(exc), f"{path}.gen") from exc
    closed = element.get("closed", not open_by_default)
    if not isinstance(closed, bool):
        raise LegacyError("FP_E_LEGACY_CLOSED", "closed must be a boolean", f"{path}.closed")
    brush = _brush(element, default_width=6.0, warnings=warnings, path=path)
    out = []
    for transform in _transforms(element, path, warnings):
        record = {
            "kind": "stroke",
            "layer": element.get("layer", 0),
            "points": copy.deepcopy(points),
            "closed": closed,
            "brush": copy.deepcopy(brush),
        }
        if transform is not None:
            record["transform"] = transform
        _copy_common(element, record)
        out.append(record)
    return out


def _convert_fill(element: dict, path: str, warnings: list[dict], *, samples: int = DEFAULT_FILL_SAMPLES):
    poly = element.get("poly")
    params = {}
    gen = element.get("gen")
    if poly is None:
        if not isinstance(gen, str) or not gen:
            raise LegacyError("FP_E_LEGACY_FILL", "fill needs 'poly' or 'gen'", path)
        gen, params = _gen_params(element, path, warnings)
        try:
            if generators.is_open(gen):
                _warn(
                    warnings,
                    "FP_W_LEGACY_OPEN_FILL",
                    f"{path}.gen",
                    f"fill uses open generator {gen!r}; the contour is implicitly closed",
                )
            poly = generators.contour(gen, params, samples=samples)
        except GenError as exc:
            raise LegacyError("FP_E_LEGACY_GEN", str(exc), f"{path}.gen") from exc
    if not isinstance(poly, list) or len(poly) < 3:
        raise LegacyError("FP_E_LEGACY_FILL", "fill polygon needs at least 3 points", f"{path}.poly")
    holes = element.get("holes")
    out = []
    for transform in _transforms(element, path, warnings):
        record: dict = {
            "kind": "fill",
            "layer": element.get("layer", 0),
            "poly": copy.deepcopy(poly),
        }
        if element.get("color") is not None:
            record["color"] = copy.deepcopy(element["color"])
        if holes:
            record["holes"] = copy.deepcopy(holes)
        if "alpha" in element:
            record["brush"] = {"alpha": copy.deepcopy(element["alpha"])}
        for key in ("opacity", "visible", "id", "tags", "group", "name", "style"):
            if key in element:
                record[key] = copy.deepcopy(element[key])
        if transform is not None:
            record["transform"] = transform
        out.append(record)
    return out


def _convert_stroke(element: dict, path: str, warnings: list[dict]):
    bezier = element.get("bezier")
    if bezier is not None:
        points = _bezier_points(bezier)
        if element.get("points") is not None:
            _warn(
                warnings,
                "FP_W_LEGACY_STROKE_BOTH",
                path,
                "stroke has both 'bezier' and 'points'; bezier wins (legacy behaviour)",
            )
    else:
        points = element.get("points")
    if not isinstance(points, list) or len(points) < 2:
        raise LegacyError("FP_E_LEGACY_STROKE", "stroke needs >= 2 points or a bezier", f"{path}.points")
    closed = bool(element.get("closed", False))
    brush = _brush(element, default_width=6.0, warnings=warnings, path=path)
    out = []
    for transform in _transforms(element, path, warnings):
        record: dict = {
            "kind": "stroke",
            "layer": element.get("layer", 0),
            "points": copy.deepcopy(points),
            "closed": closed,
            "brush": copy.deepcopy(brush),
        }
        _copy_common(element, record)
        if transform is not None:
            record["transform"] = transform
        out.append(record)
    return out


def _convert_text(element: dict, path: str):
    text = element.get("text")
    if not isinstance(text, str) or not text.strip():
        raise LegacyError("FP_E_LEGACY_TEXT", "text element needs non-empty 'text'", f"{path}.text")
    record = {"kind": "text", "layer": element.get("layer", 0), "text": text}
    for key in ("as", "font", "box", "lines"):
        if key in element:
            record[key] = copy.deepcopy(element[key])
    _copy_common(element, record)
    return record


# ---------------------------------------------------------------------------
# Plan conversion
# ---------------------------------------------------------------------------


def _convert_canvas(canvas, warnings: list[dict]):
    if not isinstance(canvas, dict):
        raise LegacyError("FP_E_LEGACY_CANVAS", "canvas must be an object", "$.canvas")
    out = {}
    for target, sources in (("width", ("width", "w")), ("height", ("height", "h"))):
        for source in sources:
            if source in canvas:
                out[target] = canvas[source]
                if source != target:
                    _warn(
                        warnings,
                        "FP_W_LEGACY_CANVAS",
                        f"$.canvas.{source}",
                        f"canvas.{source} -> canvas.{target}",
                    )
                break
    if "background" in canvas:
        out["background"] = copy.deepcopy(canvas["background"])
    elif "bg" in canvas:
        out["background"] = copy.deepcopy(canvas["bg"])
        _warn(warnings, "FP_W_LEGACY_CANVAS", "$.canvas.bg", "canvas.bg -> canvas.background")
    for key in ("dpi", "unit"):
        if key in canvas:
            out[key] = copy.deepcopy(canvas[key])
    for key in canvas:
        if key not in _CANVAS_KEYS:
            _warn(
                warnings,
                "FP_W_LEGACY_UNKNOWN_FIELD",
                f"$.canvas.{key}",
                f"unknown canvas field {key!r} dropped",
            )
    return out


def _convert_layers(plan: dict, elements: list[dict], texts: list[dict], warnings: list[dict]):
    raw = plan.get("layers")
    order: list = []
    described: dict = {}
    if isinstance(raw, list):
        for index, entry in enumerate(raw):
            if isinstance(entry, dict) and "id" in entry:
                lid = entry["id"]
                described[(type(lid).__name__, lid)] = copy.deepcopy(entry)
                order.append(lid)
            elif isinstance(entry, (int, str)) and not isinstance(entry, bool):
                order.append(entry)
                described[(type(entry).__name__, entry)] = {"id": entry, "name": f"layer_{entry}"}
            else:
                _warn(
                    warnings,
                    "FP_W_LEGACY_LAYER",
                    f"$.layers[{index}]",
                    "unrecognised layer entry dropped",
                )
    elif raw is not None:
        raise LegacyError("FP_E_LEGACY_LAYERS", "layers must be a list", "$.layers")

    refs = []
    for element in elements:
        refs.append(element.get("layer", 0))
    for text in texts:
        refs.append(text.get("layer", 0))
    known = [(type(lid).__name__, lid) for lid in order]
    missing = []
    for ref in refs:
        marker = (type(ref).__name__, ref)
        if marker not in known and marker not in missing:
            missing.append(marker)
    for marker in sorted(missing, key=lambda item: (item[0], str(item[1]))):
        lid = marker[1]
        order.append(lid)
        described[marker] = {"id": lid, "name": f"layer_{lid}"}
    if missing:
        _warn(
            warnings,
            "FP_W_LEGACY_LAYER",
            "$.layers",
            "layers referenced by elements but not declared were synthesised: "
            + ", ".join(repr(item[1]) for item in missing),
        )
    layers = []
    for lid in order:
        entry = described.get((type(lid).__name__, lid), {"id": lid, "name": f"layer_{lid}"})
        if "name" not in entry:
            entry["name"] = f"layer_{lid}"
        layers.append(entry)
    return layers


def from_legacy(plan: dict, *, samples: int = DEFAULT_FILL_SAMPLES) -> tuple[dict, list[dict]]:
    """Convert an early ``fdsl/2`` draft into a canonical ``fdsl/2.1`` plan.

    Returns ``(plan_2_1, warnings)``; warnings use the schema issue shape
    (``severity/code/path/message``).  Raises :class:`LegacyError` for
    unreadable drafts (bad schema, unknown element type, bad repeat/bezier...).
    """
    if not isinstance(plan, dict):
        raise LegacyError("FP_E_PLAN_TYPE", "plan must be a JSON object", "$")
    schema = plan.get("schema")
    if schema not in ("fdsl/2", "fdsl/2.1"):
        raise LegacyError(
            "FP_E_LEGACY_SCHEMA",
            f"legacy conversion expects schema 'fdsl/2' (got {schema!r})",
            "$.schema",
        )
    warnings: list[dict] = []
    out: dict = {"schema": FDSL_SCHEMA}
    if "name" in plan:
        out["name"] = copy.deepcopy(plan["name"])
    out["canvas"] = _convert_canvas(plan.get("canvas"), warnings)
    if "palette" in plan:
        out["palette"] = copy.deepcopy(plan["palette"])

    raw_elements = plan.get("elements")
    if not isinstance(raw_elements, list):
        raise LegacyError("FP_E_LEGACY_ELEMENTS", "elements must be a list", "$.elements")
    elements: list[dict] = []
    for index, element in enumerate(raw_elements):
        path = f"$.elements[{index}]"
        if not isinstance(element, dict):
            raise LegacyError("FP_E_LEGACY_ELEMENT", "element must be an object", path)
        kind = element.get("kind", element.get("type"))
        if kind is None:
            raise LegacyError("FP_E_LEGACY_KIND", "element has no 'type'", path)
        kind = str(kind).lower()
        if kind in ("gen", "fourier_gen"):
            elements.extend(_convert_gen(element, path, warnings, samples=samples))
        elif kind == "fill":
            elements.extend(_convert_fill(element, path, warnings, samples=samples))
        elif kind == "stroke":
            elements.extend(_convert_stroke(element, path, warnings))
        elif kind == "text":
            elements.append(_convert_text(element, path))
        else:
            raise LegacyError(
                "FP_E_LEGACY_KIND",
                f"unknown element type {kind!r}; expected gen/fill/stroke/text",
                path,
            )

    texts: list[dict] = []
    raw_texts = plan.get("texts")
    if isinstance(raw_texts, list):
        for index, entry in enumerate(raw_texts):
            path = f"$.texts[{index}]"
            if not isinstance(entry, dict):
                raise LegacyError("FP_E_LEGACY_TEXT", "text entry must be an object", path)
            record = copy.deepcopy(entry)
            if "id" not in record:
                record["id"] = f"t{index + 1}"
                _warn(warnings, "FP_W_LEGACY_TEXT_ID", path, f"missing text id synthesised as t{index + 1}")
            texts.append(record)
    elif raw_texts is not None:
        raise LegacyError("FP_E_LEGACY_TEXTS", "texts must be a list", "$.texts")

    out["layers"] = _convert_layers(plan, elements, texts, warnings)
    out["elements"] = elements
    if texts:
        out["texts"] = texts
    if "constraints" in plan:
        out["constraints"] = copy.deepcopy(plan["constraints"])
    for key in ("seed", "description", "style", "tags", "id"):
        if key in plan:
            out[key] = copy.deepcopy(plan[key])

    meta = copy.deepcopy(plan.get("meta")) if isinstance(plan.get("meta"), dict) else {}
    for source, target in (("render", "legacy_render"), ("render_contract", "legacy_render_contract")):
        if source in plan:
            if not isinstance(plan[source], dict):
                _warn(warnings, "FP_W_LEGACY_RENDER", f"$.{source}", f"{source} must be an object; dropped")
                continue
            meta[target] = copy.deepcopy(plan[source])
            _warn(warnings, "FP_W_LEGACY_RENDER", f"$.{source}", f"{source} -> meta.{target}")
    if meta:
        out["meta"] = meta

    for key in plan:
        if key not in _LEGACY_TOP and key not in KNOWN_TOP_KEYS:
            _warn(
                warnings,
                "FP_W_LEGACY_UNKNOWN_FIELD",
                f"$.{key}",
                f"unknown top-level field {key!r} dropped",
            )
    return out, warnings
