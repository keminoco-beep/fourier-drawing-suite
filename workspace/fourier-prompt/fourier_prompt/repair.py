"""Deterministic repairs for FDSL v2.1 plans (WP-A core scaffold).

Repairs are limited to changes that have exactly one sensible answer:
clipping coordinates into the canvas, dropping duplicate/non-finite points,
closing polygons, clamping numeric ranges, normalising colour spelling and
re-assigning broken/duplicate ids.  Anything that would require guessing
(unknown ``gen`` / brush preset / shape, missing canvas, invalid colour name)
raises :class:`~fourier_prompt.schema.FDSchemaError` instead.

``repair_plan`` never mutates its input.  ``strict=True`` refuses to repair:
it raises when the plan is not already valid (including warnings).
"""

from __future__ import annotations

import copy
import math

from .schema import (
    BRUSH_PRESETS,
    BRUSH_SHAPES,
    DEFAULT_MAX_ELEMENTS,
    ELEMENT_KINDS,
    FDSL_SCHEMA_ALL,
    FDSchemaError,
    GEN_LIBRARY,
    KIND_ALIASES,
    MAX_CANVAS,
    MAX_ELEMENTS_HARD,
    MAX_FONT_SIZE,
    MAX_PALETTE,
    MAX_TAGS,
    MAX_WIDTH,
    MIN_CANVAS,
    MIN_WIDTH,
    _ELEMENT_ID_RE,
    _HEX_RE,
    _PALETTE_NAME_RE,
    _SHORT_HEX_RE,
    _TAG_RE,
    _TEXT_ID_RE,
    _is_finite_num,
    _is_int_like,
    _is_num,
    _layer_ref_ok,
    validate_plan_detailed,
)

WARNING_ACTIONS = (
    "clipped",
    "dropped",
    "normalized",
    "reassigned",
    "clamped",
    "defaulted",
    "closed",
    "replaced",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def repair_plan(plan, *, strict=False):
    """Return ``(repaired_plan, warnings)`` for ``plan``.

    In strict mode nothing is repaired: the plan must validate cleanly
    (warnings included) or :class:`FDSchemaError` with code ``FP_E_STRICT``
    is raised.
    """
    if not isinstance(plan, dict):
        raise FDSchemaError("FP_E_PLAN_TYPE", "plan must be a JSON object", "$")

    if strict:
        report = validate_plan_detailed(plan, strict=True)
        if report["errors"]:
            first = report["errors"][0]
            raise FDSchemaError(
                "FP_E_STRICT",
                "strict mode refuses to repair: "
                f"{len(report['errors'])} issue(s), first {first['code']} "
                f"at {first['path']}: {first['message']}",
                first["path"],
                details=report["errors"],
            )
        # Out-of-canvas coordinates and similar cases are legal per schema but
        # still need repairing; in strict mode those must be refused too.
        _repaired, pending = repair_plan(plan, strict=False)
        if pending:
            first = pending[0]
            raise FDSchemaError(
                "FP_E_STRICT",
                "strict mode refuses to repair: "
                f"{len(pending)} repair(s) needed, first {first['code']} "
                f"at {first['path']}: {first['message']}",
                first["path"],
                details=[dict(item, severity="warning") for item in pending],
            )
        return copy.deepcopy(plan), []

    out = copy.deepcopy(plan)
    warnings = []

    schema = out.get("schema")
    if schema is None:
        raise FDSchemaError("FP_E_SCHEMA_MISSING", "required field 'schema' is missing", "$.schema")
    if not isinstance(schema, str) or schema not in FDSL_SCHEMA_ALL:
        raise FDSchemaError(
            "FP_E_SCHEMA_VERSION",
            f"unsupported schema {schema!r}; expected one of {list(FDSL_SCHEMA_ALL)}",
            "$.schema",
        )

    _repair_palette(out, warnings)
    names = _palette_name_set(out)

    canvas = out.get("canvas")
    if not isinstance(canvas, dict):
        raise FDSchemaError("FP_E_CANVAS_TYPE", "canvas must be an object", "$.canvas")
    width = _repair_canvas_dim(canvas, "width", warnings)
    height = _repair_canvas_dim(canvas, "height", warnings)
    _repair_background(canvas, warnings, names)

    layer_ids = _repair_layers(out, warnings)

    elements = out.get("elements")
    if not isinstance(elements, list):
        raise FDSchemaError("FP_E_ELEMENTS_TYPE", "elements must be a list", "$.elements")

    max_elements = _effective_max_elements(out)
    if len(elements) > max_elements:
        raise FDSchemaError(
            "FP_E_ELEMENT_LIMIT",
            f"{len(elements)} elements exceed the limit of {max_elements}",
            "$.elements",
        )

    used_ids = {
        element.get("id")
        for element in elements
        if isinstance(element, dict) and isinstance(element.get("id"), str)
        and _ELEMENT_ID_RE.match(element.get("id"))
    }
    kept_ids = set()
    for index, element in enumerate(elements):
        _repair_element(
            element,
            index,
            width=width,
            height=height,
            names=names,
            layer_ids=layer_ids,
            used_ids=used_ids,
            kept_ids=kept_ids,
            warnings=warnings,
        )

    texts = out.get("texts")
    if isinstance(texts, list):
        used_text_ids = {
            entry.get("id")
            for entry in texts
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
            and _TEXT_ID_RE.match(entry.get("id"))
        }
        kept_text_ids = set()
        for index, entry in enumerate(texts):
            _repair_text(
                entry,
                index,
                names=names,
                used_ids=used_text_ids,
                kept_ids=kept_text_ids,
                warnings=warnings,
            )

    _repair_constraints(out, warnings)

    report = validate_plan_detailed(out, strict=False)
    if report["errors"]:
        first = report["errors"][0]
        raise FDSchemaError(
            "FP_E_REPAIR_INCOMPLETE",
            f"repair could not fix the plan: {len(report['errors'])} error(s), "
            f"first {first['code']} at {first['path']}: {first['message']}",
            first["path"],
            details=report["errors"],
        )
    return out, warnings


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


def _warn(warnings, code, path, message, action):
    if action not in WARNING_ACTIONS:
        raise AssertionError(f"unknown warning action {action!r}")
    warnings.append(
        {"code": code, "path": path, "message": message, "action": action}
    )


# ---------------------------------------------------------------------------
# Colours / palette
# ---------------------------------------------------------------------------


def _palette_name_set(plan):
    palette = plan.get("palette")
    if isinstance(palette, dict):
        return set(palette.keys())
    return set()


def _normalise_color(value, path, names, warnings, *, allow_names=True):
    """Return ``(colour, changed)``; raises when the colour is not repairable.

    Only spellings the schema rejects are rewritten: 3-digit hex expanded to
    6 digits, bare hex gets ``#``, ``0x`` prefix replaced by ``#``.  Valid
    upper-case hex is kept as authored (schema colours are case-insensitive).
    """
    if not isinstance(value, str):
        raise FDSchemaError(
            "FP_E_COLOR_INVALID", f"colour must be a string (got {value!r})", path
        )
    raw = value.strip()
    lowered = raw.lower()
    if lowered.startswith("0x"):
        candidate = "#" + lowered[2:]
    elif lowered.startswith("#"):
        candidate = raw
    elif _HEX_RE.match("#" + lowered) or _SHORT_HEX_RE.match("#" + lowered):
        candidate = "#" + lowered
    else:
        if allow_names and raw in names:
            return raw, False
        raise FDSchemaError(
            "FP_E_COLOR_INVALID",
            f"unknown colour {value!r} (hex or declared palette name expected)",
            path,
        )
    candidate_lower = candidate.lower()
    if _SHORT_HEX_RE.match(candidate_lower):
        candidate = "#" + "".join(ch * 2 for ch in candidate[1:])
    if not _HEX_RE.match(candidate.lower()):
        raise FDSchemaError("FP_E_COLOR_INVALID", f"invalid hex colour {value!r}", path)
    if candidate != value:
        _warn(
            warnings,
            "FP_W_COLOR_NORMALIZED",
            path,
            f"colour {value!r} normalised to {candidate!r}",
            "normalized",
        )
        return candidate, True
    return value, False


def _repair_palette(out, warnings):
    palette = out.get("palette")
    if palette is None:
        return
    if isinstance(palette, list):
        if len(palette) > MAX_PALETTE:
            raise FDSchemaError(
                "FP_E_PALETTE_LIMIT",
                f"palette may hold at most {MAX_PALETTE} colours",
                "$.palette",
            )
        for index, entry in enumerate(palette):
            palette[index], _ = _normalise_color(
                entry, f"$.palette[{index}]", set(), warnings, allow_names=False
            )
        return
    if isinstance(palette, dict):
        if len(palette) > MAX_PALETTE:
            raise FDSchemaError(
                "FP_E_PALETTE_LIMIT",
                f"palette may hold at most {MAX_PALETTE} colours",
                "$.palette",
            )
        for key in list(palette):
            if not isinstance(key, str) or not _PALETTE_NAME_RE.match(key):
                raise FDSchemaError(
                    "FP_E_PALETTE_NAME",
                    f"invalid palette name {key!r}",
                    f"$.palette[{key!r}]",
                )
            palette[key], _ = _normalise_color(
                palette[key], f"$.palette[{key!r}]", set(), warnings, allow_names=False
            )
        return
    raise FDSchemaError("FP_E_PALETTE_TYPE", "palette must be a list or an object", "$.palette")


# ---------------------------------------------------------------------------
# Canvas / layers / constraints
# ---------------------------------------------------------------------------


def _repair_canvas_dim(canvas, key, warnings):
    value = canvas.get(key)
    if not _is_num(value) or not math.isfinite(float(value)):
        raise FDSchemaError(
            "FP_E_CANVAS_TYPE", f"canvas.{key} must be a finite number", f"$.canvas.{key}"
        )
    ivalue = int(round(float(value)))
    clamped = min(max(ivalue, MIN_CANVAS), MAX_CANVAS)
    if clamped != value:
        canvas[key] = clamped
        _warn(
            warnings,
            "FP_W_CANVAS_CLAMPED",
            f"$.canvas.{key}",
            f"canvas.{key} {value!r} clamped to {clamped}",
            "clamped",
        )
    return clamped


def _repair_background(canvas, warnings, names):
    if "background" not in canvas or canvas["background"] is None:
        return
    background = canvas["background"]
    if isinstance(background, (list, tuple)):
        if len(background) not in (3, 4):
            raise FDSchemaError(
                "FP_E_COLOR_INVALID",
                "background list must have 3 or 4 channels",
                "$.canvas.background",
            )
        channels = []
        changed = False
        for index, channel in enumerate(background):
            if not _is_num(channel) or not math.isfinite(float(channel)):
                raise FDSchemaError(
                    "FP_E_COLOR_INVALID",
                    "background channels must be finite numbers",
                    f"$.canvas.background[{index}]",
                )
            value = min(max(int(round(float(channel))), 0), 255)
            changed = changed or value != channel
            channels.append(value)
        if changed:
            canvas["background"] = channels
            _warn(
                warnings,
                "FP_W_BACKGROUND_NORMALIZED",
                "$.canvas.background",
                f"background rounded/clamped to {channels}",
                "normalized",
            )
        return
    canvas["background"], _ = _normalise_color(
        background, "$.canvas.background", names, warnings
    )


def _repair_layers(out, warnings):
    layers = out.get("layers")
    if layers is None:
        return None
    if not isinstance(layers, list):
        return None  # reported by final validation
    ids = []
    seen = set()
    for index, layer in enumerate(layers):
        path = f"$.layers[{index}]"
        if not isinstance(layer, dict):
            continue  # reported by final validation
        if "id" not in layer:
            raise FDSchemaError("FP_E_LAYER_ID", "layer.id is required", f"{path}.id")
        lid = layer["id"]
        if isinstance(lid, bool) or not isinstance(lid, (int, str)):
            raise FDSchemaError(
                "FP_E_LAYER_ID", "layer.id must be an int or string", f"{path}.id"
            )
        marker = (type(lid).__name__, lid)
        if marker in seen:
            raise FDSchemaError(
                "FP_E_LAYER_DUPLICATE", f"duplicate layer id {lid!r}", "$.layers"
            )
        seen.add(marker)
        ids.append(lid)
        if "order" in layer and _is_num(layer["order"]):
            value = layer["order"]
            ivalue = max(int(round(float(value))), 0)
            if ivalue != value:
                layer["order"] = ivalue
                _warn(
                    warnings,
                    "FP_W_LAYER_ORDER_CLAMPED",
                    f"{path}.order",
                    f"layer.order {value!r} normalised to {ivalue}",
                    "clamped",
                )
        if "opacity" in layer and _is_num(layer["opacity"]):
            value = float(layer["opacity"])
            if math.isfinite(value):
                clamped = min(max(value, 0.0), 1.0)
                if clamped != layer["opacity"]:
                    layer["opacity"] = clamped
                    _warn(
                        warnings,
                        "FP_W_OPACITY_CLAMPED",
                        f"{path}.opacity",
                        f"layer.opacity {layer['opacity']!r} clamped to {clamped}",
                        "clamped",
                    )
    return ids


def _repair_constraints(out, warnings):
    constraints = out.get("constraints")
    if not isinstance(constraints, dict):
        return
    bounds = {
        "max_elements": (1, MAX_ELEMENTS_HARD),
        "max_ops": (1, 4096),
        "text_max": (1, 1000),
        "max_layers": (1, 32),
    }
    for key, (low, high) in bounds.items():
        if key in constraints and _is_num(constraints[key]) and math.isfinite(
            float(constraints[key])
        ):
            value = constraints[key]
            ivalue = min(max(int(round(float(value))), low), high)
            if ivalue != value:
                constraints[key] = ivalue
                _warn(
                    warnings,
                    "FP_W_CONSTRAINT_CLAMPED",
                    f"$.constraints.{key}",
                    f"{key} {value!r} clamped to {ivalue}",
                    "clamped",
                )
    if "keep_margin" in constraints and _is_num(constraints["keep_margin"]):
        value = float(constraints["keep_margin"])
        if math.isfinite(value):
            clamped = min(max(value, 0.0), float(MAX_CANVAS))
            if clamped != constraints["keep_margin"]:
                constraints["keep_margin"] = clamped
                _warn(
                    warnings,
                    "FP_W_CONSTRAINT_CLAMPED",
                    "$.constraints.keep_margin",
                    f"keep_margin {constraints['keep_margin']!r} clamped to {clamped}",
                    "clamped",
                )


def _effective_max_elements(out):
    constraints = out.get("constraints")
    if isinstance(constraints, dict):
        value = constraints.get("max_elements")
        if _is_int_like(value) and 1 <= int(value) <= MAX_ELEMENTS_HARD:
            return int(value)
    return DEFAULT_MAX_ELEMENTS


# ---------------------------------------------------------------------------
# Elements
# ---------------------------------------------------------------------------


def _next_free_id(used, pattern, prefix):
    numbers = []
    for value in used:
        if isinstance(value, str) and pattern.match(value):
            try:
                numbers.append(int(value[len(prefix):]))
            except ValueError:  # pragma: no cover - regex guarantees digits
                continue
    candidate = max(numbers, default=0) + 1
    while f"{prefix}{candidate}" in used:
        candidate += 1
    return f"{prefix}{candidate}"


def _repair_element(
    element, index, *, width, height, names, layer_ids, used_ids, kept_ids, warnings
):
    path = f"$.elements[{index}]"
    if not isinstance(element, dict):
        return  # reported by final validation

    kind_raw = element.get("kind", element.get("type"))
    if kind_raw is None:
        raise FDSchemaError("FP_E_ELEMENT_KIND_MISSING", "element has no 'kind'", path)
    if not isinstance(kind_raw, str):
        raise FDSchemaError("FP_E_ELEMENT_KIND", "element 'kind' must be a string", path)
    kind = KIND_ALIASES.get(kind_raw, kind_raw)
    if kind not in ELEMENT_KINDS:
        raise FDSchemaError(
            "FP_E_ELEMENT_KIND",
            f"unsupported element kind {kind_raw!r}; expected one of {list(ELEMENT_KINDS)}",
            f"{path}.kind",
        )

    # Stable, unique ids: the first occurrence keeps its id, duplicates and
    # malformed ids are deterministically re-assigned to the next free id.
    if "id" in element:
        eid = element["id"]
        valid = isinstance(eid, str) and bool(_ELEMENT_ID_RE.match(eid))
        if not valid:
            new_id = _next_free_id(used_ids, _ELEMENT_ID_RE, "e")
            used_ids.add(new_id)
            kept_ids.add(new_id)
            element["id"] = new_id
            _warn(
                warnings,
                "FP_W_ID_REASSIGNED",
                f"{path}.id",
                f"invalid element id {eid!r} re-assigned to {new_id!r}",
                "reassigned",
            )
        elif eid in kept_ids:
            new_id = _next_free_id(used_ids, _ELEMENT_ID_RE, "e")
            used_ids.add(new_id)
            kept_ids.add(new_id)
            element["id"] = new_id
            _warn(
                warnings,
                "FP_W_ID_REASSIGNED",
                f"{path}.id",
                f"duplicate element id {eid!r} re-assigned to {new_id!r}",
                "reassigned",
            )
        else:
            kept_ids.add(eid)

    _repair_layer_ref(element, path, layer_ids, warnings)

    if "tags" in element:
        tags = element["tags"]
        if not isinstance(tags, list):
            del element["tags"]
            _warn(
                warnings,
                "FP_W_TAGS_DROPPED",
                f"{path}.tags",
                "tags was not a list; dropped",
                "dropped",
            )
        else:
            kept = []
            for tag_index, tag in enumerate(tags):
                if isinstance(tag, str) and _TAG_RE.match(tag):
                    kept.append(tag)
                else:
                    _warn(
                        warnings,
                        "FP_W_TAG_DROPPED",
                        f"{path}.tags[{tag_index}]",
                        f"invalid tag {tag!r} dropped",
                        "dropped",
                    )
            if len(kept) > MAX_TAGS:
                _warn(
                    warnings,
                    "FP_W_TAGS_TRUNCATED",
                    f"{path}.tags",
                    f"{len(kept)} tags truncated to {MAX_TAGS}",
                    "dropped",
                )
                kept = kept[:MAX_TAGS]
            element["tags"] = kept

    if "group" in element:
        group = element["group"]
        if isinstance(group, str) and group.strip():
            if len(group) > 64:
                element["group"] = group[:64]
                _warn(
                    warnings,
                    "FP_W_GROUP_TRUNCATED",
                    f"{path}.group",
                    "group truncated to 64 characters",
                    "clamped",
                )
        else:
            del element["group"]
            _warn(
                warnings,
                "FP_W_GROUP_DROPPED",
                f"{path}.group",
                f"invalid group {group!r} dropped",
                "dropped",
            )

    if "opacity" in element and _is_num(element["opacity"]):
        value = float(element["opacity"])
        if math.isfinite(value):
            clamped = min(max(value, 0.0), 1.0)
            if clamped != element["opacity"]:
                element["opacity"] = clamped
                _warn(
                    warnings,
                    "FP_W_OPACITY_CLAMPED",
                    f"{path}.opacity",
                    f"opacity {element['opacity']!r} clamped to {clamped}",
                    "clamped",
                )

    if "color" in element:
        element["color"], _ = _normalise_color(
            element["color"], f"{path}.color", names, warnings
        )

    _repair_brush(element.get("brush"), path, names, warnings)
    _repair_transform(element.get("transform"), path, warnings)
    if "params" in element:
        element["params"] = _repair_numbers(element["params"], f"{path}.params", warnings)

    if kind == "fourier_gen":
        gen = element.get("gen")
        if not isinstance(gen, str) or gen not in GEN_LIBRARY:
            raise FDSchemaError(
                "FP_E_GEN_UNKNOWN",
                f"unknown gen {gen!r}; expected one of {list(GEN_LIBRARY)}",
                f"{path}.gen",
            )
        if "params" in element and not isinstance(element["params"], dict):
            raise FDSchemaError(
                "FP_E_PARAMS_TYPE", "params must be an object", f"{path}.params"
            )
    elif kind == "stroke":
        element["points"] = _repair_points(
            element.get("points"),
            f"{path}.points",
            closed=bool(element.get("closed") is True),
            clip_w=width,
            clip_h=height,
            min_points=2,
            warnings=warnings,
        )
    elif kind == "fill":
        # Fill polygons are *implicitly* closed by the FDSL contract (see the
        # reference example in 文生图方案评估 v2 §3.3), so an unclosed `poly`
        # is canonical and must not be rewritten.
        element["poly"] = _repair_points(
            element.get("poly"),
            f"{path}.poly",
            closed=False,
            clip_w=width,
            clip_h=height,
            min_points=3,
            warnings=warnings,
        )
        holes = element.get("holes")
        if isinstance(holes, list):
            repaired_holes = []
            for hole_index, hole in enumerate(holes):
                repaired_holes.append(
                    _repair_points(
                        hole,
                        f"{path}.holes[{hole_index}]",
                        closed=False,
                        clip_w=width,
                        clip_h=height,
                        min_points=3,
                        warnings=warnings,
                    )
                )
            element["holes"] = repaired_holes
    elif kind == "text":
        _repair_text_payload(element, path, names, warnings)


def _repair_layer_ref(element, path, layer_ids, warnings):
    ref = element.get("layer")
    if _layer_ref_ok(ref, layer_ids):
        return
    fallback = layer_ids[0] if layer_ids else 0
    element["layer"] = fallback
    reason = "missing" if ref is None else f"invalid ({ref!r})"
    _warn(
        warnings,
        "FP_W_LAYER_REPAIRED",
        f"{path}.layer",
        f"element layer {reason} repaired to {fallback!r}",
        "defaulted",
    )


def _repair_brush(brush, path, names, warnings):
    if brush is None:
        return
    if not isinstance(brush, dict):
        raise FDSchemaError("FP_E_BRUSH_TYPE", "brush must be an object", f"{path}.brush")
    if "preset" in brush:
        preset = brush["preset"]
        if not isinstance(preset, str) or preset not in BRUSH_PRESETS:
            raise FDSchemaError(
                "FP_E_BRUSH_PRESET_UNKNOWN",
                f"unknown brush preset {preset!r}",
                f"{path}.brush.preset",
            )
    if "shape" in brush:
        shape = brush["shape"]
        if not isinstance(shape, str) or shape not in BRUSH_SHAPES:
            raise FDSchemaError(
                "FP_E_BRUSH_SHAPE",
                f"unknown brush shape {shape!r}; expected one of {list(BRUSH_SHAPES)}",
                f"{path}.brush.shape",
            )
    if "width" in brush:
        brush["width"] = _repair_width(brush["width"], f"{path}.brush.width", warnings)
    if "alpha" in brush and _is_num(brush["alpha"]):
        value = float(brush["alpha"])
        if math.isfinite(value):
            clamped = min(max(value, 0.0), 1.0)
            if clamped != brush["alpha"]:
                brush["alpha"] = clamped
                _warn(
                    warnings,
                    "FP_W_ALPHA_CLAMPED",
                    f"{path}.brush.alpha",
                    f"alpha {brush['alpha']!r} clamped to {clamped}",
                    "clamped",
                )
    if "pressure" in brush and _is_num(brush["pressure"]):
        value = float(brush["pressure"])
        if math.isfinite(value):
            clamped = min(max(value, 0.0), 1.0)
            if clamped != brush["pressure"]:
                brush["pressure"] = clamped
                _warn(
                    warnings,
                    "FP_W_PRESSURE_CLAMPED",
                    f"{path}.brush.pressure",
                    f"pressure {brush['pressure']!r} clamped to {clamped}",
                    "clamped",
                )
    for key in ("jitter", "spacing"):
        if key in brush and _is_num(brush[key]):
            value = float(brush[key])
            if math.isfinite(value) and value < 0:
                brush[key] = 0.0
                _warn(
                    warnings,
                    "FP_W_RANGE_CLAMPED",
                    f"{path}.brush.{key}",
                    f"{key} clamped to 0",
                    "clamped",
                )
    if "color" in brush:
        brush["color"], _ = _normalise_color(
            brush["color"], f"{path}.brush.color", names, warnings
        )


def _repair_width(width, path, warnings):
    if _is_num(width):
        if not math.isfinite(float(width)):
            _warn(
                warnings,
                "FP_W_NONFINITE_REPLACED",
                path,
                "non-finite width replaced with the minimum width",
                "replaced",
            )
            return MIN_WIDTH
        value = float(width)
        clamped = min(max(value, MIN_WIDTH), MAX_WIDTH)
        if clamped != width:
            _warn(
                warnings,
                "FP_W_WIDTH_CLAMPED",
                path,
                f"width {width!r} clamped to {clamped}",
                "clamped",
            )
        return clamped if clamped != width else width

    if not isinstance(width, list):
        raise FDSchemaError(
            "FP_E_WIDTH_TYPE",
            "width must be a number or a [[t, w], ...] curve",
            path,
        )

    entries = []
    changed = False
    for index, entry in enumerate(width):
        if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
            raise FDSchemaError(
                "FP_E_WIDTH_TYPE", "width curve entries must be [t, w] pairs", f"{path}[{index}]"
            )
        t, w = entry
        if not _is_finite_num(t):
            t = 0.0
            changed = True
        if not _is_finite_num(w):
            w = 0.0
            changed = True
        ct = min(max(float(t), 0.0), 1.0)
        cw = min(max(float(w), 0.0), MAX_WIDTH)
        if ct != t or cw != w:
            changed = True
        entries.append([ct, cw])
    if not entries:
        raise FDSchemaError("FP_E_WIDTH_TYPE", "width curve must not be empty", path)

    entries.sort(key=lambda item: item[0])
    merged = []
    for entry in entries:
        if merged and entry[0] == merged[-1][0]:
            merged[-1] = entry
            changed = True
        else:
            merged.append(entry)
    if merged[0][0] > 0.0:
        merged.insert(0, [0.0, merged[0][1]])
        changed = True
    if merged[-1][0] < 1.0:
        merged.append([1.0, merged[-1][1]])
        changed = True
    if len(merged) < 2:
        merged = [[0.0, merged[0][1]], [1.0, merged[0][1]]]
        changed = True
    if changed:
        _warn(
            warnings,
            "FP_W_WIDTH_NORMALIZED",
            path,
            "width curve sorted/clamped and pinned to t=0..1",
            "normalized",
        )
    return merged


def _repair_transform(transform, path, warnings):
    if transform is None or not isinstance(transform, dict):
        return
    for key in ("center", "anchor"):
        if key in transform and isinstance(transform[key], (list, tuple)) and len(transform[key]) == 2:
            point = transform[key]
            repaired = []
            changed = False
            for value in point:
                if _is_finite_num(value):
                    repaired.append(float(value))
                    if repaired[-1] != value:
                        changed = True
                else:
                    repaired.append(0.0)
                    changed = True
            if changed:
                transform[key] = repaired
                _warn(
                    warnings,
                    "FP_W_NONFINITE_REPLACED",
                    f"{path}.transform.{key}",
                    f"non-finite transform.{key} replaced with 0",
                    "replaced",
                )
    if "scale" in transform and _is_num(transform["scale"]):
        original = transform["scale"]
        value = float(original)
        if not math.isfinite(value) or value <= 0.0:
            scale = 1.0
        else:
            scale = min(max(value, 0.1), 64.0)
        if scale != original:
            transform["scale"] = scale
            _warn(
                warnings,
                "FP_W_SCALE_REPAIRED",
                f"{path}.transform.scale",
                f"scale {original!r} repaired to {scale}",
                "clamped",
            )
    for key in ("rotate", "skew"):
        if key in transform and not _is_finite_num(transform[key]):
            transform[key] = 0.0
            _warn(
                warnings,
                "FP_W_NONFINITE_REPLACED",
                f"{path}.transform.{key}",
                f"non-finite {key} replaced with 0",
                "replaced",
            )


def _repair_numbers(value, path, warnings):
    """Replace non-finite floats inside params-like structures with 0.0."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _warn(
                warnings,
                "FP_W_NONFINITE_REPLACED",
                path,
                "non-finite number replaced with 0",
                "replaced",
            )
            return 0.0
        return value
    if isinstance(value, dict):
        for key in list(value):
            value[key] = _repair_numbers(value[key], f"{path}.{key}", warnings)
        return value
    if isinstance(value, list):
        for index in range(len(value)):
            value[index] = _repair_numbers(value[index], f"{path}[{index}]", warnings)
        return value
    return value


def _repair_points(points, path, *, closed, clip_w, clip_h, min_points, warnings):
    if not isinstance(points, list):
        raise FDSchemaError("FP_E_POINTS_TYPE", "points must be a list", path)

    repaired = []
    dropped = 0
    clipped = 0
    for index, point in enumerate(points):
        if not (isinstance(point, (list, tuple)) and len(point) == 2):
            raise FDSchemaError(
                "FP_E_POINT_INVALID",
                "point must be an [x, y] pair",
                f"{path}[{index}]",
            )
        x, y = point
        if not _is_finite_num(x) or not _is_finite_num(y):
            dropped += 1
            continue
        cx = min(max(float(x), 0.0), float(clip_w))
        cy = min(max(float(y), 0.0), float(clip_h))
        if cx != x or cy != y:
            clipped += 1
        repaired.append([cx, cy])

    if dropped:
        _warn(
            warnings,
            "FP_W_NONFINITE_DROPPED",
            path,
            f"{dropped} point(s) with non-finite coordinates dropped",
            "dropped",
        )
    if clipped:
        _warn(
            warnings,
            "FP_W_POINT_CLIPPED",
            path,
            f"{clipped} point(s) clipped into the canvas",
            "clipped",
        )

    deduped = []
    duplicate = 0
    for point in repaired:
        if deduped and point == deduped[-1]:
            duplicate += 1
            continue
        deduped.append(point)
    if duplicate:
        _warn(
            warnings,
            "FP_W_POINT_DEDUP",
            path,
            f"{duplicate} duplicate consecutive point(s) removed",
            "dropped",
        )
    repaired = deduped

    if len(repaired) < min_points:
        raise FDSchemaError(
            "FP_E_POINT_INVALID",
            f"repair left {len(repaired)} point(s); at least {min_points} are required",
            path,
        )

    if closed and repaired[0] != repaired[-1]:
        repaired.append(list(repaired[0]))
        _warn(
            warnings,
            "FP_W_POLY_CLOSED",
            path,
            "polygon explicitly closed by repeating the first point",
            "closed",
        )

    return repaired


# ---------------------------------------------------------------------------
# Texts
# ---------------------------------------------------------------------------


def _repair_text(entry, index, *, names, used_ids, kept_ids, warnings):
    path = f"$.texts[{index}]"
    if not isinstance(entry, dict):
        return  # reported by final validation
    tid = entry.get("id")
    if not isinstance(tid, str) or not _TEXT_ID_RE.match(tid) or tid in kept_ids:
        new_id = _next_free_id(used_ids, _TEXT_ID_RE, "t")
        used_ids.add(new_id)
        kept_ids.add(new_id)
        entry["id"] = new_id
        _warn(
            warnings,
            "FP_W_TEXT_ID_REASSIGNED",
            f"{path}.id",
            f"invalid or duplicate text id {tid!r} re-assigned to {new_id!r}",
            "reassigned",
        )
    else:
        kept_ids.add(tid)
    _repair_text_payload(entry, path, names, warnings)


def _repair_text_payload(entry, path, names, warnings):
    text = entry.get("text")
    if not isinstance(text, str) or not text.strip():
        raise FDSchemaError(
            "FP_E_TEXT_MISSING", "text entry requires a non-empty string", f"{path}.text"
        )
    font = entry.get("font")
    if isinstance(font, dict):
        if "size_px" in font and _is_num(font["size_px"]):
            value = float(font["size_px"])
            if math.isfinite(value):
                clamped = min(max(value, 1.0), MAX_FONT_SIZE)
                if clamped != font["size_px"]:
                    font["size_px"] = clamped
                    _warn(
                        warnings,
                        "FP_W_FONT_SIZE_CLAMPED",
                        f"{path}.font.size_px",
                        f"font.size_px {font['size_px']!r} clamped to {clamped}",
                        "clamped",
                    )
        if "alpha" in font and _is_num(font["alpha"]):
            value = float(font["alpha"])
            if math.isfinite(value):
                clamped = min(max(value, 0.0), 1.0)
                if clamped != font["alpha"]:
                    font["alpha"] = clamped
                    _warn(
                        warnings,
                        "FP_W_ALPHA_CLAMPED",
                        f"{path}.font.alpha",
                        f"font.alpha {font['alpha']!r} clamped to {clamped}",
                        "clamped",
                    )
        if "color" in font:
            font["color"], _ = _normalise_color(
                font["color"], f"{path}.font.color", names, warnings
            )
    box = entry.get("box")
    if isinstance(box, (list, tuple)) and len(box) == 4:
        repaired = []
        changed = False
        for value in box:
            if not _is_finite_num(value):
                repaired.append(0.0)
                changed = True
            else:
                repaired.append(float(value))
        if repaired[2] <= 0.0:
            repaired[2] = 1.0
            changed = True
        if repaired[3] <= 0.0:
            repaired[3] = 1.0
            changed = True
        if changed:
            entry["box"] = repaired
            _warn(
                warnings,
                "FP_W_BOX_REPAIRED",
                f"{path}.box",
                "box repaired to finite positive size",
                "normalized",
            )
