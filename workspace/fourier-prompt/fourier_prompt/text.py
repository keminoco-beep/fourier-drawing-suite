"""Text layout + glyph outline baking for outline mode (WP-K).

The compiler freezes text into plain fill polygons here; the rendered ``.fbs``
contains **no font reference**, so the skill renderer stays font-free and
cross-platform pixel results stay L0.

Pipeline::

    text string --layout_text--> [(gid, x, baseline, advance)]
        --glyph_contours (fontTools recording pen, bezier flatten ~0.25 px)-->
        rings in font units --classify outer/hole (nesting)--> canonical
        fill elements ``{"kind":"fill","poly":...,"holes":...}`` in canvas px.

Not supported in batch 5 (documented, fail-loud): ``as="glyph"`` (embedded font
rasterisation), vertical layout, kerning/ligatures/tracking, bidi/RTL,
punctuation squeezing.  See ``reports/WP-K.md``.
"""

from __future__ import annotations

import math

from . import fonts

__all__ = [
    "FLATTEN_PX",
    "MAX_POLY_POINTS",
    "TextError",
    "glyph_contours",
    "layout_text",
    "text_fills",
]

#: Bezier flattening tolerance in **output pixels** (KICKOFF v5 §2.3).
FLATTEN_PX = 0.25
#: schema.MAX_POLY_POINTS -- one contour above this is a hard error.
MAX_POLY_POINTS = 8192
DEFAULT_SIZE_PX = 32.0
DEFAULT_LINE_HEIGHT = 1.2


class TextError(RuntimeError):
    """Structured text failure (``FP_E_TEXT_*``)."""

    def __init__(self, code: str, message: str, path: str = ""):
        self.code = str(code)
        self.message = str(message)
        self.path = str(path)
        super().__init__(f"{self.code}: {self.message}" + (f" [{self.path}]" if self.path else ""))

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "path": self.path}


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------


def _finite(value, name: str, path: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TextError("FP_E_TEXT_PARAM", f"{name} must be a number", path) from exc
    if not math.isfinite(number):
        raise TextError("FP_E_TEXT_PARAM", f"{name} must be finite", path)
    return number


def _unwrap_box(box, canvas) -> tuple[float, float, float, float] | None:
    if box is None:
        if isinstance(canvas, dict) and canvas.get("width") and canvas.get("height"):
            width = _finite(canvas["width"], "canvas.width", "$.canvas")
            height = _finite(canvas["height"], "canvas.height", "$.canvas")
            margin = 0.0
            return (margin, margin, width - 2 * margin, height - 2 * margin)
        return None
    if isinstance(box, dict):
        try:
            values = (box["x"], box["y"], box["w"], box["h"])
        except KeyError as exc:
            raise TextError("FP_E_TEXT_BOX", "box needs x/y/w/h", "$.box") from exc
    elif isinstance(box, (list, tuple)) and len(box) == 4:
        values = box
    else:
        raise TextError("FP_E_TEXT_BOX", "box must be [x, y, width, height] or {x,y,w,h}", "$.box")
    x, y, width, height = (_finite(v, "box component", "$.box") for v in values)
    if width <= 0 or height <= 0:
        raise TextError("FP_E_TEXT_BOX", "box width/height must be positive", "$.box")
    return x, y, width, height


def _point_in_ring(point, ring) -> bool:
    """Even-odd ray casting; points are also y-flipped by the caller so this is orientation-free."""
    x, y = point
    inside = False
    count = len(ring)
    j = count - 1
    for i in range(count):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y):
            cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < cross:
                inside = not inside
        j = i
    return inside


def _signed_area(ring) -> float:
    total = 0.0
    count = len(ring)
    for i in range(count):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % count]
        total += x0 * y1 - x1 * y0
    return 0.5 * total


def _classify_rings(rings: list[list[tuple[float, float]]]) -> list[dict]:
    """Classify rings into outer polygons and holes using winding order.

    TrueType outers are clockwise (negative signed area with y-up), CFF outers
    are counter-clockwise; the largest ring decides the convention for the
    glyph.  Winding -- not nesting -- is authoritative: Noto CJK has glyphs with
    **overlapping outer contours** (e.g. "合" in the packaged subset), so a
    nested ring with the outer winding must stay a fill, not become a hole.
    Holes are attached to the smallest enclosing positive ring; even-depth
    islands come back as their own fills ordered after their parent.
    """
    count = len(rings)
    if not count:
        return []
    areas = [_signed_area(ring) for ring in rings]
    largest = max(range(count), key=lambda index: abs(areas[index]))
    outer_positive = areas[largest] > 0
    positives = [
        index for index in range(count) if (areas[index] > 0) == outer_positive
    ]
    holes: dict[int, list[int]] = {index: [] for index in positives}
    positive_set = set(positives)
    for index in range(count):
        if index in positive_set:
            continue
        probe = rings[index][0]
        containers = [
            other for other in positives if _point_in_ring(probe, rings[other])
        ]
        if not containers:
            # Defensive: a counter without a containing outer is treated as a
            # fill (painting ink twice is safe; punching a hole is not).
            positives.append(index)
            holes[index] = []
            continue
        parent = min(containers, key=lambda other: abs(areas[other]))
        holes[parent].append(index)

    depth: dict[int, int] = {}
    for index in positives:
        probe = rings[index][0]
        depth[index] = sum(
            1
            for other in positives
            if other != index and _point_in_ring(probe, rings[other])
        )
    fills = [
        {"outer": index, "holes": holes[index], "depth": depth[index]}
        for index in positives
    ]
    fills.sort(key=lambda entry: (entry["depth"], entry["outer"]))
    return fills


# ---------------------------------------------------------------------------
# Glyph outlines
# ---------------------------------------------------------------------------


def _flatten_quadratic(p0, p1, p2, tolerance, out):
    length = math.dist(p0, p1) + math.dist(p1, p2)
    steps = max(1, int(math.ceil(length / tolerance))) if tolerance > 0 else 1
    for step in range(1, steps + 1):
        t = step / steps
        mt = 1.0 - t
        out.append(
            (
                mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0],
                mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1],
            )
        )


def _flatten_cubic(p0, p1, p2, p3, tolerance, out):
    length = math.dist(p0, p1) + math.dist(p1, p2) + math.dist(p2, p3)
    steps = max(1, int(math.ceil(length / tolerance))) if tolerance > 0 else 1
    for step in range(1, steps + 1):
        t = step / steps
        mt = 1.0 - t
        a, b, c, d = mt * mt * mt, 3 * mt * mt * t, 3 * mt * t * t, t * t * t
        out.append(
            (
                a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
            )
        )


def _pen_rings(font: fonts.FontHandle, glyph_id: int, tolerance: float) -> list[list[tuple[float, float]]]:
    """Record + flatten one glyph into implicitly closed rings (font units, y up)."""
    try:
        from fontTools.pens.recordingPen import DecomposingRecordingPen  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - environment guard
        raise TextError("FP_E_TEXT_FONT", f"fontTools is required for outlines: {exc}") from exc
    if not isinstance(glyph_id, int) or isinstance(glyph_id, bool):
        raise TextError("FP_E_TEXT_GLYPH", f"glyph id must be an int (got {glyph_id!r})")
    if not 0 <= glyph_id < len(font.glyph_order):
        raise TextError("FP_E_TEXT_GLYPH", f"glyph id {glyph_id} outside font glyph count {len(font.glyph_order)}")
    pen = DecomposingRecordingPen(font.glyph_set)
    try:
        font.glyph_set[font.glyph_name(glyph_id)].draw(pen)
    except Exception as exc:
        raise TextError("FP_E_TEXT_GLYPH", f"cannot draw glyph {glyph_id} ({font.glyph_name(glyph_id)}): {exc}") from exc

    rings: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    start = None
    last = None

    def flush():
        nonlocal current, start, last
        if len(current) >= 3:
            deduped = [current[0]]
            for point in current[1:]:
                if math.dist(point, deduped[-1]) > 1e-9:
                    deduped.append(point)
            if len(deduped) >= 3 and abs(_signed_area(deduped)) > 1e-9:
                if math.dist(deduped[0], deduped[-1]) <= 1e-9:
                    deduped.pop()
                if len(deduped) < 3:
                    pass
                elif len(deduped) > MAX_POLY_POINTS:
                    raise TextError(
                        "FP_E_TEXT_GEOMETRY",
                        f"flattened contour has {len(deduped)} points (> {MAX_POLY_POINTS}); "
                        "reduce font size or raise the flattening tolerance",
                    )
                else:
                    rings.append(deduped)
        current = []
        start = None
        last = None

    for operation, args in pen.value:
        if operation == "moveTo":
            flush()
            point = (float(args[0][0]), float(args[0][1]))
            current = [point]
            start = last = point
        elif operation == "lineTo":
            point = (float(args[0][0]), float(args[0][1]))
            if last is not None and math.dist(point, last) > 1e-9:
                current.append(point)
            last = point
        elif operation == "qCurveTo":
            raw = list(args)
            if not raw:  # pragma: no cover - malformed font
                continue
            closed = raw[-1] is None
            offs = raw[:-1]
            end = start if closed else (float(raw[-1][0]), float(raw[-1][1]))
            if start is None or last is None:  # pragma: no cover - malformed font
                raise TextError("FP_E_TEXT_GLYPH", "quadratic curve without a current point")
            previous = last
            if offs:
                for index, off in enumerate(offs):
                    control = (float(off[0]), float(off[1]))
                    if index < len(offs) - 1:
                        following = (float(offs[index + 1][0]), float(offs[index + 1][1]))
                        midpoint = ((control[0] + following[0]) / 2.0, (control[1] + following[1]) / 2.0)
                        _flatten_quadratic(previous, control, midpoint, tolerance, current)
                        previous = midpoint
                    else:
                        _flatten_quadratic(previous, control, end, tolerance, current)
                        previous = end
            else:
                current.append(end)
            last = end
            if closed:
                flush()
        elif operation == "curveTo":
            if len(args) != 3:  # pragma: no cover - malformed font
                raise TextError("FP_E_TEXT_GLYPH", "cubic curve needs 3 control points")
            if last is None:  # pragma: no cover
                raise TextError("FP_E_TEXT_GLYPH", "cubic curve without a current point")
            p1 = (float(args[0][0]), float(args[0][1]))
            p2 = (float(args[1][0]), float(args[1][1]))
            p3 = (float(args[2][0]), float(args[2][1]))
            _flatten_cubic(last, p1, p2, p3, tolerance, current)
            last = p3
        elif operation == "closePath":
            flush()
        elif operation == "endPath":  # pragma: no cover - open contours are not glyph outlines
            flush()
        else:  # pragma: no cover - addComponent is decomposed by the pen
            raise TextError("FP_E_TEXT_GLYPH", f"unsupported pen operation {operation!r}")
    flush()
    return rings


def glyph_contours(
    font,
    glyph_id: int,
    *,
    size_px: float | None = None,
    tol_px: float = FLATTEN_PX,
):
    """Return flattened, implicitly closed rings for ``glyph_id`` in **font units**.

    Rings are flattened so that each bezier segment is split into
    ``max(1, ceil(segment_length / tol_px))`` pieces at ``size_px``; ``tol_px``
    is converted to font units with the font's ``unitsPerEm``.  The first and
    last points are never duplicates (closure is implicit).
    """
    handle = fonts.load_font(font)
    if size_px is None:
        size_px = 64.0
    size = _finite(size_px, "size_px", "$.font.size_px")
    if size <= 0:
        raise TextError("FP_E_TEXT_PARAM", "size_px must be > 0", "$.font.size_px")
    tolerance = max(_finite(tol_px, "tol_px", "$"), 1e-3) * handle.upem / size
    key = (int(glyph_id), round(tolerance, 6))
    cached = handle._contours.get(key)
    if cached is not None:
        return [list(ring) for ring in cached]
    rings = _pen_rings(handle, int(glyph_id), tolerance)
    handle._contours[key] = rings
    return [list(ring) for ring in rings]


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def _wrap_line(font, text: str, size: float, max_width: float | None):
    """Greedy per-character wrap; CJK breaks anywhere, ASCII at spaces when possible."""
    glyphs = []
    line_width = 0.0
    for char in text:
        codepoint = ord(char)
        if char == "\t":
            raise TextError("FP_E_TEXT_CHAR", "tab characters are not supported in text (use spaces)", "$.text")
        if codepoint < 0x20:
            raise TextError(
                "FP_E_TEXT_CHAR", f"control character U+{codepoint:04X} is not supported", "$.text"
            )
        gid = font.glyph_id(char)
        if gid is None:
            raise TextError(
                "FP_E_TEXT_GLYPH",
                f"font {font.family or font.path!r} does not cover U+{codepoint:04X} {char!r}; "
                "no system-font fallback is allowed",
                "$.text",
            )
        advance = font.advance_units(gid) * size / float(font.upem)
        if max_width is not None and glyphs and line_width + advance > max_width + 1e-9:
            if char == " ":
                continue  # drop the space that caused the break
            break
        glyphs.append({"glyph_id": gid, "advance": float(advance), "ch": char, "size_px": float(size)})
        line_width += advance
    return glyphs, line_width


def _layout(text: str, font: fonts.FontHandle, *, size_px: float, box, align: str, line_height: float):
    if not isinstance(text, str):
        raise TextError("FP_E_TEXT_TYPE", "text must be a string", "$.text")
    if not text.strip():
        raise TextError("FP_E_TEXT_TYPE", "text must be a non-empty string", "$.text")
    size = _finite(size_px, "size_px", "$.font.size_px")
    if not 1.0 <= size <= 4096.0:
        raise TextError("FP_E_TEXT_SIZE", f"size_px {size} outside [1, 4096]", "$.font.size_px")
    spacing = _finite(line_height, "line_height", "$.font.line_gap")
    if not 0.5 <= spacing <= 8.0:
        raise TextError("FP_E_TEXT_PARAM", f"line_height {spacing} outside [0.5, 8]", "$.font.line_gap")
    if align not in ("left", "center", "right"):
        raise TextError(
            "FP_E_TEXT_ALIGN", f"align {align!r} must be one of left/center/right", "$.font.align"
        )
    box_rect = _unwrap_box(box, None)
    max_width = box_rect[2] if box_rect else None
    x0 = box_rect[0] if box_rect else 0.0
    y0 = box_rect[1] if box_rect else 0.0
    line_px = size * spacing
    baseline_offset = font.ascent_ratio * size

    visual_lines = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if paragraph == "":
            visual_lines.append(([], 0.0))
            continue
        remaining = paragraph
        while remaining:
            glyphs, width = _wrap_line(font, remaining, size, max_width)
            if not glyphs and max_width is not None:
                # A single glyph wider than the box: place it and let the
                # overflow check warn (never loop forever, never drop text).
                glyphs, width = _wrap_line(font, remaining[:1], size, None)
                visual_lines.append((glyphs, width))
                remaining = remaining[1:]
                continue
            visual_lines.append((glyphs, width))
            consumed = len(glyphs)
            remaining = remaining[consumed:]
            if remaining.startswith(" "):
                remaining = remaining[1:]

    glyphs_out = []
    max_line_width = 0.0
    for line_index, (line_glyphs, width) in enumerate(visual_lines):
        max_line_width = max(max_line_width, width)
        if box_rect is not None:
            free = box_rect[2] - width
            if align == "center":
                pen_x = x0 + free / 2.0
            elif align == "right":
                pen_x = x0 + free
            else:
                pen_x = x0
        else:
            pen_x = x0
        baseline = y0 + baseline_offset + line_index * line_px
        for glyph in line_glyphs:
            glyphs_out.append(
                {
                    "glyph_id": glyph["glyph_id"],
                    "advance": glyph["advance"],
                    "x": float(pen_x),
                    "y": float(baseline),
                    "line": line_index,
                    "ch": glyph["ch"],
                    "size_px": float(size),
                }
            )
            pen_x += glyph["advance"]

    descent_px = -font.descent_ratio * size
    bottom = (y0 + baseline_offset + (len(visual_lines) - 1) * line_px + descent_px) if visual_lines else y0
    meta = {
        "line_count": len(visual_lines),
        "line_widths": [round(width, 6) for _, width in visual_lines],
        "max_line_width": max_line_width,
        "box": list(box_rect) if box_rect else None,
        "overflow_x": bool(box_rect is not None and max_line_width > box_rect[2] + 0.5),
        "overflow_y": bool(box_rect is not None and bottom > box_rect[1] + box_rect[3] + 0.5),
        "bottom": bottom,
    }
    return glyphs_out, meta


def layout_text(
    text: str,
    font,
    *,
    size_px: float,
    box: tuple[float, float, float, float] | None = None,
    align: str = "left",
    line_height: float = DEFAULT_LINE_HEIGHT,
):
    """Lay out ``text`` and return ``[{glyph_id, advance, x, y, line}]``.

    Coordinates are canvas-world pixels, origin top-left, y down.  ``x`` is the
    glyph origin (left edge before its left side bearing), ``y`` is the
    baseline.  ``\\n`` is an explicit break; when ``box`` is given, lines are
    greedily wrapped to ``box[2]``.  Missing glyphs fail loud with
    ``FP_E_TEXT_GLYPH``.
    """
    handle = fonts.load_font(font)
    glyphs, _meta = _layout(
        text, handle, size_px=size_px, box=box, align=align, line_height=line_height
    )
    return glyphs


# ---------------------------------------------------------------------------
# Canonical fill elements
# ---------------------------------------------------------------------------


def _transform_points(points, transform: dict, pivot) -> list[tuple[float, float]]:
    if not transform:
        return points
    skew = transform.get("skew")
    if skew is not None and abs(float(skew)) > 1e-12:
        raise TextError("FP_E_TEXT_TRANSFORM", "transform.skew is not supported for text", "$.transform")
    scale = transform.get("scale", 1.0)
    if isinstance(scale, (list, tuple)):
        if len(scale) != 2:
            raise TextError("FP_E_TEXT_TRANSFORM", "transform.scale pair must have 2 entries", "$.transform")
        sx, sy = float(scale[0]), float(scale[1])
    else:
        sx = sy = float(scale)
    if not (math.isfinite(sx) and math.isfinite(sy)) or sx <= 0 or sy <= 0:
        raise TextError("FP_E_TEXT_TRANSFORM", "transform.scale must be > 0", "$.transform")
    rotate = math.radians(_finite(transform.get("rotate", 0.0), "transform.rotate", "$.transform"))
    cos_r, sin_r = math.cos(rotate), math.sin(rotate)
    px, py = pivot
    output = []
    for x, y in points:
        dx, dy = (x - px) * sx, (y - py) * sy
        output.append((px + dx * cos_r - dy * sin_r, py + dx * sin_r + dy * cos_r))
    return output


def text_fills(
    plan_element_or_text: dict,
    font,
    *,
    canvas: dict | None = None,
    warnings: list | None = None,
) -> list[dict]:
    """Bake one text element / ``texts[]`` entry into canonical fill elements.

    Returns plan-level fill dicts ``{"kind": "fill", "poly": ..., "holes": ...,
    "color": ..., "opacity": ..., "layer": ...}`` in canvas-world pixels.
    ``box`` (list or dict) controls alignment and wrapping; ``transform``
    (center/anchor/scale/rotate) is applied to the whole text block.  Overflowing
    a ``box`` adds ``FP_W_TEXT_OVERFLOW`` to ``warnings`` when provided.
    """
    if not isinstance(plan_element_or_text, dict):
        raise TextError("FP_E_TEXT_TYPE", "text element must be an object", "$")
    element = plan_element_or_text
    text = element.get("text")
    if not isinstance(text, str) or not text.strip():
        raise TextError("FP_E_TEXT_TYPE", "text must be a non-empty string", "$.text")
    handle = fonts.load_font(font)
    font_spec = element.get("font") or {}
    if not isinstance(font_spec, dict):
        raise TextError("FP_E_TEXT_FONT", "font must be an object", "$.font")
    size = _finite(font_spec.get("size_px", DEFAULT_SIZE_PX), "font.size_px", "$.font.size_px")
    align = font_spec.get("align", "left")
    line_height = font_spec.get("line_gap", font_spec.get("line_height", DEFAULT_LINE_HEIGHT))
    if line_height is None:
        line_height = DEFAULT_LINE_HEIGHT

    box = element.get("box")
    if box is None and isinstance(canvas, dict) and canvas.get("width") and canvas.get("height"):
        # Default: text block anchored at the canvas top-left with a small
        # margin, left aligned; explicit boxes are the normal case.
        margin = max(1.0, size * 0.25)
        box = [margin, margin, float(canvas["width"]) - 2 * margin, float(canvas["height"]) - 2 * margin]
    glyphs, meta = _layout(
        text, handle, size_px=size, box=box, align=align, line_height=float(line_height)
    )
    if warnings is not None:
        if meta["overflow_x"] or meta["overflow_y"]:
            warnings.append(
                {
                    "severity": "warning",
                    "code": "FP_W_TEXT_OVERFLOW",
                    "path": "$.box",
                    "message": (
                        f"text overflows its box (line width {meta['max_line_width']:.2f}px vs "
                        f"box {meta['box'][2]:.2f}px; bottom {meta['bottom']:.2f}px vs "
                        f"{meta['box'][1] + meta['box'][3]:.2f}px)"
                    ),
                    "details": {"meta": meta},
                }
            )

    scale = size / float(handle.upem)
    blocks = []
    for glyph in glyphs:
        rings_font = glyph_contours(handle, glyph["glyph_id"], size_px=size)
        if not rings_font:
            continue
        origin_x, baseline = glyph["x"], glyph["y"]
        rings_px = [
            [(origin_x + x * scale, baseline - y * scale) for x, y in ring] for ring in rings_font
        ]
        blocks.append((glyph, rings_px))

    transform = element.get("transform") or None
    if transform:
        if not isinstance(transform, dict):
            raise TextError("FP_E_TEXT_TRANSFORM", "transform must be an object", "$.transform")
        pivot = transform.get("center", transform.get("anchor"))
        if pivot is None:
            total = [point for _glyph, rings in blocks for ring in rings for point in ring]
            if not total:
                return []
            pivot = (
                sum(point[0] for point in total) / len(total),
                sum(point[1] for point in total) / len(total),
            )
        try:
            pivot = (float(pivot[0]), float(pivot[1]))
        except (TypeError, ValueError, IndexError) as exc:
            raise TextError("FP_E_TEXT_TRANSFORM", "transform.center must be [x, y]", "$.transform") from exc
        blocks = [
            (glyph, [_transform_points(ring, transform, pivot) for ring in rings])
            for glyph, rings in blocks
        ]

    opacity = element.get("opacity", 1.0)
    if isinstance(opacity, bool) or not isinstance(opacity, (int, float)):
        opacity = 1.0
    alpha_spec = font_spec.get("alpha", 1.0)
    if alpha_spec is None:
        alpha_spec = 1.0
    if isinstance(alpha_spec, bool) or not isinstance(alpha_spec, (int, float)):
        raise TextError("FP_E_TEXT_PARAM", "font.alpha must be a number", "$.font.alpha")
    alpha = min(max(float(alpha_spec), 0.0), 1.0)
    color = font_spec.get("color", element.get("color", "#000000"))
    layer = element.get("layer", 0)

    fills = []
    for _glyph, rings in blocks:
        classified = _classify_rings(rings)
        for entry in classified:
            outer = rings[entry["outer"]]
            holes = [rings[index] for index in entry["holes"]]
            fills.append(
                {
                    "kind": "fill",
                    "poly": [[float(x), float(y)] for x, y in outer],
                    "holes": [[[float(x), float(y)] for x, y in hole] for hole in holes],
                    "color": color,
                    "opacity": float(min(max(float(opacity) * alpha, 0.0), 1.0)),
                    "layer": layer,
                }
            )
    return fills
