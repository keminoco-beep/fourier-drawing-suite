"""FDSL v2.1 schema validation (WP-A core scaffold).

Scope (v1, per KICKOFF v1 §2.1): top-level fields, schema version, canvas,
element kinds/enums, required fields, numeric ranges, colours, layers and
element limits.  Text / edit fields are validated structurally only; their
semantics belong to later work packages.

The validator never mutates its input and never guesses: an unknown ``gen``,
brush preset or shape is an error, not a warning.
"""

from __future__ import annotations

import json
import math
import os
import re

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

FDSL_SCHEMA = "fdsl/2.1"
#: Accepted schema identifiers (``fdsl/2`` is read as a compatible subset).
FDSL_SCHEMA_COMPAT = ("fdsl/2", "fdsl/2.1")
FDSL_SCHEMA_ALL = ("fdsl/2", "fdsl/2.1")

MAX_ELEMENTS_HARD = 2000
DEFAULT_MAX_ELEMENTS = 400
MAX_LAYERS = 32
MAX_PALETTE = 64
MAX_TEXTS = 64
MAX_TAGS = 8
MAX_STROKE_POINTS = 20000
MAX_POLY_POINTS = 8192
MAX_HOLES = 64
MIN_CANVAS = 16
MAX_CANVAS = 8192
MAX_POINT_ABS = 1e7
MAX_WIDTH = 256.0
MIN_WIDTH = 0.1
MAX_FONT_SIZE = 4096.0

GEN_LIBRARY = (
    "circle",
    "ellipse",
    "arc",
    "regular_polygon",
    "star",
    "rose",
    "spiral",
    "wave",
    "lissajous",
    "superellipse",
)

ELEMENT_KINDS = ("fourier_gen", "stroke", "fill", "text")
KIND_ALIASES = {"gen": "fourier_gen"}

#: Built-in brush presets of ``fourier-brushstroke/scripts/fbs_core/brushlib.py``.
#: WP-C owns the catalog; this list is the frozen v1 contract for validation.
BRUSH_PRESETS = (
    "basic_airbrush",
    "basic_round_hard",
    "basic_round_soft",
    "bristle_oil",
    "chalk",
    "charcoal",
    "dry_flat",
    "ink_brush_rough",
    "ink_fineliner",
    "ink_gpen",
    "marker_alcohol",
    "marker_chisel",
    "palette_knife",
    "pastel",
    "pencil_2b",
    "pencil_hb",
    "spatter_brush",
    "sumi_e",
    "watercolor_wash",
    "wet_oil",
)

BRUSH_SHAPES = (
    "round",
    "soft",
    "marker",
    "bristle",
    "chalk",
    "spatter",
    "airbrush",
    "pencil",
    "watercolor",
    "ink",
)

#: Built-in font ids (D6); the font subsystem is a later work package.
BUILTIN_FONTS = (
    "source_han_sans_sc",
    "source_han_serif_sc",
    "lxgw_wenkai",
    "inter",
    "source_serif_4",
    "caveat",
)

TEXT_AS = ("outline", "glyph")
TEXT_ALIGNS = ("left", "center", "right")

KNOWN_TOP_KEYS = {
    "schema",
    "name",
    "canvas",
    "palette",
    "layers",
    "elements",
    "texts",
    "constraints",
    "seed",
    "meta",
    "description",
    "style",
    "tags",
    "id",
}
KNOWN_CANVAS_KEYS = {"width", "height", "background", "dpi", "unit"}
KNOWN_LAYER_KEYS = {"id", "name", "order", "visible", "opacity"}
KNOWN_CONSTRAINT_KEYS = {
    "max_elements",
    "max_ops",
    "text_max",
    "keep_margin",
    "single_line",
    "max_layers",
}
KNOWN_ELEMENT_KEYS = {
    "kind",
    "type",
    "id",
    "tags",
    "group",
    "layer",
    "visible",
    "opacity",
    "brush",
    "transform",
    "gen",
    "params",
    "points",
    "closed",
    "poly",
    "holes",
    "color",
    "text",
    "font",
    "as",
    "box",
    "lines",
    "name",
    "style",
    "bbox",
}
KNOWN_BRUSH_KEYS = {
    "preset",
    "width",
    "color",
    "alpha",
    "shape",
    "pressure",
    "jitter",
    "spacing",
}
KNOWN_TRANSFORM_KEYS = {"center", "scale", "rotate", "skew", "anchor"}
KNOWN_FONT_KEYS = {"id", "size_px", "color", "alpha", "align", "spacing", "line_gap"}
KNOWN_TEXT_KEYS = {"id", "as", "layer", "text", "font", "box", "lines", "visible", "opacity"}

_ELEMENT_ID_RE = re.compile(r"^e[1-9][0-9]{0,5}$")
_TEXT_ID_RE = re.compile(r"^t[1-9][0-9]{0,3}$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_:-]{0,31}$")
_HEX_RE = re.compile(r"^#(?:[0-9a-f]{6}|[0-9a-f]{8})$")
_SHORT_HEX_RE = re.compile(r"^#[0-9a-f]{3}$")
_PALETTE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,31}$")


# ---------------------------------------------------------------------------
# Errors / issues
# ---------------------------------------------------------------------------


class FDSchemaError(ValueError):
    """Structured FDSL error carrying ``code`` / ``message`` / ``path``."""

    def __init__(self, code, message, path="$", *, details=None):
        super().__init__(f"{code} at {path}: {message}")
        self.code = str(code)
        self.message = str(message)
        self.path = str(path)
        self.details = list(details or [])

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "severity": "error",
        }


class _Issues:
    """Ordered issue collector; deterministic output order."""

    def __init__(self):
        self.items = []

    def error(self, code, path, message):
        self.items.append(
            {"severity": "error", "code": code, "path": path, "message": message}
        )

    def warn(self, code, path, message):
        self.items.append(
            {"severity": "warning", "code": code, "path": path, "message": message}
        )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_num(value):
    return _is_num(value) and math.isfinite(float(value))


def _is_int_like(value):
    """True for ints (not bool) and integral floats."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value) and value.is_integer()


def _is_point(value):
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(_is_finite_num(v) for v in value)
    )


def is_hex_color(value):
    return isinstance(value, str) and bool(_HEX_RE.match(value.strip().lower()))


def color_ok(value, names):
    if not isinstance(value, str):
        return False
    text = value.strip()
    if is_hex_color(text):
        return True
    return text in names


def _format_issue(issue):
    return f"{issue['code']} at {issue['path']}: {issue['message']}"


def _check_unknown_keys(obj, known, path, issues):
    for key in obj:
        if key not in known:
            issues.warn(
                "FP_W_UNKNOWN_FIELD",
                f"{path}.{key}",
                f"unknown field {key!r} (kept as-is; forward-compatible)",
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_plan_detailed(plan, *, strict=False):
    """Return a structured validation report.

    ``{"ok": bool, "strict": bool, "schema": str|None,
       "errors": [issue], "warnings": [issue]}``

    In strict mode warnings are promoted to errors (they keep
    ``severity="warning"`` plus ``promoted=True``).
    """
    if not isinstance(plan, dict):
        raise FDSchemaError("FP_E_PLAN_TYPE", "plan must be a JSON object", "$")
    issues = _Issues()
    _validate_plan(plan, issues)
    hard = [item for item in issues.items if item["severity"] == "error"]
    soft = [item for item in issues.items if item["severity"] == "warning"]
    if strict:
        errors = hard + [dict(item, promoted=True) for item in soft]
        warnings = []
    else:
        errors = hard
        warnings = soft
    return {
        "ok": not errors,
        "strict": bool(strict),
        "schema": plan.get("schema"),
        "errors": errors,
        "warnings": warnings,
    }


def validate_plan(plan, *, strict=False):
    """Validate ``plan`` and return a list of error strings (empty == valid).

    ``strict=True`` treats warnings as errors.
    """
    report = validate_plan_detailed(plan, strict=strict)
    return [_format_issue(item) for item in report["errors"]]


def load_plan(path):
    """Load a JSON plan from ``path``; raises :class:`FDSchemaError` on I/O or JSON errors."""
    location = os.fspath(path)
    try:
        with open(location, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise FDSchemaError(
            "FP_E_LOAD", f"cannot read plan: {exc}", location
        ) from exc
    except json.JSONDecodeError as exc:
        raise FDSchemaError(
            "FP_E_JSON", f"invalid JSON: {exc}", location
        ) from exc
    if not isinstance(data, dict):
        raise FDSchemaError("FP_E_PLAN_TYPE", "plan must be a JSON object", location)
    return data


# ---------------------------------------------------------------------------
# Validation internals
# ---------------------------------------------------------------------------


def _validate_plan(plan, issues):
    schema = plan.get("schema")
    if schema is None:
        issues.error("FP_E_SCHEMA_MISSING", "$.schema", "required field 'schema' is missing")
    elif not isinstance(schema, str):
        issues.error("FP_E_SCHEMA_TYPE", "$.schema", "schema must be a string")
    elif schema not in FDSL_SCHEMA_ALL:
        issues.error(
            "FP_E_SCHEMA_VERSION",
            "$.schema",
            f"unsupported schema {schema!r}; expected one of {list(FDSL_SCHEMA_ALL)}",
        )

    name = plan.get("name")
    if name is not None:
        if not isinstance(name, str) or not name.strip():
            issues.error("FP_E_NAME_TYPE", "$.name", "name must be a non-empty string")
        elif len(name) > 200:
            issues.error("FP_E_NAME_LENGTH", "$.name", "name must be <= 200 characters")

    names = _validate_palette(plan, issues)

    if "canvas" not in plan:
        issues.error("FP_E_CANVAS_MISSING", "$.canvas", "required field 'canvas' is missing")
    else:
        _validate_canvas(plan["canvas"], issues, names)

    layer_ids = _validate_layers(plan, issues)
    limits = _validate_constraints(plan, issues)

    if "elements" not in plan:
        issues.error("FP_E_ELEMENTS_MISSING", "$.elements", "required field 'elements' is missing")
    else:
        _validate_elements(
            plan["elements"],
            issues,
            names=names,
            layer_ids=layer_ids,
            max_elements=limits["max_elements"],
        )

    _validate_texts(plan, issues, names=names, layer_ids=layer_ids)

    _check_unknown_keys(plan, KNOWN_TOP_KEYS, "$", issues)


def _validate_canvas(canvas, issues, names):
    if not isinstance(canvas, dict):
        issues.error("FP_E_CANVAS_TYPE", "$.canvas", "canvas must be an object")
        return None, None
    width = height = None
    for key in ("width", "height"):
        if key not in canvas:
            issues.error(
                "FP_E_CANVAS_MISSING",
                f"$.canvas.{key}",
                f"canvas.{key} is required",
            )
            continue
        value = canvas[key]
        if not _is_int_like(value):
            issues.error(
                "FP_E_CANVAS_TYPE",
                f"$.canvas.{key}",
                f"canvas.{key} must be an integer (got {value!r})",
            )
            continue
        ivalue = int(value)
        if not MIN_CANVAS <= ivalue <= MAX_CANVAS:
            issues.error(
                "FP_E_CANVAS_RANGE",
                f"$.canvas.{key}",
                f"canvas.{key} must be in [{MIN_CANVAS}, {MAX_CANVAS}] (got {ivalue})",
            )
            continue
        if key == "width":
            width = ivalue
        else:
            height = ivalue

    if "background" in canvas and canvas["background"] is not None:
        background = canvas["background"]
        if isinstance(background, (list, tuple)):
            if (
                len(background) not in (3, 4)
                or not all(
                    isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255
                    for c in background
                )
            ):
                issues.error(
                    "FP_E_COLOR_INVALID",
                    "$.canvas.background",
                    "background list must be 3 or 4 integers in [0, 255]",
                )
        elif not color_ok(background, names):
            issues.error(
                "FP_E_COLOR_INVALID",
                "$.canvas.background",
                f"invalid background colour {background!r}",
            )

    if "dpi" in canvas and not (
        _is_finite_num(canvas["dpi"]) and 1 <= float(canvas["dpi"]) <= 2400
    ):
        issues.error("FP_E_CANVAS_TYPE", "$.canvas.dpi", "dpi must be a number in [1, 2400]")

    _check_unknown_keys(canvas, KNOWN_CANVAS_KEYS, "$.canvas", issues)
    return width, height


def _validate_palette(plan, issues):
    palette = plan.get("palette")
    if palette is None:
        return set()
    if isinstance(palette, list):
        if len(palette) > MAX_PALETTE:
            issues.error(
                "FP_E_PALETTE_LIMIT",
                "$.palette",
                f"palette may hold at most {MAX_PALETTE} colours",
            )
        for index, entry in enumerate(palette):
            if not is_hex_color(entry):
                issues.error(
                    "FP_E_COLOR_INVALID",
                    f"$.palette[{index}]",
                    f"palette entries must be hex colours (got {entry!r})",
                )
        return set()
    if isinstance(palette, dict):
        if len(palette) > MAX_PALETTE:
            issues.error(
                "FP_E_PALETTE_LIMIT",
                "$.palette",
                f"palette may hold at most {MAX_PALETTE} colours",
            )
        names = set()
        for key, entry in palette.items():
            if not isinstance(key, str) or not _PALETTE_NAME_RE.match(key):
                issues.error(
                    "FP_E_PALETTE_NAME",
                    f"$.palette[{key!r}]",
                    "palette names must match ^[A-Za-z_][A-Za-z0-9_.:-]{0,31}$",
                )
                continue
            if not is_hex_color(entry):
                issues.error(
                    "FP_E_COLOR_INVALID",
                    f"$.palette[{key!r}]",
                    f"palette values must be hex colours (got {entry!r})",
                )
                continue
            names.add(key)
        return names
    issues.error("FP_E_PALETTE_TYPE", "$.palette", "palette must be a list or an object")
    return set()


def _validate_layers(plan, issues):
    layers = plan.get("layers")
    if layers is None:
        return None
    if not isinstance(layers, list):
        issues.error("FP_E_LAYER_TYPE", "$.layers", "layers must be a list")
        return None
    if len(layers) > MAX_LAYERS:
        issues.error(
            "FP_E_LAYER_LIMIT",
            "$.layers",
            f"at most {MAX_LAYERS} layers are supported (got {len(layers)})",
        )
    ids = []
    for index, layer in enumerate(layers):
        path = f"$.layers[{index}]"
        if not isinstance(layer, dict):
            issues.error("FP_E_LAYER_TYPE", path, "layer must be an object")
            continue
        if "id" not in layer:
            issues.error("FP_E_LAYER_ID", f"{path}.id", "layer.id is required")
        else:
            lid = layer["id"]
            if isinstance(lid, bool) or not isinstance(lid, (int, str)):
                issues.error("FP_E_LAYER_ID", f"{path}.id", "layer.id must be an int or string")
            elif isinstance(lid, str) and not (1 <= len(lid) <= 32):
                issues.error(
                    "FP_E_LAYER_ID", f"{path}.id", "layer.id string must be 1..32 characters"
                )
            else:
                ids.append(lid)
        if "name" in layer:
            lname = layer["name"]
            if not isinstance(lname, str) or not lname.strip():
                issues.error("FP_E_LAYER_NAME", f"{path}.name", "layer.name must be a non-empty string")
            elif len(lname) > 64:
                issues.error("FP_E_LAYER_NAME", f"{path}.name", "layer.name must be <= 64 characters")
        if "order" in layer and not (
            _is_int_like(layer["order"]) and int(layer["order"]) >= 0
        ):
            issues.error("FP_E_LAYER_ORDER", f"{path}.order", "layer.order must be an integer >= 0")
        if "visible" in layer and not isinstance(layer["visible"], bool):
            issues.error("FP_E_LAYER_VISIBLE", f"{path}.visible", "layer.visible must be a boolean")
        if "opacity" in layer and not (
            _is_finite_num(layer["opacity"]) and 0.0 <= float(layer["opacity"]) <= 1.0
        ):
            issues.error(
                "FP_E_LAYER_OPACITY", f"{path}.opacity", "layer.opacity must be in [0, 1]"
            )
        _check_unknown_keys(layer, KNOWN_LAYER_KEYS, path, issues)

    seen = set()
    for lid in ids:
        marker = (type(lid).__name__, lid)
        if marker in seen:
            issues.error("FP_E_LAYER_DUPLICATE", "$.layers", f"duplicate layer id {lid!r}")
        seen.add(marker)
    return ids


def _validate_constraints(plan, issues):
    limits = {"max_elements": DEFAULT_MAX_ELEMENTS}
    constraints = plan.get("constraints")
    if constraints is None:
        return limits
    if not isinstance(constraints, dict):
        issues.error("FP_E_CONSTRAINTS_TYPE", "$.constraints", "constraints must be an object")
        return limits

    if "max_elements" in constraints:
        value = constraints["max_elements"]
        if not (_is_int_like(value) and 1 <= int(value) <= MAX_ELEMENTS_HARD):
            issues.error(
                "FP_E_RANGE",
                "$.constraints.max_elements",
                f"max_elements must be an integer in [1, {MAX_ELEMENTS_HARD}]",
            )
        else:
            limits["max_elements"] = int(value)

    if "max_ops" in constraints:
        value = constraints["max_ops"]
        if not (_is_int_like(value) and 1 <= int(value) <= 4096):
            issues.error(
                "FP_E_RANGE", "$.constraints.max_ops", "max_ops must be an integer in [1, 4096]"
            )
    if "text_max" in constraints:
        value = constraints["text_max"]
        if not (_is_int_like(value) and 1 <= int(value) <= 1000):
            issues.error(
                "FP_E_RANGE", "$.constraints.text_max", "text_max must be an integer in [1, 1000]"
            )
    if "max_layers" in constraints:
        value = constraints["max_layers"]
        if not (_is_int_like(value) and 1 <= int(value) <= MAX_LAYERS):
            issues.error(
                "FP_E_RANGE",
                "$.constraints.max_layers",
                f"max_layers must be an integer in [1, {MAX_LAYERS}]",
            )
    if "keep_margin" in constraints:
        value = constraints["keep_margin"]
        if not (_is_finite_num(value) and 0 <= float(value) <= MAX_CANVAS):
            issues.error(
                "FP_E_RANGE", "$.constraints.keep_margin", "keep_margin must be a number in [0, 8192]"
            )
    if "single_line" in constraints and not isinstance(constraints["single_line"], bool):
        issues.error(
            "FP_E_CONSTRAINTS_TYPE", "$.constraints.single_line", "single_line must be a boolean"
        )
    _check_unknown_keys(constraints, KNOWN_CONSTRAINT_KEYS, "$.constraints", issues)
    return limits


def _layer_ref_ok(ref, layer_ids):
    if isinstance(ref, bool):
        return False
    if isinstance(ref, int):
        if layer_ids is None:
            return 0 <= ref < MAX_LAYERS
        return ref in layer_ids or 0 <= ref < len(layer_ids)
    if isinstance(ref, str):
        return layer_ids is not None and ref in layer_ids
    return False


def _validate_elements(elements, issues, *, names, layer_ids, max_elements):
    if not isinstance(elements, list):
        issues.error("FP_E_ELEMENTS_TYPE", "$.elements", "elements must be a list")
        return
    if len(elements) > max_elements:
        issues.error(
            "FP_E_ELEMENT_LIMIT",
            "$.elements",
            f"{len(elements)} elements exceed the limit of {max_elements}",
        )

    seen_ids = set()
    for index, element in enumerate(elements):
        path = f"$.elements[{index}]"
        if not isinstance(element, dict):
            issues.error("FP_E_ELEMENT_TYPE", path, "element must be an object")
            continue

        kind_raw = element.get("kind", element.get("type"))
        if kind_raw is None:
            issues.error("FP_E_ELEMENT_KIND_MISSING", path, "element has no 'kind'")
            continue
        if not isinstance(kind_raw, str):
            issues.error("FP_E_ELEMENT_KIND", path, "element 'kind' must be a string")
            continue
        kind = KIND_ALIASES.get(kind_raw, kind_raw)
        if kind not in ELEMENT_KINDS:
            issues.error(
                "FP_E_ELEMENT_KIND",
                f"{path}.kind",
                f"unsupported element kind {kind_raw!r}; expected one of {list(ELEMENT_KINDS)}",
            )
            continue

        if "id" in element:
            eid = element["id"]
            if not isinstance(eid, str) or not _ELEMENT_ID_RE.match(eid):
                issues.error(
                    "FP_E_ELEMENT_ID",
                    f"{path}.id",
                    "element id must match ^e[1-9][0-9]{0,5}$",
                )
            elif eid in seen_ids:
                issues.error("FP_E_ELEMENT_ID", f"{path}.id", f"duplicate element id {eid!r}")
            else:
                seen_ids.add(eid)

        if "tags" in element:
            tags = element["tags"]
            if not isinstance(tags, list):
                issues.error("FP_E_TAGS", f"{path}.tags", "tags must be a list")
            else:
                if len(tags) > MAX_TAGS:
                    issues.error(
                        "FP_E_TAGS",
                        f"{path}.tags",
                        f"at most {MAX_TAGS} tags are allowed (got {len(tags)})",
                    )
                for tag_index, tag in enumerate(tags):
                    if not isinstance(tag, str) or not _TAG_RE.match(tag):
                        issues.error(
                            "FP_E_TAG",
                            f"{path}.tags[{tag_index}]",
                            "tag must match ^[a-z0-9][a-z0-9_:-]{0,31}$",
                        )

        if "group" in element and not (
            isinstance(element["group"], str) and 1 <= len(element["group"]) <= 64
        ):
            issues.error("FP_E_GROUP", f"{path}.group", "group must be a string of 1..64 characters")

        if "layer" not in element:
            issues.error("FP_E_LAYER_MISSING", f"{path}.layer", "element.layer is required")
        elif not _layer_ref_ok(element["layer"], layer_ids):
            issues.error(
                "FP_E_LAYER_REF",
                f"{path}.layer",
                f"layer {element['layer']!r} is not declared in $.layers",
            )

        if "visible" in element and not isinstance(element["visible"], bool):
            issues.error("FP_E_VISIBLE", f"{path}.visible", "visible must be a boolean")
        if "opacity" in element and not (
            _is_finite_num(element["opacity"]) and 0.0 <= float(element["opacity"]) <= 1.0
        ):
            issues.error("FP_E_OPACITY", f"{path}.opacity", "opacity must be in [0, 1]")

        if "color" in element and not color_ok(element["color"], names):
            issues.error(
                "FP_E_COLOR_INVALID",
                f"{path}.color",
                f"invalid colour {element['color']!r} (hex or palette name expected)",
            )

        _validate_brush(element.get("brush"), f"{path}.brush", issues, names)
        _validate_transform(element.get("transform"), f"{path}.transform", issues)

        if kind == "fourier_gen":
            _validate_gen(element, path, issues)
        elif kind == "stroke":
            _validate_stroke(element, path, issues)
        elif kind == "fill":
            _validate_fill(element, path, issues, names)
        elif kind == "text":
            _validate_text_element(element, path, issues, names, layer_ids)

        _check_unknown_keys(element, KNOWN_ELEMENT_KEYS, path, issues)


def _validate_gen(element, path, issues):
    gen = element.get("gen")
    if gen is None:
        issues.error("FP_E_GEN_MISSING", f"{path}.gen", "fourier_gen element requires 'gen'")
        return
    if not isinstance(gen, str) or gen not in GEN_LIBRARY:
        issues.error(
            "FP_E_GEN_UNKNOWN",
            f"{path}.gen",
            f"unknown gen {gen!r}; expected one of {list(GEN_LIBRARY)}",
        )
    if "params" in element and not isinstance(element["params"], dict):
        issues.error("FP_E_PARAMS_TYPE", f"{path}.params", "params must be an object")


def _validate_stroke(element, path, issues):
    if "points" not in element:
        issues.error("FP_E_POINTS_MISSING", f"{path}.points", "stroke element requires 'points'")
    else:
        _validate_points(element["points"], f"{path}.points", issues, min_points=2,
                         limit=MAX_STROKE_POINTS)
    if "closed" in element and not isinstance(element["closed"], bool):
        issues.error("FP_E_CLOSED_TYPE", f"{path}.closed", "closed must be a boolean")


def _validate_fill(element, path, issues, names):
    if "poly" not in element:
        issues.error("FP_E_POLY_MISSING", f"{path}.poly", "fill element requires 'poly'")
    else:
        _validate_points(element["poly"], f"{path}.poly", issues, min_points=3,
                         limit=MAX_POLY_POINTS)
    if "holes" in element:
        holes = element["holes"]
        if not isinstance(holes, list):
            issues.error("FP_E_HOLES_TYPE", f"{path}.holes", "holes must be a list of polygons")
        else:
            if len(holes) > MAX_HOLES:
                issues.error(
                    "FP_E_HOLES_LIMIT", f"{path}.holes", f"at most {MAX_HOLES} holes are supported"
                )
            for index, hole in enumerate(holes):
                _validate_points(hole, f"{path}.holes[{index}]", issues, min_points=3,
                                 limit=MAX_POLY_POINTS)


def _validate_points(points, path, issues, *, min_points, limit):
    if not isinstance(points, list):
        issues.error("FP_E_POINTS_TYPE", path, "points must be a list of [x, y] pairs")
        return
    if len(points) > limit:
        issues.error(
            "FP_E_POINTS_LIMIT", path, f"at most {limit} points are supported (got {len(points)})"
        )
    for index, point in enumerate(points):
        if not _is_point(point):
            issues.error(
                "FP_E_POINT_INVALID",
                f"{path}[{index}]",
                "point must be a finite [x, y] pair of numbers",
            )
        elif any(abs(float(v)) > MAX_POINT_ABS for v in point):
            issues.error(
                "FP_E_POINT_RANGE",
                f"{path}[{index}]",
                f"coordinates must be within +/-{MAX_POINT_ABS:g}",
            )
    if len(points) < min_points:
        issues.error(
            "FP_E_POINTS_MIN",
            path,
            f"at least {min_points} points are required (got {len(points)})",
        )


def _validate_brush(brush, path, issues, names):
    if brush is None:
        return
    if not isinstance(brush, dict):
        issues.error("FP_E_BRUSH_TYPE", path, "brush must be an object")
        return
    if "preset" in brush:
        preset = brush["preset"]
        if not isinstance(preset, str) or preset not in BRUSH_PRESETS:
            issues.error(
                "FP_E_BRUSH_PRESET_UNKNOWN",
                f"{path}.preset",
                f"unknown brush preset {preset!r}",
            )
    if "width" in brush:
        _validate_width(brush["width"], f"{path}.width", issues)
    if "color" in brush and not color_ok(brush["color"], names):
        issues.error(
            "FP_E_COLOR_INVALID",
            f"{path}.color",
            f"invalid colour {brush['color']!r} (hex or palette name expected)",
        )
    if "alpha" in brush and not (
        _is_finite_num(brush["alpha"]) and 0.0 <= float(brush["alpha"]) <= 1.0
    ):
        issues.error("FP_E_ALPHA_RANGE", f"{path}.alpha", "alpha must be a number in [0, 1]")
    if "shape" in brush:
        shape = brush["shape"]
        if not isinstance(shape, str) or shape not in BRUSH_SHAPES:
            issues.error(
                "FP_E_BRUSH_SHAPE",
                f"{path}.shape",
                f"unknown brush shape {shape!r}; expected one of {list(BRUSH_SHAPES)}",
            )
    if "pressure" in brush and not (
        _is_finite_num(brush["pressure"]) and 0.0 <= float(brush["pressure"]) <= 1.0
    ):
        issues.error("FP_E_PRESSURE_RANGE", f"{path}.pressure", "pressure must be in [0, 1]")
    for key in ("jitter", "spacing"):
        if key in brush and not (_is_finite_num(brush[key]) and float(brush[key]) >= 0):
            issues.error("FP_E_RANGE", f"{path}.{key}", f"{key} must be a number >= 0")
    _check_unknown_keys(brush, KNOWN_BRUSH_KEYS, path, issues)


def _validate_width(width, path, issues):
    if _is_num(width):
        if not (_is_finite_num(width) and MIN_WIDTH <= float(width) <= MAX_WIDTH):
            issues.error(
                "FP_E_WIDTH_RANGE",
                path,
                f"width must be a number in [{MIN_WIDTH}, {MAX_WIDTH:g}]",
            )
        return
    if not isinstance(width, list):
        issues.error("FP_E_WIDTH_TYPE", path, "width must be a number or a [[t, w], ...] curve")
        return
    if len(width) < 2:
        issues.error("FP_E_WIDTH_TYPE", path, "width curve needs at least 2 points")
        return
    previous = -1.0
    for index, entry in enumerate(width):
        if not (
            isinstance(entry, (list, tuple))
            and len(entry) == 2
            and all(_is_finite_num(v) for v in entry)
        ):
            issues.error(
                "FP_E_WIDTH_TYPE", f"{path}[{index}]", "width curve entries must be finite [t, w]"
            )
            continue
        t, w = float(entry[0]), float(entry[1])
        if not 0.0 <= t <= 1.0:
            issues.error("FP_E_WIDTH_RANGE", f"{path}[{index}][0]", "curve t must be in [0, 1]")
        if t < previous:
            issues.error(
                "FP_E_WIDTH_ORDER", f"{path}[{index}][0]", "curve t values must be non-decreasing"
            )
        previous = t
        if not 0.0 <= w <= MAX_WIDTH:
            issues.error(
                "FP_E_WIDTH_RANGE", f"{path}[{index}][1]", f"curve w must be in [0, {MAX_WIDTH:g}]"
            )


def _validate_transform(transform, path, issues):
    if transform is None:
        return
    if not isinstance(transform, dict):
        issues.error("FP_E_TRANSFORM_TYPE", path, "transform must be an object")
        return
    if "center" in transform and not _is_point(transform["center"]):
        issues.error(
            "FP_E_POINT_INVALID", f"{path}.center", "transform.center must be a finite [x, y] pair"
        )
    if "anchor" in transform and not _is_point(transform["anchor"]):
        issues.error(
            "FP_E_POINT_INVALID", f"{path}.anchor", "transform.anchor must be a finite [x, y] pair"
        )
    if "scale" in transform:
        scale = transform["scale"]
        scales = scale if isinstance(scale, (list, tuple)) else [scale]
        if not scales or not all(
            _is_finite_num(v) and 0.0 < float(v) <= 64.0 for v in scales
        ):
            issues.error(
                "FP_E_SCALE_RANGE", f"{path}.scale", "scale must be a number (or pair) in (0, 64]"
            )
        elif isinstance(scale, (list, tuple)) and len(scale) != 2:
            issues.error("FP_E_SCALE_RANGE", f"{path}.scale", "scale pair must have 2 entries")
    for key in ("rotate", "skew"):
        if key in transform and not _is_finite_num(transform[key]):
            issues.error("FP_E_TRANSFORM_TYPE", f"{path}.{key}", f"{key} must be a finite number")
    _check_unknown_keys(transform, KNOWN_TRANSFORM_KEYS, path, issues)


def _validate_text_element(element, path, issues, names, layer_ids):
    if "text" not in element:
        issues.error("FP_E_TEXT_MISSING", f"{path}.text", "text element requires 'text'")
    elif not isinstance(element["text"], str) or not element["text"].strip():
        issues.error("FP_E_TEXT_TYPE", f"{path}.text", "text must be a non-empty string")
    elif len(element["text"]) > 2000:
        issues.error("FP_E_TEXT_LENGTH", f"{path}.text", "text must be <= 2000 characters")
    if "as" in element and element["as"] not in TEXT_AS:
        issues.error("FP_E_TEXT_AS", f"{path}.as", f"text mode must be one of {list(TEXT_AS)}")
    _validate_font(element.get("font"), f"{path}.font", issues, names)
    _validate_box(element.get("box"), f"{path}.box", issues)


def _validate_font(font, path, issues, names):
    if font is None:
        return
    if not isinstance(font, dict):
        issues.error("FP_E_FONT_TYPE", path, "font must be an object")
        return
    if "id" in font:
        font_id = font["id"]
        if not isinstance(font_id, str) or not font_id:
            issues.error("FP_E_FONT_TYPE", f"{path}.id", "font.id must be a non-empty string")
        elif font_id not in BUILTIN_FONTS:
            issues.warn(
                "FP_W_FONT_UNKNOWN",
                f"{path}.id",
                f"font id {font_id!r} is not a built-in font {list(BUILTIN_FONTS)}",
            )
    else:
        issues.warn("FP_W_FONT_DEFAULT", path, "font.id is missing; builder will use its default")
    if "size_px" in font and not (
        _is_finite_num(font["size_px"]) and 1.0 <= float(font["size_px"]) <= MAX_FONT_SIZE
    ):
        issues.error(
            "FP_E_FONT_SIZE",
            f"{path}.size_px",
            f"font.size_px must be in [1, {MAX_FONT_SIZE:g}]",
        )
    if "color" in font and not color_ok(font["color"], names):
        issues.error("FP_E_COLOR_INVALID", f"{path}.color", f"invalid colour {font['color']!r}")
    if "alpha" in font and not (
        _is_finite_num(font["alpha"]) and 0.0 <= float(font["alpha"]) <= 1.0
    ):
        issues.error("FP_E_ALPHA_RANGE", f"{path}.alpha", "alpha must be in [0, 1]")
    if "align" in font and font["align"] not in TEXT_ALIGNS:
        issues.error(
            "FP_E_TEXT_ALIGN", f"{path}.align", f"align must be one of {list(TEXT_ALIGNS)}"
        )
    for key in ("spacing", "line_gap"):
        if key in font and not _is_finite_num(font[key]):
            issues.error("FP_E_FONT_TYPE", f"{path}.{key}", f"{key} must be a finite number")
    _check_unknown_keys(font, KNOWN_FONT_KEYS, path, issues)


def _validate_box(box, path, issues):
    if box is None:
        return
    if not (
        isinstance(box, (list, tuple))
        and len(box) == 4
        and all(_is_finite_num(v) for v in box)
        and float(box[2]) > 0
        and float(box[3]) > 0
    ):
        issues.error("FP_E_BOX", path, "box must be [x, y, width, height] with positive size")


def _validate_texts(plan, issues, *, names, layer_ids):
    texts = plan.get("texts")
    if texts is None:
        return
    if not isinstance(texts, list):
        issues.error("FP_E_TEXTS_TYPE", "$.texts", "texts must be a list")
        return
    if len(texts) > MAX_TEXTS:
        issues.error("FP_E_TEXTS_LIMIT", "$.texts", f"at most {MAX_TEXTS} texts are supported")
    seen = set()
    for index, entry in enumerate(texts):
        path = f"$.texts[{index}]"
        if not isinstance(entry, dict):
            issues.error("FP_E_TEXTS_TYPE", path, "text entry must be an object")
            continue
        tid = entry.get("id")
        if not isinstance(tid, str) or not _TEXT_ID_RE.match(tid):
            issues.error("FP_E_TEXT_ID", f"{path}.id", "text id must match ^t[1-9][0-9]{0,3}$")
        elif tid in seen:
            issues.error("FP_E_TEXT_ID", f"{path}.id", f"duplicate text id {tid!r}")
        else:
            seen.add(tid)
        if "text" not in entry:
            issues.error("FP_E_TEXT_MISSING", f"{path}.text", "text entry requires 'text'")
        elif not isinstance(entry["text"], str) or not entry["text"].strip():
            issues.error("FP_E_TEXT_TYPE", f"{path}.text", "text must be a non-empty string")
        elif len(entry["text"]) > 2000:
            issues.error("FP_E_TEXT_LENGTH", f"{path}.text", "text must be <= 2000 characters")
        if "as" in entry and entry["as"] not in TEXT_AS:
            issues.error("FP_E_TEXT_AS", f"{path}.as", f"text mode must be one of {list(TEXT_AS)}")
        if "layer" not in entry:
            issues.error("FP_E_LAYER_MISSING", f"{path}.layer", "text.layer is required")
        elif not _layer_ref_ok(entry["layer"], layer_ids):
            issues.error(
                "FP_E_LAYER_REF",
                f"{path}.layer",
                f"layer {entry['layer']!r} is not declared in $.layers",
            )
        _validate_font(entry.get("font"), f"{path}.font", issues, names)
        _validate_box(entry.get("box"), f"{path}.box", issues)
        if "lines" in entry and not isinstance(entry["lines"], list):
            issues.error("FP_E_TEXT_LINES", f"{path}.lines", "lines must be a list")
        _check_unknown_keys(entry, KNOWN_TEXT_KEYS, path, issues)
