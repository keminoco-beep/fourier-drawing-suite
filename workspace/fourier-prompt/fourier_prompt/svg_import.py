"""SVG → FDSL v2.1 importer (WP-S).

This module turns an SVG document (hand-written, OmniSVG output, vectoriser
output) into a canonical FDSL v2.1 plan that :func:`fourier_prompt.compile.compile_plan`
can compile and :mod:`fourier_prompt.render_fast` can rasterise.

Supported (v1)
--------------
* ``path``: M/L/H/V/C/S/Q/T/A/Z, absolute + relative, implicit repeats, multiple
  subpaths; curves are flattened adaptively, arcs are sampled (endpoint →
  centre parameterisation per SVG 1.1 F.6.5);
* ``polygon``, ``polyline``, ``rect``, ``circle``, ``ellipse``, ``line``;
* ``g`` / ``a`` / ``switch`` nesting, ``transform`` lists with
  ``matrix/translate/scale/rotate/skewX/skewY`` (composed and baked into the
  emitted coordinates — FDSL elements carry no transform);
* presentation attributes **and** a minimal CSS subset (``<style>`` rules with
  ``tag`` / ``.class`` / ``#id`` / ``tag.class`` / ``*`` selectors, plus the
  ``style=""`` attribute), ``currentColor``;
* paints: ``#rgb`` / ``#rgba`` / ``#rrggbb`` / ``#rrggbbaa``, CSS named colours,
  ``rgb()`` / ``rgba()``, ``none`` / ``transparent``;
* ``fill`` / ``fill-opacity`` / ``fill-rule`` / ``stroke`` / ``stroke-width`` /
  ``stroke-opacity`` / ``opacity`` / ``display`` / ``visibility``;
* ``viewBox`` + ``width``/``height`` (px, pt, pc, mm, cm, in) and
  ``preserveAspectRatio`` (meet / slice / none and x/y alignment);
* groups become FDSL ``layers`` (top-level ``g`` only; nested groups inherit).

Degradation (v1, never silent — each case emits a warning)
---------------------------------------------------------
* gradients → average stop colour (``SI_W_GRADIENT_APPROX``);
* patterns → average child colour, grey when empty (``SI_W_PATTERN_APPROX``);
* ``filter`` / ``clip-path`` / ``mask`` / ``marker-*`` → ignored
  (``SI_W_FILTER_IGNORED``, ``SI_W_CLIP_IGNORED`` ...);
* ``stroke-dasharray`` → solid stroke (``SI_W_DASH_IGNORED``);
* ``text`` / ``image`` / ``foreignObject`` / ``use`` / unknown elements →
  skipped (``SI_W_TEXT_SKIPPED`` ...);
* non-zero fill-rule is honoured by ring orientation; nested same-direction
  rings become separate fill regions (visually identical for one colour);
* ``rx``/``ry`` on ``rect`` → sharp corners (``SI_W_ROUNDED_RECT_APPROX``).

Quality controls (WP-V, v1.1)
-----------------------------
* ``fill_dilate_px`` (0.0–4.0, default 0.0 API / 0.5 CLI) expands every fill
  ring by a sub-pixel amount (holes shrink by the same amount) so independently
  rasterised neighbouring fills overlap instead of leaving anti-aliasing white
  seams.  ``0`` reproduces v1.0 geometry exactly.
* ``max_colors`` supports up to 64 (hard schema limit).  The palette is ranked
  by ink weight (largest area/length first) instead of first-appearance order,
  so a 32/64-colour budget preserves the dominant colours rather than whatever
  happened to be painted first; equal weights keep document order.
* ``stroke_to_fill`` (opt-in) converts strokes whose canvas width is
  ``<= stroke_to_fill_max_width`` (default 2.0 px) into closed fill ribbons
  (closed strokes become outer ring + hole), removing the expensive Fourier
  stroke pass for hairline-heavy artwork.  ``include_stroke=False`` /
  ``--no-stroke`` remains the blunt switch that drops strokes entirely.
* ``overflow`` (``"area"`` | ``"region"``) chooses the path-limit ranking:
  ``area`` keeps the largest individual ink areas (v1.0 behaviour), ``region``
  first ranks whole same-colour groups by total ink weight and keeps every path
  of the winning groups, which avoids fragmenting multi-triangle illustrations.

Limits (configurable, fail-loud *or* trim + warning)
----------------------------------------------------
``max_paths`` (default 600, hard 2000), ``max_total_points`` (200 000),
``max_colors`` (32 API / 64 CLI default, hard 64), ``max_points_per_path``
(4096) and ``max_warnings_per_code`` (8).  Overflow policy is
``on_overflow="trim"`` (deterministic, document order preserved) or
``"error"`` which raises :class:`SvgImportError`.

Output
------
``svg_to_fdsl(...) -> (plan, warnings, stats)``.  ``plan`` is a validated
``fdsl/2.1`` dict; ``warnings`` is a list of ``{severity, code, path, message}``
dicts; ``stats`` summarises the conversion (source hash, counts, timing).
``plan["meta"]["source"]`` records the SVG name/SHA-256/viewBox for provenance.

CLI::

    python -m fourier_prompt.svg_import --svg X.svg --out plan.json \
        [--png out.png] [--fdraw out.fdraw] [--resolution 256] [--json] \
        [--max-colors 64] [--fill-dilate 0.5] [--overflow area|region] \
        [--no-stroke | --stroke-to-fill [--stroke-fill-max-width 2.0]]

Exit codes: 0 ok, 1 import failure, 2 usage, 3 compile/render/IO failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

from .schema import (
    FDSL_SCHEMA,
    MAX_CANVAS,
    MAX_ELEMENTS_HARD,
    MAX_HOLES,
    MAX_LAYERS,
    MAX_PALETTE,
    MAX_STROKE_POINTS,
    MAX_WIDTH,
    MIN_CANVAS,
    MIN_WIDTH,
    validate_plan_detailed,
)

__all__ = [
    "SvgImportError",
    "brush_preset_for_width",
    "import_svg",
    "main",
    "svg_to_fdsl",
]

IMPORTER_VERSION = "1.1"
#: default fill dilation used by the CLI (API keeps 0.0 for geometry stability)
DEFAULT_FILL_DILATE_PX = 0.5
#: default stroke width threshold for ``stroke_to_fill`` (canvas px)
DEFAULT_STROKE_TO_FILL_WIDTH = 2.0

#: hard stop for pathological SVG documents
MAX_SOURCE_BYTES = 32 * 1024 * 1024
#: per-subpath point cap before simplification (memory guard)
_RAW_SUBPATH_POINTS = 65536
#: area (canvas px^2) below which a fill ring is treated as degenerate
_MIN_RING_AREA = 0.25
#: below this alpha an element is invisible and skipped
_MIN_ALPHA = 0.004
#: maximum nesting depth of groups / elements
_MAX_DEPTH = 64
#: maximum number of subpaths for ring nesting analysis
_MAX_NESTING_RINGS = 256


class SvgImportError(ValueError):
    """Structured SVG import error (``SI_E_*`` code + location)."""

    def __init__(self, code, message, path="<svg>", *, details=None):
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


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


class _Warnings:
    """Ordered warning collector with a per-code cap (avoids 10k-line reports)."""

    def __init__(self, limit_per_code=8):
        self.items = []
        self.counts = {}
        self.suppressed = 0
        self.limit = max(1, int(limit_per_code))

    def add(self, code, path, message):
        self.counts[code] = self.counts.get(code, 0) + 1
        if self.counts[code] > self.limit:
            self.suppressed += 1
            return
        self.items.append(
            {
                "severity": "warning",
                "code": str(code),
                "path": str(path),
                "message": str(message),
            }
        )

    def finish(self):
        if self.suppressed:
            self.items.append(
                {
                    "severity": "warning",
                    "code": "SI_W_WARNINGS_SUPPRESSED",
                    "path": "<svg>",
                    "message": (
                        f"{self.suppressed} further warning(s) suppressed "
                        f"(max {self.limit} per code)"
                    ),
                }
            )
        return self.items


# ---------------------------------------------------------------------------
# Matrix helpers
# ---------------------------------------------------------------------------

Matrix = tuple  # (a, b, c, d, e, f)

IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mat_mul(m, n):
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (
        a * A + c * B,
        b * A + d * B,
        a * C + c * D,
        b * C + d * D,
        a * E + c * F + e,
        b * E + d * F + f,
    )


def _mat_apply(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def _mat_scale(m):
    a, b, c, d, _e, _f = m
    det = a * d - b * c
    scale = math.sqrt(abs(det))
    return scale if scale > 1e-12 else 1.0


_TRANSFORM_RE = re.compile(r"([A-Za-z]+)\s*\(([^)]*)\)")
_NUMBER_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def _numbers(text):
    return [float(token) for token in _NUMBER_RE.findall(text or "")]


def parse_transform(text, *, path="<svg>"):
    """Parse an SVG ``transform`` list into a single matrix (left-to-right)."""
    matrix = IDENTITY
    consumed = 0
    for match in _TRANSFORM_RE.finditer(text or ""):
        gap = (text or "")[consumed : match.start()]
        if gap.strip(" ,\t\r\n"):
            raise SvgImportError(
                "SI_E_TRANSFORM", f"unexpected text {gap.strip()!r} in transform", path
            )
        name = match.group(1).strip().lower()
        args = _numbers(match.group(2))
        consumed = match.end()
        if name == "matrix":
            if len(args) != 6:
                raise SvgImportError("SI_E_TRANSFORM", "matrix() needs 6 numbers", path)
            step = tuple(args)
        elif name == "translate":
            if len(args) not in (1, 2):
                raise SvgImportError("SI_E_TRANSFORM", "translate() needs 1 or 2 numbers", path)
            step = (1.0, 0.0, 0.0, 1.0, args[0], args[1] if len(args) == 2 else 0.0)
        elif name == "scale":
            if len(args) not in (1, 2):
                raise SvgImportError("SI_E_TRANSFORM", "scale() needs 1 or 2 numbers", path)
            sx = args[0]
            sy = args[1] if len(args) == 2 else args[0]
            step = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif name == "rotate":
            if len(args) not in (1, 3):
                raise SvgImportError("SI_E_TRANSFORM", "rotate() needs 1 or 3 numbers", path)
            angle = math.radians(args[0])
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            rot = (cos_a, sin_a, -sin_a, cos_a, 0.0, 0.0)
            if len(args) == 3:
                cx, cy = args[1], args[2]
                step = _mat_mul(
                    _mat_mul((1.0, 0.0, 0.0, 1.0, cx, cy), rot),
                    (1.0, 0.0, 0.0, 1.0, -cx, -cy),
                )
            else:
                step = rot
        elif name == "skewx":
            if len(args) != 1:
                raise SvgImportError("SI_E_TRANSFORM", "skewX() needs 1 number", path)
            step = (1.0, 0.0, math.tan(math.radians(args[0])), 1.0, 0.0, 0.0)
        elif name == "skewy":
            if len(args) != 1:
                raise SvgImportError("SI_E_TRANSFORM", "skewY() needs 1 number", path)
            step = (1.0, math.tan(math.radians(args[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            raise SvgImportError("SI_E_TRANSFORM", f"unknown transform {name!r}", path)
        matrix = _mat_mul(matrix, step)
    if (text or "").strip() and consumed == 0:
        raise SvgImportError("SI_E_TRANSFORM", f"cannot parse transform {text!r}", path)
    if (text or "").strip()[consumed:].strip():
        raise SvgImportError(
            "SI_E_TRANSFORM", f"trailing garbage in transform {text!r}", path
        )
    return matrix


# ---------------------------------------------------------------------------
# Lengths / numbers
# ---------------------------------------------------------------------------

_UNIT_SCALE = {
    "": 1.0,
    "px": 1.0,
    "pt": 96.0 / 72.0,
    "pc": 16.0,
    "mm": 96.0 / 25.4,
    "cm": 96.0 / 2.54,
    "in": 96.0,
}
_LENGTH_RE = re.compile(
    r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([a-zA-Z%]*)\s*$"
)


def _parse_length(value, *, default=None, percent_base=None, path="<svg>", warnings=None):
    """Parse a CSS/SVG length to user units; ``%`` resolves against ``percent_base``."""
    if value is None:
        return default
    text = str(value).strip()
    match = _LENGTH_RE.match(text)
    if not match:
        if warnings is not None:
            warnings.add(
                "SI_W_INVALID_LENGTH",
                path,
                f"cannot parse length {value!r}; using {default!r}",
            )
        return default
    number = float(match.group(1))
    unit = match.group(2).lower()
    if unit == "%":
        if percent_base is None:
            if warnings is not None:
                warnings.add(
                    "SI_W_INVALID_LENGTH",
                    path,
                    f"percentage length {value!r} has no reference; using {default!r}",
                )
            return default
        return number * float(percent_base) / 100.0
    if unit not in _UNIT_SCALE:
        if warnings is not None:
            warnings.add(
                "SI_W_UNKNOWN_UNIT", path, f"unknown unit {unit!r} in {value!r}; treated as px"
            )
        return number
    return number * _UNIT_SCALE[unit]


# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3,8})$")
_FUNC_RE = re.compile(r"^(rgba?|hsla?)\s*\((.*)\)$", re.IGNORECASE | re.DOTALL)
_URL_RE = re.compile(r"""^url\(\s*['"]?#([^)'"\s]+)['"]?\s*\)""", re.IGNORECASE)

_NAMED_COLORS = {
    "aliceblue": "#f0f8ff", "antiquewhite": "#faebd7", "aqua": "#00ffff",
    "aquamarine": "#7fffd4", "azure": "#f0ffff", "beige": "#f5f5dc",
    "bisque": "#ffe4c4", "black": "#000000", "blanchedalmond": "#ffebcd",
    "blue": "#0000ff", "blueviolet": "#8a2be2", "brown": "#a52a2a",
    "burlywood": "#deb887", "cadetblue": "#5f9ea0", "chartreuse": "#7fff00",
    "chocolate": "#d2691e", "coral": "#ff7f50", "cornflowerblue": "#6495ed",
    "cornsilk": "#fff8dc", "crimson": "#dc143c", "cyan": "#00ffff",
    "darkblue": "#00008b", "darkcyan": "#008b8b", "darkgoldenrod": "#b8860b",
    "darkgray": "#a9a9a9", "darkgreen": "#006400", "darkgrey": "#a9a9a9",
    "darkkhaki": "#bdb76b", "darkmagenta": "#8b008b", "darkolivegreen": "#556b2f",
    "darkorange": "#ff8c00", "darkorchid": "#9932cc", "darkred": "#8b0000",
    "darksalmon": "#e9967a", "darkseagreen": "#8fbc8f", "darkslateblue": "#483d8b",
    "darkslategray": "#2f4f4f", "darkslategrey": "#2f4f4f", "darkturquoise": "#00ced1",
    "darkviolet": "#9400d3", "deeppink": "#ff1493", "deepskyblue": "#00bfff",
    "dimgray": "#696969", "dimgrey": "#696969", "dodgerblue": "#1e90ff",
    "firebrick": "#b22222", "floralwhite": "#fffaf0", "forestgreen": "#228b22",
    "fuchsia": "#ff00ff", "gainsboro": "#dcdcdc", "ghostwhite": "#f8f8ff",
    "gold": "#ffd700", "goldenrod": "#daa520", "gray": "#808080",
    "green": "#008000", "greenyellow": "#adff2f", "grey": "#808080",
    "honeydew": "#f0fff0", "hotpink": "#ff69b4", "indianred": "#cd5c5c",
    "indigo": "#4b0082", "ivory": "#fffff0", "khaki": "#f0e68c",
    "lavender": "#e6e6fa", "lavenderblush": "#fff0f5", "lawngreen": "#7cfc00",
    "lemonchiffon": "#fffacd", "lightblue": "#add8e6", "lightcoral": "#f08080",
    "lightcyan": "#e0ffff", "lightgoldenrodyellow": "#fafad2", "lightgray": "#d3d3d3",
    "lightgreen": "#90ee90", "lightgrey": "#d3d3d3", "lightpink": "#ffb6c1",
    "lightsalmon": "#ffa07a", "lightseagreen": "#20b2aa", "lightskyblue": "#87cefa",
    "lightslategray": "#778899", "lightslategrey": "#778899", "lightsteelblue": "#b0c4de",
    "lightyellow": "#ffffe0", "lime": "#00ff00", "limegreen": "#32cd32",
    "linen": "#faf0e6", "magenta": "#ff00ff", "maroon": "#800000",
    "mediumaquamarine": "#66cdaa", "mediumblue": "#0000cd", "mediumorchid": "#ba55d3",
    "mediumpurple": "#9370db", "mediumseagreen": "#3cb371", "mediumslateblue": "#7b68ee",
    "mediumspringgreen": "#00fa9a", "mediumturquoise": "#48d1cc",
    "mediumvioletred": "#c71585", "midnightblue": "#191970", "mintcream": "#f5fffa",
    "mistyrose": "#ffe4e1", "moccasin": "#ffe4b5", "navajowhite": "#ffdead",
    "navy": "#000080", "oldlace": "#fdf5e6", "olive": "#808000",
    "olivedrab": "#6b8e23", "orange": "#ffa500", "orangered": "#ff4500",
    "orchid": "#da70d6", "palegoldenrod": "#eee8aa", "palegreen": "#98fb98",
    "paleturquoise": "#afeeee", "palevioletred": "#db7093", "papayawhip": "#ffefd5",
    "peachpuff": "#ffdab9", "peru": "#cd853f", "pink": "#ffc0cb",
    "plum": "#dda0dd", "powderblue": "#b0e0e6", "purple": "#800080",
    "rebeccapurple": "#663399", "red": "#ff0000", "rosybrown": "#bc8f8f",
    "royalblue": "#4169e1", "saddlebrown": "#8b4513", "salmon": "#fa8072",
    "sandybrown": "#f4a460", "seagreen": "#2e8b57", "seashell": "#fff5ee",
    "sienna": "#a0522d", "silver": "#c0c0c0", "skyblue": "#87ceeb",
    "slateblue": "#6a5acd", "slategray": "#708090", "slategrey": "#708090",
    "snow": "#fffafa", "springgreen": "#00ff7f", "steelblue": "#4682b4",
    "tan": "#d2b48c", "teal": "#008080", "thistle": "#d8bfd8",
    "tomato": "#ff6347", "turquoise": "#40e0d0", "violet": "#ee82ee",
    "wheat": "#f5deb3", "white": "#ffffff", "whitesmoke": "#f5f5f5",
    "yellow": "#ffff00", "yellowgreen": "#9acd32",
}


def _hex_to_rgba(body):
    body = body.strip()
    if len(body) == 3:
        rgb = [int(ch * 2, 16) for ch in body]
        return (rgb[0], rgb[1], rgb[2], 1.0)
    if len(body) == 4:
        rgb = [int(ch * 2, 16) for ch in body[:3]]
        return (rgb[0], rgb[1], rgb[2], int(body[3] * 2, 16) / 255.0)
    if len(body) == 6:
        return (
            int(body[0:2], 16),
            int(body[2:4], 16),
            int(body[4:6], 16),
            1.0,
        )
    if len(body) == 8:
        return (
            int(body[0:2], 16),
            int(body[2:4], 16),
            int(body[4:6], 16),
            int(body[6:8], 16) / 255.0,
        )
    raise ValueError(f"invalid hex colour #{body}")


def _parse_color(text):
    """Return ``(r, g, b, a)`` or ``None`` when the colour is not understood."""
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered in ("none", "transparent"):
        return None
    if lowered in _NAMED_COLORS:
        return _hex_to_rgba(_NAMED_COLORS[lowered][1:])
    match = _HEX_RE.match(raw)
    if match:
        try:
            return _hex_to_rgba(match.group(1))
        except ValueError:
            return None
    match = _FUNC_RE.match(raw)
    if match:
        name = match.group(1).lower()
        body = match.group(2).strip()
        if name.startswith("rgb"):
            alpha = 1.0
            if "/" in body:
                body, alpha_text = body.split("/", 1)
                alpha = _parse_alpha(alpha_text)
            parts = [part for part in re.split(r"[,\s]+", body) if part]
            if len(parts) == 4:
                alpha = _parse_alpha(parts[3])
                parts = parts[:3]
            if len(parts) != 3:
                return None
            channels = []
            for part in parts:
                if part.endswith("%"):
                    channels.append(int(round(float(part[:-1]) * 255.0 / 100.0)))
                else:
                    channels.append(_clamp_byte(float(part)))
            return (channels[0], channels[1], channels[2], alpha)
        # hsl / hsla are not needed for SVG vector import (rare); fall through.
        return None
    return None


def _parse_alpha(text):
    raw = str(text).strip()
    if raw.endswith("%"):
        return _clamp01(float(raw[:-1]) / 100.0)
    return _clamp01(float(raw))


def _clamp_byte(value):
    return int(max(0, min(255, round(value))))


def _clamp01(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(number):
        return 1.0
    return max(0.0, min(1.0, number))


def _rgb_hex(rgb):
    return "#%02x%02x%02x" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------


class _Palette:
    """Deterministic colour table: first appearance wins, overflow quantises."""

    def __init__(self, limit, warnings):
        self.limit = max(1, min(MAX_PALETTE, int(limit)))
        self.warnings = warnings
        self.entries = []  # list of (r, g, b)
        self.quantized = 0
        self._warned = False

    def add(self, rgb, path="<svg>"):
        rgb = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        for index, entry in enumerate(self.entries):
            if entry == rgb:
                return f"c{index}"
        if len(self.entries) < self.limit:
            self.entries.append(rgb)
            return f"c{len(self.entries) - 1}"
        # nearest neighbour in RGB space; ties resolve to the earliest entry
        best_index = 0
        best_distance = None
        for index, entry in enumerate(self.entries):
            distance = (
                (entry[0] - rgb[0]) ** 2
                + (entry[1] - rgb[1]) ** 2
                + (entry[2] - rgb[2]) ** 2
            )
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_index = index
        self.quantized += 1
        if not self._warned:
            self._warned = True
            self.warnings.add(
                "SI_W_COLOR_QUANTIZED",
                path,
                f"more than {self.limit} colours; nearest-colour quantisation applied",
            )
        return f"c{best_index}"

    def as_dict(self):
        return {f"c{index}": _rgb_hex(rgb) for index, rgb in enumerate(self.entries)}

    def seed(self, colors):
        """Pre-fill the palette with ``colors`` (already ranked, first wins)."""
        self.entries = []
        for rgb in colors:
            entry = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
            if entry not in self.entries and len(self.entries) < self.limit:
                self.entries.append(entry)


def _rank_palette(candidates, limit):
    """Rank RGB colours by ink weight (area/length), ties by document order."""
    weights = {}
    first_seen = {}
    for index, candidate in enumerate(candidates):
        rgb = candidate["element"].get("_rgb")
        if rgb is None:
            continue
        rgb = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        weight = float(candidate.get("weight") or 0.0)
        weights[rgb] = weights.get(rgb, 0.0) + max(weight, 1e-9)
        first_seen.setdefault(rgb, index)
    ranked = sorted(weights, key=lambda rgb: (-weights[rgb], first_seen[rgb]))
    return ranked[: max(1, min(MAX_PALETTE, int(limit)))]


def _nearest_color_index(rgb, colors):
    """Index of the nearest entry in a small RGB palette (ties: earliest)."""
    best = 0
    best_distance = None
    for index, entry in enumerate(colors):
        distance = (
            (entry[0] - rgb[0]) ** 2
            + (entry[1] - rgb[1]) ** 2
            + (entry[2] - rgb[2]) ** 2
        )
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best = index
    return best




# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _round2(value):
    rounded = round(float(value), 2)
    return 0.0 if abs(rounded) < 1e-9 else rounded


def _points_out(points):
    return [[_round2(x), _round2(y)] for x, y in points]


def _ring_area(points):
    """Signed shoelace area (canvas coordinates, y down)."""
    count = len(points)
    if count < 3:
        return 0.0
    total = 0.0
    for index in range(count):
        x0, y0 = points[index]
        x1, y1 = points[(index + 1) % count]
        total += x0 * y1 - x1 * y0
    return total / 2.0


def _point_in_ring(point, ring):
    """Ray-casting point-in-polygon (even-odd test)."""
    x, y = point
    inside = False
    count = len(ring)
    j = count - 1
    for index in range(count):
        xi, yi = ring[index]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / (yj - yi + 0.0) + xi
        ):
            inside = not inside
        j = index
    return inside


def _offset_polygon(points, amount):
    """Offset a closed ring by ``amount`` px (positive = outward).

    Uses per-vertex miter normals with a 4x miter clamp.  The sign of
    ``_ring_area`` decides which side is outward, so both windings work.
    """
    count = len(points)
    if abs(amount) < 1e-9 or count < 3:
        return [(float(x), float(y)) for x, y in points]
    area = _ring_area(points)
    if abs(area) < 1e-12:
        return [(float(x), float(y)) for x, y in points]
    orientation = 1.0 if area > 0 else -1.0
    normals = []
    for index in range(count):
        x0, y0 = points[index]
        x1, y1 = points[(index + 1) % count]
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length < 1e-9:
            normals.append((0.0, 0.0))
        else:
            normals.append((orientation * dy / length, -orientation * dx / length))
    result = []
    limit = 4.0 * abs(amount)
    for index in range(count):
        nx0, ny0 = normals[index - 1]
        nx1, ny1 = normals[index]
        dot = nx0 * nx1 + ny0 * ny1
        denom = 1.0 + dot
        if denom < 1e-6:
            mx, my = nx1, ny1
            factor = 1.0
        else:
            mx, my = nx0 + nx1, ny0 + ny1
            factor = 1.0 / denom
        vx, vy = amount * mx * factor, amount * my * factor
        length = math.hypot(vx, vy)
        if length > limit > 0:
            vx *= limit / length
            vy *= limit / length
        result.append((points[index][0] + vx, points[index][1] + vy))
    return result


def _open_ribbon(points, half_width):
    """Return a butt-capped polygon outline for an open polyline stroke."""
    count = len(points)
    if count < 2:
        return []
    edge_normals = []
    for index in range(count - 1):
        x0, y0 = points[index]
        x1, y1 = points[index + 1]
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length < 1e-9:
            edge_normals.append((0.0, 0.0))
        else:
            edge_normals.append((dy / length, -dx / length))
    left = []
    right = []
    limit = 4.0 * abs(half_width)
    for index in range(count):
        n_prev = edge_normals[index - 1] if index > 0 else edge_normals[0]
        n_next = edge_normals[index] if index < count - 1 else edge_normals[-1]
        dot = n_prev[0] * n_next[0] + n_prev[1] * n_next[1]
        denom = 1.0 + dot
        if denom < 1e-6:
            mx, my = n_next
            factor = 1.0
        else:
            mx, my = n_prev[0] + n_next[0], n_prev[1] + n_next[1]
            factor = 1.0 / denom
        vx, vy = half_width * mx * factor, half_width * my * factor
        length = math.hypot(vx, vy)
        if length > limit > 0:
            vx *= limit / length
            vy *= limit / length
        left.append((points[index][0] + vx, points[index][1] + vy))
        right.append((points[index][0] - vx, points[index][1] - vy))
    return left + right[::-1]


def _closed_stroke_rings(points, half_width):
    """Return ``(outer, inner)`` rings for a closed centreline stroke, or None."""
    outer = _offset_polygon(points, half_width)
    inner = _offset_polygon(points, -half_width)
    if len(outer) < 3 or len(inner) < 3:
        return None
    outer_area = abs(_ring_area(outer))
    inner_area = abs(_ring_area(inner))
    if inner_area < 1.0 or outer_area <= inner_area + 1.0:
        return None
    return outer, inner


def _keyhole_polygon(outer, inner):
    """Splice an outer ring and an inner ring into one simple keyhole polygon.

    FDSL fill ``holes`` are painted with the canvas background (cutout
    semantics), which would erase layers below a converted stroke.  A keyhole
    polygon keeps the ring hollow without touching the background.
    """
    count = min(len(outer), len(inner))
    if count < 3:
        return None
    best = 0
    best_distance = None
    for index in range(count):
        distance = (outer[index][0] - inner[index][0]) ** 2 + (
            outer[index][1] - inner[index][1]
        ) ** 2
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best = index
    index = best
    polygon = (
        list(outer[: index + 1])
        + list(inner[index::-1])
        + list(inner[:index:-1])
        + list(outer[index + 1 :])
    )
    return polygon




def _decimate(points, limit, *, closed=False):
    """Uniformly reduce ``points`` to ``limit`` (keeps endpoints for open paths)."""
    count = len(points)
    if count <= limit:
        return list(points)
    step = int(math.ceil(count / float(limit)))
    reduced = list(points[::step])
    if not closed and reduced and reduced[-1] != points[-1]:
        if len(reduced) < limit:
            reduced.append(points[-1])
        else:
            reduced[-1] = points[-1]
    return reduced


def _flatten_cubic(p0, p1, p2, p3, tolerance, out, depth=0):
    """Append flattened points of a cubic (excluding p0) to ``out``."""
    if depth >= 12 or _cubic_flat(p0, p1, p2, p3, tolerance):
        out.append(p3)
        return
    p01 = _mid(p0, p1)
    p12 = _mid(p1, p2)
    p23 = _mid(p2, p3)
    p012 = _mid(p01, p12)
    p123 = _mid(p12, p23)
    p0123 = _mid(p012, p123)
    _flatten_cubic(p0, p01, p012, p0123, tolerance, out, depth + 1)
    _flatten_cubic(p0123, p123, p23, p3, tolerance, out, depth + 1)


def _mid(a, b):
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def _cubic_flat(p0, p1, p2, p3, tolerance):
    limit = max(tolerance, 1e-9)
    return (
        _point_line_distance(p1, p0, p3) <= limit
        and _point_line_distance(p2, p0, p3) <= limit
    )


def _point_line_distance(point, start, end):
    px, py = point
    x0, y0 = start
    x1, y1 = end
    dx = x1 - x0
    dy = y1 - y0
    length = math.hypot(dx, dy)
    if length < 1e-12:
        return math.hypot(px - x0, py - y0)
    return abs(dy * px - dx * py + x1 * y0 - y1 * x0) / length


def _flatten_quadratic(p0, p1, p2, tolerance, out, depth=0):
    """Append flattened points of a quadratic (excluding p0) to ``out``."""
    if depth >= 12 or _point_line_distance(p1, p0, p2) <= tolerance:
        out.append(p2)
        return
    p01 = _mid(p0, p1)
    p12 = _mid(p1, p2)
    p012 = _mid(p01, p12)
    _flatten_quadratic(p0, p01, p012, tolerance, out, depth + 1)
    _flatten_quadratic(p012, p12, p2, tolerance, out, depth + 1)


def _arc_points(p0, rx, ry, rotation, large_arc, sweep, p1, tolerance):
    """Sample an SVG elliptical arc (endpoint parameterisation) into points."""
    x0, y0 = p0
    x1, y1 = p1
    if abs(x0 - x1) < 1e-12 and abs(y0 - y1) < 1e-12:
        return []
    rx = abs(float(rx))
    ry = abs(float(ry))
    if rx < 1e-12 or ry < 1e-12:
        return [p1]
    phi = math.radians(float(rotation) % 360.0)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx = (x0 - x1) / 2.0
    dy = (y0 - y1) / 2.0
    x1p = cos_phi * dx + sin_phi * dy
    y1p = -sin_phi * dx + cos_phi * dy
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1.0:
        scale = math.sqrt(lam)
        rx *= scale
        ry *= scale
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / den)) if den > 1e-15 else 0.0
    if bool(large_arc) == bool(sweep):
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cos_phi * cxp - sin_phi * cyp + (x0 + x1) / 2.0
    cy = sin_phi * cxp + cos_phi * cyp + (y0 + y1) / 2.0
    ux = (x1p - cxp) / rx
    uy = (y1p - cyp) / ry
    vx = (-x1p - cxp) / rx
    vy = (-y1p - cyp) / ry
    theta1 = math.atan2(uy, ux)
    delta = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
    if not sweep and delta > 0:
        delta -= 2.0 * math.pi
    elif sweep and delta < 0:
        delta += 2.0 * math.pi
    radius = max(rx, ry)
    if radius <= tolerance:
        steps = 8
    else:
        step_angle = 2.0 * math.acos(max(-1.0, min(1.0, 1.0 - tolerance / radius)))
        step_angle = max(step_angle, math.radians(1.0))
        steps = int(math.ceil(abs(delta) / step_angle))
    steps = max(2, min(2048, steps))
    points = []
    for index in range(1, steps + 1):
        angle = theta1 + delta * index / steps
        x = cx + cos_phi * rx * math.cos(angle) - sin_phi * ry * math.sin(angle)
        y = cy + sin_phi * rx * math.cos(angle) + cos_phi * ry * math.sin(angle)
        points.append((x, y))
    if points:
        points[-1] = p1  # exact endpoint
    return points


def _ellipse_points(cx, cy, rx, ry, tolerance):
    radius = max(abs(rx), abs(ry))
    if radius < 1e-9:
        return []
    if radius <= tolerance:
        steps = 12
    else:
        step_angle = 2.0 * math.acos(max(-1.0, min(1.0, 1.0 - tolerance / radius)))
        step_angle = max(step_angle, math.radians(1.0))
        steps = int(math.ceil(2.0 * math.pi / step_angle))
    steps = max(12, min(2048, steps))
    return [
        (
            cx + rx * math.cos(2.0 * math.pi * index / steps),
            cy + ry * math.sin(2.0 * math.pi * index / steps),
        )
        for index in range(steps)
    ]


# ---------------------------------------------------------------------------
# Path data parser
# ---------------------------------------------------------------------------

_COMMANDS = set("MmLlHhVvCcSsQqTtAaZz")
_ARITY = {
    "M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2,
    "A": 7, "Z": 0,
}
_PATH_TOKEN_RE = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def _tokenize_path(data, path="<svg>"):
    tokens = []
    position = 0
    for match in _PATH_TOKEN_RE.finditer(data):
        gap = data[position : match.start()]
        if gap.strip(" \t\r\n,"):
            raise SvgImportError(
                "SI_E_PATH_DATA", f"unexpected characters {gap.strip()!r}", path
            )
        token = match.group(0)
        tokens.append(token if token in _COMMANDS else float(token))
        position = match.end()
    if data[position:].strip(" \t\r\n,"):
        raise SvgImportError(
            "SI_E_PATH_DATA", f"unexpected characters {data[position:]!r}", path
        )
    return tokens


def parse_path_data(data, *, tolerance=0.35, path="<svg>", warnings=None):
    """Parse an SVG ``d`` attribute into ``[{"points": [...], "closed": bool}]``."""
    if not isinstance(data, str) or not data.strip():
        return []
    tokens = _tokenize_path(data, path)
    if not tokens or tokens[0] not in ("M", "m"):
        raise SvgImportError(
            "SI_E_PATH_DATA", "path data must start with a moveto (M/m)", path
        )
    subpaths = []
    current = (0.0, 0.0)
    start = (0.0, 0.0)
    active = None
    previous_up = None
    previous_ctrl = None
    command = None
    index = 0

    def ensure_active():
        nonlocal active
        if active is None:
            active = {"points": [current], "closed": False}

    def finish_active(closed=False):
        nonlocal active
        if active is not None:
            points = active["points"]
            if len(points) >= 2:
                if closed:
                    if len(points) > 1 and _near(points[-1], points[0]):
                        points = points[:-1]
                    active["closed"] = True
                if len(points) > _RAW_SUBPATH_POINTS:
                    points = _decimate(points, _RAW_SUBPATH_POINTS)
                    if warnings is not None:
                        warnings.add(
                            "SI_W_POINTS_DECIMATED",
                            path,
                            f"subpath reduced to {_RAW_SUBPATH_POINTS} points "
                            "(raw flattening limit)",
                        )
                active["points"] = points
                subpaths.append(active)
            active = None

    def extend(points):
        """Append curve samples, guarding memory on pathological paths."""
        active["points"].extend(points)
        if len(active["points"]) > 4 * _RAW_SUBPATH_POINTS:
            active["points"] = _decimate(active["points"], _RAW_SUBPATH_POINTS)

    while index < len(tokens):
        token = tokens[index]
        if isinstance(token, str):
            command = token
            index += 1
            if command.upper() == "Z":
                if active is not None:
                    finish_active(closed=True)
                current = start
                previous_ctrl = None
                previous_up = "Z"
                continue
        elif command is None or command.upper() == "Z":
            raise SvgImportError(
                "SI_E_PATH_DATA", "path data must start with a command", path
            )

        upper = command.upper()
        relative = command.islower()
        arity = _ARITY[upper]
        if index + arity > len(tokens) or any(
            isinstance(value, str) for value in tokens[index : index + arity]
        ):
            raise SvgImportError(
                "SI_E_PATH_DATA",
                f"command {command!r} needs {arity} number(s) near token {index}",
                path,
            )
        args = tokens[index : index + arity]
        index += arity

        if upper == "M":
            x, y = args
            if relative:
                x += current[0]
                y += current[1]
            finish_active()
            current = (x, y)
            start = current
            previous_ctrl = None
            previous_up = "M"
            # subsequent coordinate pairs are implicit lineto
            command = "l" if relative else "L"
            continue

        ensure_active()
        if upper == "L":
            x, y = args
            if relative:
                x += current[0]
                y += current[1]
            current = (x, y)
            active["points"].append(current)
        elif upper == "H":
            (x,) = args
            if relative:
                x += current[0]
            current = (x, current[1])
            active["points"].append(current)
        elif upper == "V":
            (y,) = args
            if relative:
                y += current[1]
            current = (current[0], y)
            active["points"].append(current)
        elif upper in ("C", "S"):
            if upper == "C":
                c1 = (args[0] + current[0], args[1] + current[1]) if relative else (args[0], args[1])
                c2 = (args[2] + current[0], args[3] + current[1]) if relative else (args[2], args[3])
                end = (args[4] + current[0], args[5] + current[1]) if relative else (args[4], args[5])
            else:
                if previous_up in ("C", "S") and previous_ctrl is not None:
                    c1 = (2.0 * current[0] - previous_ctrl[0], 2.0 * current[1] - previous_ctrl[1])
                else:
                    c1 = current
                c2 = (args[0] + current[0], args[1] + current[1]) if relative else (args[0], args[1])
                end = (args[2] + current[0], args[3] + current[1]) if relative else (args[2], args[3])
            points = []
            _flatten_cubic(current, c1, c2, end, tolerance, points)
            extend(points)
            previous_ctrl = c2
            current = end
        elif upper in ("Q", "T"):
            if upper == "Q":
                control = (
                    (args[0] + current[0], args[1] + current[1]) if relative else (args[0], args[1])
                )
                end = (
                    (args[2] + current[0], args[3] + current[1]) if relative else (args[2], args[3])
                )
            else:
                if previous_up in ("Q", "T") and previous_ctrl is not None:
                    control = (
                        2.0 * current[0] - previous_ctrl[0],
                        2.0 * current[1] - previous_ctrl[1],
                    )
                else:
                    control = current
                end = (
                    (args[0] + current[0], args[1] + current[1]) if relative else (args[0], args[1])
                )
            points = []
            _flatten_quadratic(current, control, end, tolerance, points)
            extend(points)
            previous_ctrl = control
            current = end
        elif upper == "A":
            rx, ry, rotation, large_arc, sweep, x, y = args
            if relative:
                x += current[0]
                y += current[1]
            end = (x, y)
            points = _arc_points(
                current, rx, ry, rotation, bool(large_arc), bool(sweep), end, tolerance
            )
            extend(points)
            current = end
            previous_ctrl = None
        else:  # pragma: no cover - _ARITY covers every command
            raise SvgImportError("SI_E_PATH_DATA", f"unsupported command {command!r}", path)

        previous_up = upper

    finish_active(closed=False)
    return subpaths


def _near(a, b, epsilon=0.01):
    return abs(a[0] - b[0]) <= epsilon and abs(a[1] - b[1]) <= epsilon


# ---------------------------------------------------------------------------
# Definitions (gradients / patterns / ignored effects)
# ---------------------------------------------------------------------------


def _local_name(tag):
    """Lower-cased local tag name (``linearGradient`` → ``lineargradient``)."""
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    return tag.lower()


def _attributes(node):
    attrs = {}
    for key, value in node.attrib.items():
        if "}" in key:
            local = key.rsplit("}", 1)[1]
        elif ":" in key:
            local = key.split(":", 1)[1]
        else:
            local = key
        attrs[local] = value
    return attrs


def _parse_declarations(text):
    declarations = {}
    if not text:
        return declarations
    for chunk in str(text).split(";"):
        if ":" not in chunk:
            continue
        prop, value = chunk.split(":", 1)
        prop = prop.strip().lower()
        value = value.strip()
        if not prop or not value:
            continue
        if value.lower().endswith("!important"):
            value = value[: -len("!important")].strip()
        declarations[prop] = value
    return declarations


_STYLE_PROPS = (
    "fill", "stroke", "stroke-width", "fill-opacity", "stroke-opacity",
    "fill-rule", "opacity", "color", "display", "visibility", "filter",
    "clip-path", "mask", "stroke-dasharray", "marker-start", "marker-mid",
    "marker-end",
)
_INHERITED_PROPS = (
    "fill", "stroke", "stroke-width", "fill-opacity", "stroke-opacity",
    "fill-rule", "color", "visibility",
)
_STYLE_DEFAULTS = {
    "fill": "#000000",
    "stroke": "none",
    "stroke-width": "1",
    "fill-opacity": "1",
    "stroke-opacity": "1",
    "fill-rule": "nonzero",
    "color": "#000000",
    "visibility": "visible",
}


class _Defs:
    """Definitions collected from ``defs`` / anywhere in the document."""

    def __init__(self):
        self.gradients = {}
        self.patterns = {}
        self.filters = set()
        self.clips = set()
        self.masks = set()
        self.markers = set()


def _collect_stops(gradient):
    stops = []
    for child in gradient:
        if _local_name(child.tag) != "stop":
            continue
        attrs = _attributes(child)
        raw_offset = attrs.get("offset", "0")
        offset = _parse_length(raw_offset, default=0.0, percent_base=1.0)
        declarations = _parse_declarations(attrs.get("style", ""))
        color_text = attrs.get("stop-color", declarations.get("stop-color", "#000000"))
        color = _parse_color(color_text)
        if color is None:
            color = (0, 0, 0, 1.0)
        opacity_text = attrs.get("stop-opacity", declarations.get("stop-opacity", "1"))
        opacity = _clamp01(opacity_text)
        stops.append(
            (
                max(0.0, min(1.0, float(offset))),
                (color[0], color[1], color[2], color[3] * opacity),
            )
        )
    stops.sort(key=lambda item: item[0])
    return stops


def _average_stops(stops):
    """Weighted average colour of a stop list (for gradient degradation)."""
    if not stops:
        return (128, 128, 128, 1.0)
    if len(stops) == 1:
        return stops[0][1]
    total = 0.0
    r = g = b = a = 0.0
    for index in range(len(stops) - 1):
        offset0, color0 = stops[index]
        offset1, color1 = stops[index + 1]
        weight = offset1 - offset0
        if weight <= 0:
            continue
        total += weight
        r += weight * (color0[0] + color1[0]) / 2.0
        g += weight * (color0[1] + color1[1]) / 2.0
        b += weight * (color0[2] + color1[2]) / 2.0
        a += weight * (color0[3] + color1[3]) / 2.0
    if total <= 1e-12:
        return stops[0][1]
    return (int(round(r / total)), int(round(g / total)), int(round(b / total)), a / total)


def _pattern_average_color(pattern, depth=0):
    """Average solid fill/stroke colour found inside a pattern's content."""
    if depth > 4:
        return None
    colors = []
    for child in pattern:
        tag = _local_name(child.tag)
        if tag in ("rect", "circle", "ellipse", "polygon", "polyline", "path", "line", "text"):
            attrs = _attributes(child)
            declarations = _parse_declarations(attrs.get("style", ""))
            for prop in ("fill", "stroke"):
                raw = attrs.get(prop, declarations.get(prop))
                if not raw:
                    continue
                color = _parse_color(raw)
                if color is not None:
                    colors.append(color)
        if len(child):
            nested = _pattern_average_color(child, depth + 1)
            if nested is not None:
                colors.append(nested)
    if not colors:
        return None
    total = len(colors)
    return (
        int(round(sum(color[0] for color in colors) / total)),
        int(round(sum(color[1] for color in colors) / total)),
        int(round(sum(color[2] for color in colors) / total)),
        max(0.0, min(1.0, sum(color[3] for color in colors) / total)),
    )


def collect_defs(root):
    defs = _Defs()
    gradients = {}
    for node in root.iter():
        tag = _local_name(node.tag)
        attrs = _attributes(node)
        node_id = attrs.get("id")
        if tag in ("lineargradient", "radialgradient") and node_id:
            href = (attrs.get("href") or "").strip().lstrip("#") or None
            gradients[node_id] = (tag, node, _collect_stops(node), href)
        elif tag == "pattern" and node_id:
            defs.patterns[node_id] = _pattern_average_color(node)
        elif tag == "filter" and node_id:
            defs.filters.add(node_id)
        elif tag == "clippath" and node_id:
            defs.clips.add(node_id)
        elif tag == "mask" and node_id:
            defs.masks.add(node_id)
        elif tag == "marker" and node_id:
            defs.markers.add(node_id)
    # Resolve gradient href inheritance after all gradients are known.
    for node_id, (kind, _node, stops, href) in list(gradients.items()):
        if not stops and href:
            referenced = gradients.get(href)
            if referenced is not None:
                stops = referenced[2]
        defs.gradients[node_id] = {
            "type": kind,
            "stops": stops,
            "color": _average_stops(stops),
        }
    return defs


# ---------------------------------------------------------------------------
# Minimal CSS
# ---------------------------------------------------------------------------

_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_PSEUDO_RE = re.compile(r":{1,2}[A-Za-z-]+(?:\([^)]*\))?")


class _Selector:
    __slots__ = ("tag", "element_id", "classes")

    def __init__(self, tag, element_id, classes):
        self.tag = tag
        self.element_id = element_id
        self.classes = classes

    def specificity(self):
        return (
            1 if self.element_id else 0,
            len(self.classes),
            1 if self.tag and self.tag != "*" else 0,
        )


def _parse_selector(selector):
    text = _PSEUDO_RE.sub("", selector.strip())
    if not text or any(char in text for char in "[]>+~ ") :
        return None
    tag = None
    element_id = None
    classes = []
    for part in re.findall(r"[#.]?[A-Za-z0-9_-]+|\*", text):
        if part == "*":
            tag = "*"
        elif part.startswith("#"):
            element_id = part[1:]
        elif part.startswith("."):
            classes.append(part[1:])
        elif tag is None:
            tag = part.lower()
        else:
            return None
    return _Selector(tag, element_id, classes)


def _selector_matches(selector, tag, attrs):
    if selector.tag and selector.tag != "*" and selector.tag != tag:
        return False
    if selector.element_id and attrs.get("id") != selector.element_id:
        return False
    if selector.classes:
        classes = set((attrs.get("class") or "").split())
        if not set(selector.classes).issubset(classes):
            return False
    return True


def collect_css(root):
    rules = []
    order = 0
    for node in root.iter():
        if _local_name(node.tag) != "style":
            continue
        text = _CSS_COMMENT_RE.sub("", node.text or "")
        for rule in text.split("}"):
            if "{" not in rule:
                continue
            selector_text, declaration_text = rule.split("{", 1)
            declarations = _parse_declarations(declaration_text)
            if not declarations:
                continue
            for selector_text_part in selector_text.split(","):
                selector = _parse_selector(selector_text_part)
                if selector is None:
                    continue
                rules.append((selector.specificity(), order, selector, declarations))
                order += 1
    rules.sort(key=lambda item: (item[0], item[1]))
    return rules


# ---------------------------------------------------------------------------
# Brush mapping
# ---------------------------------------------------------------------------


def brush_preset_for_width(width):
    """Map a stroke width (canvas px) to a catalog brush preset.

    Thresholds follow the measured catalog (WP-C): hairlines use the fineliner,
    comic linework the G-pen, thicker linework the rough ink brush and broad
    strokes the hard round.
    """
    try:
        value = float(width)
    except (TypeError, ValueError):
        value = 1.0
    if value <= 0.9:
        return "ink_fineliner"
    if value <= 2.2:
        return "ink_gpen"
    if value <= 5.5:
        return "ink_brush_rough"
    return "basic_round_hard"


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------

_SKIP_TAGS = {
    "defs", "symbol", "clippath", "mask", "marker", "pattern", "filter",
    "lineargradient", "radialgradient", "metadata", "title", "desc", "style",
}
_SHAPE_TAGS = {
    "path", "polygon", "polyline", "rect", "circle", "ellipse", "line",
}
_GROUP_TAGS = {"g", "a", "switch"}


class _Shape:
    __slots__ = (
        "tag", "path", "subpaths", "fill", "stroke", "fill_rule", "opacity",
        "fill_alpha", "stroke_alpha", "stroke_width", "layer",
    )

    def __init__(self, **kwargs):
        for key in self.__slots__:
            setattr(self, key, kwargs.get(key))


class _Context:
    __slots__ = ("matrix", "style", "opacity", "layer", "depth", "path")

    def __init__(self, matrix, style, opacity, layer, depth, path):
        self.matrix = matrix
        self.style = style
        self.opacity = opacity
        self.layer = layer
        self.depth = depth
        self.path = path


class _Importer:
    def __init__(
        self,
        *,
        canvas=None,
        target_size=384,
        background="#ffffff",
        max_paths=600,
        max_total_points=200_000,
        max_colors=32,
        max_points_per_path=4096,
        max_warnings_per_code=8,
        on_overflow="trim",
        overflow="area",
        fill_dilate_px=0.0,
        stroke_to_fill=False,
        stroke_to_fill_max_width=DEFAULT_STROKE_TO_FILL_WIDTH,
        flatten_tolerance=0.35,
        include_fill=True,
        include_stroke=True,
        brush_preset=None,
    ):
        if on_overflow not in ("trim", "error"):
            raise SvgImportError(
                "SI_E_CONFIG", "on_overflow must be 'trim' or 'error'", "config"
            )
        if overflow not in ("area", "region"):
            raise SvgImportError(
                "SI_E_CONFIG", "overflow must be 'area' or 'region'", "config"
            )
        if isinstance(fill_dilate_px, bool):
            raise SvgImportError(
                "SI_E_CONFIG", "fill_dilate_px must be a number", "config"
            )
        try:
            fill_dilate = float(fill_dilate_px)
        except (TypeError, ValueError) as exc:
            raise SvgImportError(
                "SI_E_CONFIG", "fill_dilate_px must be a number", "config"
            ) from exc
        if not 0.0 <= fill_dilate <= 4.0:
            raise SvgImportError(
                "SI_E_CONFIG", "fill_dilate_px must be in [0, 4]", "config"
            )
        if not isinstance(stroke_to_fill, bool):
            raise SvgImportError(
                "SI_E_CONFIG", "stroke_to_fill must be a boolean", "config"
            )
        try:
            stroke_fill_width = float(stroke_to_fill_max_width)
        except (TypeError, ValueError) as exc:
            raise SvgImportError(
                "SI_E_CONFIG", "stroke_to_fill_max_width must be a number", "config"
            ) from exc
        if not 0.05 <= stroke_fill_width <= MAX_WIDTH:
            raise SvgImportError(
                "SI_E_CONFIG",
                f"stroke_to_fill_max_width must be in [0.05, {MAX_WIDTH}]",
                "config",
            )
        if isinstance(max_paths, bool) or not isinstance(max_paths, int):
            raise SvgImportError("SI_E_CONFIG", "max_paths must be an integer", "config")
        if not 1 <= max_paths <= MAX_ELEMENTS_HARD:
            raise SvgImportError(
                "SI_E_CONFIG",
                f"max_paths must be in [1, {MAX_ELEMENTS_HARD}] (got {max_paths})",
                "config",
            )
        if isinstance(max_colors, bool) or not isinstance(max_colors, int):
            raise SvgImportError("SI_E_CONFIG", "max_colors must be an integer", "config")
        if not 1 <= max_colors <= MAX_PALETTE:
            raise SvgImportError(
                "SI_E_CONFIG",
                f"max_colors must be in [1, {MAX_PALETTE}] (got {max_colors})",
                "config",
            )
        if isinstance(max_total_points, bool) or not isinstance(max_total_points, int):
            raise SvgImportError("SI_E_CONFIG", "max_total_points must be an integer", "config")
        if max_total_points < 16:
            raise SvgImportError(
                "SI_E_CONFIG", "max_total_points must be >= 16", "config"
            )
        if isinstance(max_points_per_path, bool) or not isinstance(max_points_per_path, int):
            raise SvgImportError(
                "SI_E_CONFIG", "max_points_per_path must be an integer", "config"
            )
        if not 3 <= max_points_per_path <= MAX_STROKE_POINTS:
            raise SvgImportError(
                "SI_E_CONFIG",
                f"max_points_per_path must be in [3, {MAX_STROKE_POINTS}]",
                "config",
            )
        if not 0.001 <= float(flatten_tolerance) <= 50.0:
            raise SvgImportError(
                "SI_E_CONFIG", "flatten_tolerance must be in [0.001, 50]", "config"
            )
        self.canvas = canvas
        self.target_size = int(target_size)
        self.background = background
        self.max_paths = max_paths
        self.max_total_points = max_total_points
        self.max_colors = max_colors
        self.max_points_per_path = max_points_per_path
        self.on_overflow = on_overflow
        self.overflow = overflow
        self.fill_dilate_px = fill_dilate
        self.stroke_to_fill = stroke_to_fill
        self.stroke_to_fill_max_width = stroke_fill_width
        self.tolerance = float(flatten_tolerance)
        self.include_fill = bool(include_fill)
        self.include_stroke = bool(include_stroke)
        self.brush_preset = brush_preset
        self.warnings = _Warnings(max_warnings_per_code)
        self.palette = _Palette(max_colors, self.warnings)
        self.defs = _Defs()
        self.css = []
        self.shapes = []
        self.layers = [{"id": 0, "name": "base", "order": 0}]
        self._layer_names = {"base"}
        self.stats = {
            "shapes_seen": 0,
            "unsupported": 0,
            "gradients": 0,
            "patterns": 0,
            "filters": 0,
            "clips": 0,
            "masks": 0,
            "dashes": 0,
            "strokes_to_fill": 0,
            "fills_dilated": 0,
        }
        self.width = 0
        self.height = 0
        self.viewbox = (0.0, 0.0, 1.0, 1.0)
        self.source = {}

    # -- document ---------------------------------------------------------

    def import_document(self, root, source_meta):
        self.source = dict(source_meta)
        self.defs = collect_defs(root)
        self.css = collect_css(root)
        viewbox = self._resolve_viewbox(root)
        self.viewbox = viewbox
        self.source["viewBox"] = [round(float(value), 4) for value in viewbox[0]]
        canvas_width, canvas_height = self._resolve_canvas(viewbox)
        self.width = canvas_width
        self.height = canvas_height
        matrix = self._viewport_matrix(viewbox, canvas_width, canvas_height, root)
        style = dict(_STYLE_DEFAULTS)
        ctx = _Context(matrix, style, 1.0, 0, 0, "<svg>")
        layer_index = 0
        child_index = 0
        for child in root:
            tag = _local_name(child.tag)
            if tag in ("title", "desc", "metadata", "style", "defs"):
                continue
            child_path = f"<svg>/[{child_index}]"
            if tag == "g":
                layer = 0
                if layer_index + 1 < MAX_LAYERS:
                    layer_index += 1
                    layer = layer_index
                    name = self._layer_name(child, layer_index)
                    self.layers.append(
                        {"id": layer, "name": name, "order": layer_index}
                    )
                else:
                    self.warnings.add(
                        "SI_W_LAYER_LIMIT",
                        child_path,
                        f"more than {MAX_LAYERS} top-level groups; merged into the last layer",
                    )
                self._walk(child, _Context(matrix, style, 1.0, layer, 1, child_path))
            else:
                self._walk(child, _Context(matrix, style, 1.0, 0, 1, child_path))
            child_index += 1

    def _layer_name(self, node, index):
        attrs = _attributes(node)
        raw = attrs.get("label") or attrs.get("id") or ""
        name = re.sub(r"[^A-Za-z0-9_. -]+", "_", str(raw)).strip()
        name = name[:64] or f"group{index}"
        base = name
        suffix = 2
        while name in self._layer_names:
            name = f"{base[:58]}_{suffix}"
            suffix += 1
        self._layer_names.add(name)
        return name

    def _resolve_viewbox(self, root):
        attrs = _attributes(root)
        raw_viewbox = attrs.get("viewBox") or attrs.get("viewbox")
        width = _parse_length(
            attrs.get("width"),
            default=None,
            percent_base=None,
            path="<svg>",
            warnings=self.warnings,
        )
        height = _parse_length(
            attrs.get("height"),
            default=None,
            percent_base=None,
            path="<svg>",
            warnings=self.warnings,
        )
        if raw_viewbox:
            numbers = _numbers(raw_viewbox)
            if len(numbers) != 4:
                raise SvgImportError(
                    "SI_E_VIEWBOX", f"viewBox needs 4 numbers (got {raw_viewbox!r})", "<svg>"
                )
            min_x, min_y, view_width, view_height = numbers
            if view_width <= 0 or view_height <= 0:
                raise SvgImportError(
                    "SI_E_VIEWBOX", "viewBox width/height must be positive", "<svg>"
                )
        else:
            if width is None or height is None:
                width = width if width is not None else 384.0
                height = height if height is not None else 384.0
                self.warnings.add(
                    "SI_W_VIEWBOX_MISSING",
                    "<svg>",
                    "no viewBox and no width/height; assuming 384x384 user units",
                )
            min_x, min_y = 0.0, 0.0
            view_width, view_height = float(width), float(height)
        if width is not None and width <= 0:
            width = None
        if height is not None and height <= 0:
            height = None
        return (min_x, min_y, view_width, view_height), width, height

    def _resolve_canvas(self, viewbox_info):
        min_x, min_y, view_width, view_height = viewbox_info[0]
        if self.canvas is None:
            long_side = max(16, min(MAX_CANVAS, int(self.target_size)))
            if view_width >= view_height:
                width = long_side
                height = max(1, int(round(long_side * view_height / view_width)))
            else:
                height = long_side
                width = max(1, int(round(long_side * view_width / view_height)))
        elif isinstance(self.canvas, dict):
            width = self.canvas.get("width")
            height = self.canvas.get("height")
        elif isinstance(self.canvas, (tuple, list)) and len(self.canvas) == 2:
            width, height = self.canvas
        else:
            raise SvgImportError(
                "SI_E_CANVAS", "canvas must be (width, height) or {'width','height'}", "config"
            )
        try:
            width = int(round(float(width)))
            height = int(round(float(height)))
        except (TypeError, ValueError) as exc:
            raise SvgImportError("SI_E_CANVAS", f"invalid canvas size: {exc}", "config") from exc
        clamped = False
        if width < MIN_CANVAS:
            width = MIN_CANVAS
            clamped = True
        if height < MIN_CANVAS:
            height = MIN_CANVAS
            clamped = True
        if width > MAX_CANVAS:
            width = MAX_CANVAS
            clamped = True
        if height > MAX_CANVAS:
            height = MAX_CANVAS
            clamped = True
        if clamped:
            self.warnings.add(
                "SI_W_CANVAS_CLAMPED",
                "config",
                f"canvas clamped to {width}x{height} (FDSL allows {MIN_CANVAS}..{MAX_CANVAS})",
            )
        return width, height

    def _viewport_matrix(self, viewbox_info, canvas_width, canvas_height, root):
        min_x, min_y, view_width, view_height = viewbox_info[0]
        attrs = _attributes(root)
        preserve = (attrs.get("preserveAspectRatio") or "xMidYMid meet").strip()
        if "none" in preserve:
            scale_x = canvas_width / view_width
            scale_y = canvas_height / view_height
            return (scale_x, 0.0, 0.0, scale_y, -min_x * scale_x, -min_y * scale_y)
        scale = min(canvas_width / view_width, canvas_height / view_height)
        align = preserve.split()[0]
        x_align = 0.5
        y_align = 0.5
        if align.startswith("xMin"):
            x_align = 0.0
        elif align.startswith("xMax"):
            x_align = 1.0
        if "YMin" in align:
            y_align = 0.0
        elif "YMax" in align:
            y_align = 1.0
        offset_x = (canvas_width - view_width * scale) * x_align - min_x * scale
        offset_y = (canvas_height - view_height * scale) * y_align - min_y * scale
        return (scale, 0.0, 0.0, scale, offset_x, offset_y)

    # -- style ------------------------------------------------------------

    def _element_declarations(self, node, tag, attrs):
        declarations = {}
        for prop in _STYLE_PROPS:
            if prop in attrs:
                declarations[prop] = attrs[prop]
        for _specificity, _order, selector, props in self.css:
            if _selector_matches(selector, tag, attrs):
                declarations.update(props)
        if "style" in attrs:
            declarations.update(_parse_declarations(attrs["style"]))
        return declarations

    def _compute_style(self, node, tag, attrs, inherited):
        declarations = self._element_declarations(node, tag, attrs)
        style = dict(_STYLE_DEFAULTS)
        style.update(inherited)
        for prop, value in declarations.items():
            if prop in _INHERITED_PROPS:
                style[prop] = value
        opacity = _clamp01(declarations.get("opacity", 1.0))
        return style, declarations, opacity

    # -- traversal ----------------------------------------------------------

    def _walk(self, node, ctx):
        tag = _local_name(node.tag)
        if tag in _SKIP_TAGS:
            return
        if ctx.depth > _MAX_DEPTH:
            self.warnings.add(
                "SI_W_DEPTH_LIMIT",
                ctx.path,
                f"nesting deeper than {_MAX_DEPTH}; subtree skipped",
            )
            return
        attrs = _attributes(node)
        style, declarations, opacity = self._compute_style(
            node, tag, attrs, ctx.style
        )
        display = str(declarations.get("display", "")).strip().lower()
        if display == "none":
            return
        matrix = ctx.matrix
        if attrs.get("transform"):
            matrix = _mat_mul(matrix, parse_transform(attrs["transform"], path=ctx.path))
        if tag in _GROUP_TAGS or tag == "svg":
            if tag == "svg" and ctx.depth > 0:
                self.warnings.add(
                    "SI_W_NESTED_SVG", ctx.path, "nested <svg> treated as a group"
                )
            child_style = style
            child_ctx = _Context(
                matrix,
                child_style,
                ctx.opacity * opacity,
                ctx.layer,
                ctx.depth + 1,
                ctx.path,
            )
            child_index = 0
            for child in node:
                child_tag = _local_name(child.tag)
                if child_tag in ("title", "desc", "metadata", "style"):
                    continue
                self._walk(
                    child,
                    _Context(
                        child_ctx.matrix,
                        child_ctx.style,
                        child_ctx.opacity,
                        child_ctx.layer,
                        child_ctx.depth,
                        f"{ctx.path}/{child_index}",
                    ),
                )
                child_index += 1
            return
        if tag in _SHAPE_TAGS:
            self._handle_shape(
                tag, attrs, style, declarations, ctx.opacity * opacity, matrix, ctx
            )
            return
        if tag == "text":
            self.warnings.add(
                "SI_W_TEXT_SKIPPED", ctx.path, "SVG text is not converted (v1 degrades to skip)"
            )
            self.stats["unsupported"] += 1
            return
        if tag == "image":
            self.warnings.add(
                "SI_W_IMAGE_SKIPPED", ctx.path, "SVG image is not converted (v1 degrades to skip)"
            )
            self.stats["unsupported"] += 1
            return
        if tag == "foreignobject":
            self.warnings.add(
                "SI_W_FOREIGNOBJECT_SKIPPED",
                ctx.path,
                "foreignObject is not converted",
            )
            self.stats["unsupported"] += 1
            return
        if tag == "use":
            self.warnings.add(
                "SI_W_USE_SKIPPED",
                ctx.path,
                "<use> is not resolved in v1; referenced shape skipped",
            )
            self.stats["unsupported"] += 1
            return
        self.warnings.add(
            "SI_W_UNSUPPORTED_ELEMENT", ctx.path, f"unsupported element <{tag}> skipped"
        )
        self.stats["unsupported"] += 1

    def _handle_shape(self, tag, attrs, style, declarations, opacity, matrix, ctx):
        path = ctx.path
        if "filter" in declarations and str(declarations["filter"]).strip().lower() != "none":
            self.warnings.add("SI_W_FILTER_IGNORED", path, "filter ignored")
            self.stats["filters"] += 1
        if "clip-path" in declarations and str(declarations["clip-path"]).strip().lower() != "none":
            self.warnings.add("SI_W_CLIP_IGNORED", path, "clip-path ignored")
            self.stats["clips"] += 1
        if "mask" in declarations and str(declarations["mask"]).strip().lower() != "none":
            self.warnings.add("SI_W_MASK_IGNORED", path, "mask ignored")
            self.stats["masks"] += 1
        for prop in ("marker-start", "marker-mid", "marker-end"):
            if prop in declarations and "none" not in str(declarations[prop]).lower():
                self.warnings.add("SI_W_MARKER_IGNORED", path, f"{prop} ignored")
                break
        if style.get("visibility", "visible").strip().lower() in ("hidden", "collapse"):
            return
        dash = str(declarations.get("stroke-dasharray", "")).strip()
        if dash and dash.lower() != "none":
            self.warnings.add(
                "SI_W_DASH_IGNORED",
                path,
                f"stroke-dasharray {dash!r} rendered as a solid stroke",
            )
            self.stats["dashes"] += 1
        tolerance = self.tolerance / max(_mat_scale(matrix), 1e-9)
        subpaths = self._geometry(tag, attrs, tolerance, path)
        if not subpaths:
            self.warnings.add(
                "SI_W_DEGENERATE_SKIPPED", path, f"<{tag}> has no usable geometry"
            )
            return
        for subpath in subpaths:
            subpath["points"] = [
                _mat_apply(matrix, x, y) for x, y in subpath["points"]
            ]
        fill = self._resolve_paint(style.get("fill", "#000000"), style, "fill", path)
        stroke = self._resolve_paint(style.get("stroke", "none"), style, "stroke", path)
        if fill["type"] == "none" and stroke["type"] == "none":
            return
        fill_alpha = _clamp01(style.get("fill-opacity", "1")) * opacity
        stroke_alpha = _clamp01(style.get("stroke-opacity", "1")) * opacity
        width = _parse_length(
            style.get("stroke-width", "1"),
            default=1.0,
            path=path,
            warnings=self.warnings,
        )
        if width is None:
            width = 1.0
        width = float(width) * _mat_scale(matrix)
        self.shapes.append(
            _Shape(
                tag=tag,
                path=path,
                subpaths=subpaths,
                fill=fill,
                stroke=stroke,
                fill_rule=str(style.get("fill-rule", "nonzero")).strip().lower(),
                opacity=opacity,
                fill_alpha=fill_alpha,
                stroke_alpha=stroke_alpha,
                stroke_width=width,
                layer=ctx.layer,
            )
        )
        self.stats["shapes_seen"] += 1

    def _resolve_paint(self, raw, style, kind, path):
        text = "" if raw is None else str(raw).strip()
        if not text or text.lower() == "none" or text.lower() == "transparent":
            return {"type": "none", "rgba": (0, 0, 0, 0.0)}
        if text.lower() == "currentcolor":
            text = str(style.get("color", "#000000"))
        match = _URL_RE.match(text)
        if match:
            reference = match.group(1)
            gradient = self.defs.gradients.get(reference)
            if gradient is not None:
                color = gradient["color"]
                self.warnings.add(
                    "SI_W_GRADIENT_APPROX",
                    path,
                    f"gradient #{reference} approximated by its average colour "
                    f"{_rgb_hex(color)}",
                )
                self.stats["gradients"] += 1
                return {
                    "type": "solid",
                    "rgba": (color[0], color[1], color[2], color[3]),
                }
            if reference in self.defs.patterns:
                color = self.defs.patterns[reference] or (128, 128, 128, 1.0)
                self.warnings.add(
                    "SI_W_PATTERN_APPROX",
                    path,
                    f"pattern #{reference} approximated by {_rgb_hex(color)}",
                )
                self.stats["patterns"] += 1
                return {
                    "type": "solid",
                    "rgba": (color[0], color[1], color[2], color[3]),
                }
            self.warnings.add(
                "SI_W_PAINT_FALLBACK",
                path,
                f"paint reference url(#{reference}) is not a gradient/pattern; "
                "using black",
            )
            return {"type": "solid", "rgba": (0, 0, 0, 1.0)}
        color = _parse_color(text)
        if color is None:
            self.warnings.add(
                "SI_W_UNSUPPORTED_PAINT", path, f"unsupported {kind} colour {text!r}; using black"
            )
            return {"type": "solid", "rgba": (0, 0, 0, 1.0)}
        return {"type": "solid", "rgba": color}

    # -- primitives ---------------------------------------------------------

    def _geometry(self, tag, attrs, tolerance, path):
        try:
            if tag == "path":
                return parse_path_data(
                    attrs.get("d", ""),
                    tolerance=tolerance,
                    path=path,
                    warnings=self.warnings,
                )
            if tag == "polygon" or tag == "polyline":
                points = self._pair_list(attrs.get("points", ""), path)
                if len(points) < (3 if tag == "polygon" else 2):
                    return []
                return [
                    {
                        "points": points,
                        "closed": tag == "polygon",
                    }
                ]
            if tag == "rect":
                return self._rect(attrs, path)
            if tag == "circle":
                cx = _parse_length(attrs.get("cx", "0"), default=0.0) or 0.0
                cy = _parse_length(attrs.get("cy", "0"), default=0.0) or 0.0
                r = _parse_length(attrs.get("r", "0"), default=0.0) or 0.0
                if r <= 0:
                    return []
                return [
                    {
                        "points": _ellipse_points(cx, cy, r, r, tolerance),
                        "closed": True,
                    }
                ]
            if tag == "ellipse":
                cx = _parse_length(attrs.get("cx", "0"), default=0.0) or 0.0
                cy = _parse_length(attrs.get("cy", "0"), default=0.0) or 0.0
                rx = _parse_length(attrs.get("rx", "0"), default=0.0) or 0.0
                ry = _parse_length(attrs.get("ry", "0"), default=0.0) or 0.0
                if rx <= 0 or ry <= 0:
                    return []
                return [
                    {
                        "points": _ellipse_points(cx, cy, rx, ry, tolerance),
                        "closed": True,
                    }
                ]
            if tag == "line":
                x1 = _parse_length(attrs.get("x1", "0"), default=0.0) or 0.0
                y1 = _parse_length(attrs.get("y1", "0"), default=0.0) or 0.0
                x2 = _parse_length(attrs.get("x2", "0"), default=0.0) or 0.0
                y2 = _parse_length(attrs.get("y2", "0"), default=0.0) or 0.0
                return [{"points": [(x1, y1), (x2, y2)], "closed": False}]
        except SvgImportError:
            raise
        except (TypeError, ValueError) as exc:
            raise SvgImportError(
                "SI_E_GEOMETRY", f"invalid <{tag}> geometry: {exc}", path
            ) from exc
        return []

    def _pair_list(self, text, path):
        numbers = _numbers(text)
        if len(numbers) % 2:
            raise SvgImportError(
                "SI_E_POINTS", f"point list has an odd number of coordinates: {text!r}", path
            )
        return [(numbers[index], numbers[index + 1]) for index in range(0, len(numbers), 2)]

    def _rect(self, attrs, path):
        x = _parse_length(attrs.get("x", "0"), default=0.0) or 0.0
        y = _parse_length(attrs.get("y", "0"), default=0.0) or 0.0
        width = _parse_length(attrs.get("width", "0"), default=0.0) or 0.0
        height = _parse_length(attrs.get("height", "0"), default=0.0) or 0.0
        if width <= 0 or height <= 0:
            return []
        if attrs.get("rx") or attrs.get("ry"):
            self.warnings.add(
                "SI_W_ROUNDED_RECT_APPROX",
                path,
                "rounded rect corners approximated as sharp corners",
            )
        return [
            {
                "points": [
                    (x, y),
                    (x + width, y),
                    (x + width, y + height),
                    (x, y + height),
                ],
                "closed": True,
            }
        ]

    # -- conversion ---------------------------------------------------------

    def _build_candidates(self):
        candidates = []
        order = 0
        for shape in self.shapes:
            if self.include_fill and shape.fill["type"] == "solid":
                new = self._fill_candidates(shape, order)
                candidates.extend(new)
                order += len(new)
            if self.include_stroke and shape.stroke["type"] == "solid":
                new = None
                if (
                    self.stroke_to_fill
                    and float(shape.stroke_width) <= self.stroke_to_fill_max_width
                ):
                    new = self._stroke_fill_candidates(shape, order)
                if new is None:
                    new = self._stroke_candidates(shape, order)
                else:
                    self.stats["strokes_to_fill"] += len(new)
                candidates.extend(new)
                order += len(new)
        return candidates

    def _fill_candidates(self, shape, order):
        rings = [
            subpath["points"]
            for subpath in shape.subpaths
            if len(subpath["points"]) >= 3
            and abs(_ring_area(subpath["points"])) >= _MIN_RING_AREA
        ]
        if not rings:
            if any(len(subpath["points"]) >= 3 for subpath in shape.subpaths):
                self.warnings.add(
                    "SI_W_DEGENERATE_SKIPPED",
                    shape.path,
                    "fill ring area is degenerate (all rings below the minimum area)",
                )
            return []
        regions = self._group_rings(rings, shape.fill_rule, shape.path)
        candidates = []
        for outer, holes in regions:
            poly = _decimate(list(outer), self.max_points_per_path, closed=True)
            if len(poly) < 3:
                continue
            if len(poly) < len(outer):
                self.warnings.add(
                    "SI_W_POINTS_DECIMATED",
                    shape.path,
                    f"fill reduced from {len(outer)} to {len(poly)} points "
                    f"(max_points_per_path={self.max_points_per_path})",
                )
            if self.fill_dilate_px > 0.0:
                weight = abs(_ring_area(poly))
                grown = _offset_polygon(poly, self.fill_dilate_px)
                if len(grown) == len(poly):
                    poly = grown
                    self.stats["fills_dilated"] += 1
            else:
                weight = abs(_ring_area(poly))
            hole_points = []
            ordered_holes = sorted(holes, key=lambda ring: -abs(_ring_area(ring)))
            for hole in ordered_holes[:MAX_HOLES]:
                reduced = _decimate(list(hole), self.max_points_per_path, closed=True)
                if self.fill_dilate_px > 0.0 and len(reduced) >= 3:
                    # Shrink holes so the fill also closes seams along hole edges.
                    reduced = _offset_polygon(reduced, -self.fill_dilate_px)
                if len(reduced) >= 3:
                    hole_points.append(_points_out(reduced))
            if len(ordered_holes) > MAX_HOLES:
                self.warnings.add(
                    "SI_W_HOLE_LIMIT",
                    shape.path,
                    f"{len(ordered_holes)} holes exceed the limit of {MAX_HOLES}; "
                    "smallest dropped",
                )
            color = shape.fill["rgba"]
            alpha = _clamp01(shape.fill_alpha * float(color[3]))
            if alpha <= _MIN_ALPHA:
                continue
            element = {
                "kind": "fill",
                "layer": int(shape.layer),
                "opacity": round(alpha, 3),
                "poly": _points_out(poly),
                "color": None,
                "_rgb": (color[0], color[1], color[2]),
            }
            if hole_points:
                element["holes"] = hole_points
            candidates.append(
                {
                    "element": element,
                    "weight": weight,
                    "points": len(poly) + sum(len(h) for h in hole_points),
                    "order": order,
                }
            )
            order += 1
        return candidates

    def _stroke_candidates(self, shape, order):
        color = shape.stroke["rgba"]
        alpha = _clamp01(shape.stroke_alpha * float(color[3]))
        if alpha <= _MIN_ALPHA:
            return []
        width = float(shape.stroke_width)
        if width < MIN_WIDTH:
            width = MIN_WIDTH
            self.warnings.add(
                "SI_W_STROKE_WIDTH_CLAMPED",
                shape.path,
                f"stroke-width clamped to {MIN_WIDTH}",
            )
        elif width > MAX_WIDTH:
            width = MAX_WIDTH
            self.warnings.add(
                "SI_W_STROKE_WIDTH_CLAMPED",
                shape.path,
                f"stroke-width clamped to {MAX_WIDTH}",
            )
        preset = self.brush_preset or brush_preset_for_width(width)
        candidates = []
        for subpath in shape.subpaths:
            points = subpath["points"]
            if len(points) < 2:
                continue
            reduced = _decimate(list(points), min(self.max_points_per_path, MAX_STROKE_POINTS),
                                closed=bool(subpath["closed"]))
            if len(reduced) < len(points):
                self.warnings.add(
                    "SI_W_POINTS_DECIMATED",
                    shape.path,
                    f"stroke reduced from {len(points)} to {len(reduced)} points",
                )
            elements_points = _points_out(reduced)
            if len(elements_points) < 2:
                continue
            element = {
                "kind": "stroke",
                "layer": int(shape.layer),
                "points": elements_points,
                "closed": bool(subpath["closed"]),
                "opacity": round(alpha, 3),
                "brush": {
                    "preset": preset,
                    "width": round(width, 2),
                    "alpha": 1.0,
                    "color": None,
                },
                "_rgb": (color[0], color[1], color[2]),
            }
            length = sum(
                math.hypot(
                    reduced[index + 1][0] - reduced[index][0],
                    reduced[index + 1][1] - reduced[index][1],
                )
                for index in range(len(reduced) - 1)
            )
            if subpath["closed"]:
                length += math.hypot(
                    reduced[0][0] - reduced[-1][0], reduced[0][1] - reduced[-1][1]
                )
            candidates.append(
                {
                    "element": element,
                    "weight": max(0.01, length * width),
                    "points": len(reduced),
                    "order": order,
                }
            )
            order += 1
        return candidates

    def _stroke_fill_candidates(self, shape, order):
        """Convert a thin stroke into fill ribbons (``stroke_to_fill``).

        Returns ``None`` when any subpath cannot be converted safely, in which
        case the caller falls back to normal Fourier strokes.
        """
        color = shape.stroke["rgba"]
        alpha = _clamp01(shape.stroke_alpha * float(color[3]))
        if alpha <= _MIN_ALPHA:
            return None
        width = float(shape.stroke_width)
        if width < MIN_WIDTH:
            width = MIN_WIDTH
        elif width > MAX_WIDTH:
            width = MAX_WIDTH
        half = width / 2.0
        candidates = []
        centre_limit = max(3, min(self.max_points_per_path // 2, MAX_STROKE_POINTS))
        for subpath in shape.subpaths:
            points = subpath["points"]
            if len(points) < 2:
                continue
            reduced = _decimate(
                list(points), centre_limit, closed=bool(subpath["closed"])
            )
            if subpath["closed"]:
                rings = _closed_stroke_rings(reduced, half)
                if rings is None:
                    return None
                polygon = _keyhole_polygon(rings[0], rings[1])
            else:
                polygon = _open_ribbon(reduced, half)
            if not polygon or len(polygon) < 3:
                return None
            if len(polygon) > self.max_points_per_path:
                polygon = _decimate(polygon, self.max_points_per_path, closed=True)
            polygon = _points_out(polygon)
            length = sum(
                math.hypot(
                    reduced[index + 1][0] - reduced[index][0],
                    reduced[index + 1][1] - reduced[index][1],
                )
                for index in range(len(reduced) - 1)
            )
            if subpath["closed"]:
                length += math.hypot(
                    reduced[0][0] - reduced[-1][0], reduced[0][1] - reduced[-1][1]
                )
            element = {
                "kind": "fill",
                "layer": int(shape.layer),
                "opacity": round(alpha, 3),
                "poly": polygon,
                "color": None,
                "_rgb": (color[0], color[1], color[2]),
            }
            candidates.append(
                {
                    "element": element,
                    "weight": max(0.01, length * width),
                    "points": len(polygon),
                    "order": order,
                }
            )
            order += 1
        return candidates or None

    def _group_rings(self, rings, fill_rule, path):
        """Group rings into ``[(outer, [holes])]`` using containment + winding."""
        count = len(rings)
        if count == 1:
            return [(rings[0], [])]
        if count > _MAX_NESTING_RINGS:
            self.warnings.add(
                "SI_W_FILL_RULE_APPROX",
                path,
                f"{count} subpaths exceed nesting analysis limit; each is filled separately",
            )
            return [(ring, []) for ring in rings]
        areas = [abs(_ring_area(ring)) for ring in rings]
        parents = [-1] * count
        for index in range(count):
            best = -1
            best_area = None
            for other in range(count):
                if other == index:
                    continue
                if areas[other] <= areas[index]:
                    continue
                if not _point_in_ring(rings[index][0], rings[other]):
                    continue
                if best_area is None or areas[other] < best_area:
                    best_area = areas[other]
                    best = other
            parents[index] = best
        depths = [0] * count
        for index in range(count):
            depth = 0
            parent = parents[index]
            guard = 0
            while parent != -1 and guard <= count:
                depth += 1
                parent = parents[parent]
                guard += 1
            depths[index] = depth
        evenodd = fill_rule == "evenodd"
        signs = [1 if _ring_area(ring) >= 0 else -1 for ring in rings]
        is_hole = [False] * count
        if evenodd:
            for index in range(count):
                is_hole[index] = depths[index] % 2 == 1
        else:
            for index in range(count):
                parent = parents[index]
                if parent != -1 and signs[index] != signs[parent]:
                    is_hole[index] = True
        regions = {}
        region_order = []
        for index in range(count):
            if is_hole[index]:
                continue
            regions[index] = []
            region_order.append(index)
        if not region_order:  # defensive: every ring reported as a hole
            return [(ring, []) for ring in rings]
        for index in range(count):
            if not is_hole[index]:
                continue
            parent = parents[index]
            guard = 0
            while parent != -1 and is_hole[parent] and guard <= count:
                parent = parents[parent]
                guard += 1
            if parent in regions:
                regions[parent].append(rings[index])
        return [(rings[index], regions[index]) for index in region_order]

    # -- limits / assembly ---------------------------------------------------

    def _apply_limits(self, candidates):
        if len(candidates) > self.max_paths:
            if self.on_overflow == "error":
                raise SvgImportError(
                    "SI_E_PATH_LIMIT",
                    f"{len(candidates)} drawable paths exceed max_paths={self.max_paths}; "
                    "raise max_paths or pass on_overflow='trim'",
                    "<svg>",
                )
            if self.overflow == "region":
                # Region priority: keep whole same-colour fill buckets first,
                # then spend the remaining path budget on the heaviest strokes.
                fill_indices = [
                    index
                    for index, candidate in enumerate(candidates)
                    if candidate["element"]["kind"] == "fill"
                ]
                stroke_indices = [
                    index
                    for index, candidate in enumerate(candidates)
                    if candidate["element"]["kind"] != "fill"
                ]
                keep = set()
                if fill_indices:
                    bucket_colors = _rank_palette(
                        [candidates[index] for index in fill_indices],
                        self.max_colors,
                    )
                    weights = {}
                    first_seen = {}
                    groups = {}
                    for index in fill_indices:
                        rgb = candidates[index]["element"].get("_rgb") or (0, 0, 0)
                        rgb = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
                        key = _nearest_color_index(rgb, bucket_colors)
                        groups.setdefault(key, []).append(index)
                        first_seen.setdefault(key, index)
                        weights[key] = weights.get(key, 0.0) + max(
                            float(candidates[index]["weight"]), 1e-9
                        )
                    ranked_groups = sorted(
                        groups, key=lambda key: (-weights[key], first_seen[key])
                    )
                    total_weight = sum(weights.values()) or 1.0
                    cap = max(1, int(round(self.max_paths * 0.35)))
                    quotas = {}
                    for key in ranked_groups:
                        share = int(
                            self.max_paths * weights[key] / total_weight
                        )
                        quotas[key] = min(len(groups[key]), max(0, share), cap)
                    for key in ranked_groups:
                        for index in groups[key][: quotas[key]]:
                            if len(keep) >= self.max_paths:
                                break
                            keep.add(index)
                        if len(keep) >= self.max_paths:
                            break
                    # Round-robin the remainder so a single huge colour bucket
                    # cannot starve every other colour.
                    cursor = {key: quotas[key] for key in ranked_groups}
                    active = [
                        key
                        for key in ranked_groups
                        if cursor[key] < len(groups[key])
                    ]
                    while active and len(keep) < self.max_paths:
                        progressed = False
                        for key in active:
                            if len(keep) >= self.max_paths:
                                break
                            index = groups[key][cursor[key]]
                            cursor[key] += 1
                            keep.add(index)
                            progressed = True
                        active = [
                            key
                            for key in active
                            if cursor[key] < len(groups[key])
                        ]
                        if not progressed:
                            break
                for index in sorted(
                    stroke_indices,
                    key=lambda item: (
                        -candidates[item]["weight"],
                        candidates[item]["order"],
                    ),
                ):
                    if len(keep) >= self.max_paths:
                        break
                    keep.add(index)
                dropped = len(candidates) - len(keep)
                if fill_indices:
                    detail = (
                        "largest same-colour fill regions kept first, "
                        "document order preserved"
                    )
                else:
                    detail = "largest ink area kept, document order preserved"
                self.warnings.add(
                    "SI_W_PATH_LIMIT",
                    "<svg>",
                    f"{dropped} path(s) dropped (max_paths={self.max_paths}); {detail}",
                )
                candidates = [
                    candidate
                    for index, candidate in enumerate(candidates)
                    if index in keep
                ]
            else:
                ranked = sorted(
                    range(len(candidates)),
                    key=lambda index: (-candidates[index]["weight"], candidates[index]["order"]),
                )
                keep = set(ranked[: self.max_paths])
                dropped = len(candidates) - len(keep)
                self.warnings.add(
                    "SI_W_PATH_LIMIT",
                    "<svg>",
                    f"{dropped} path(s) dropped (max_paths={self.max_paths}); "
                    "largest ink area kept, document order preserved",
                )
                candidates = [
                    candidate
                    for index, candidate in enumerate(candidates)
                    if index in keep
                ]
        total = 0
        kept = []
        for index, candidate in enumerate(candidates):
            if total + candidate["points"] > self.max_total_points:
                if self.on_overflow == "error":
                    raise SvgImportError(
                        "SI_E_POINT_LIMIT",
                        f"total point budget {self.max_total_points} exceeded at path {index}",
                        "<svg>",
                    )
                dropped = len(candidates) - index
                self.warnings.add(
                    "SI_W_TOTAL_POINTS_LIMIT",
                    "<svg>",
                    f"{dropped} path(s) dropped: total point budget "
                    f"{self.max_total_points} reached",
                )
                break
            total += candidate["points"]
            kept.append(candidate)
        return kept

    def _assemble(self, candidates, name):
        elements = []
        for candidate in candidates:
            element = candidate["element"]
            rgb = element.pop("_rgb")
            color_name = self.palette.add(rgb)
            if element["kind"] == "fill":
                element["color"] = color_name
            else:
                element["brush"]["color"] = color_name
            element["id"] = f"e{len(elements) + 1}"
            elements.append(element)
        if not elements:
            raise SvgImportError(
                "SI_E_EMPTY",
                "SVG produced no drawable elements after import/degradation",
                "<svg>",
            )
        plan = {
            "schema": FDSL_SCHEMA,
            "name": name,
            "description": (
                f"Imported from SVG ({len(elements)} element(s), "
                f"{len(self.warnings.items)} warning(s))"
            ),
            "canvas": {
                "width": int(self.width),
                "height": int(self.height),
            },
            "layers": self.layers,
            "elements": elements,
            "constraints": {
                "max_elements": min(
                    MAX_ELEMENTS_HARD, max(len(elements), 400)
                )
            },
            "meta": {
                "source": self.source,
                "svg_import": {
                    "tool": "fourier_prompt.svg_import",
                    "version": IMPORTER_VERSION,
                    "flatten_tolerance_px": self.tolerance,
                    "on_overflow": self.on_overflow,
                    "overflow": self.overflow,
                    "fill_dilate_px": round(self.fill_dilate_px, 3),
                    "stroke_to_fill": bool(self.stroke_to_fill),
                    "stroke_to_fill_max_width": round(
                        self.stroke_to_fill_max_width, 3
                    ),
                    "palette_ranking": "ink_weight",
                    "max_paths": self.max_paths,
                    "max_total_points": self.max_total_points,
                    "max_colors": self.max_colors,
                    "max_points_per_path": self.max_points_per_path,
                },
            },
        }
        background = self._background_value()
        if background is not None:
            plan["canvas"]["background"] = background
        palette = self.palette.as_dict()
        if palette:
            plan["palette"] = palette
        report = validate_plan_detailed(plan)
        if not report["ok"]:
            first = report["errors"][0]
            raise SvgImportError(
                "SI_E_PLAN",
                f"generated plan is invalid: {first.get('code')} at "
                f"{first.get('path')}: {first.get('message')}",
                "$",
                details=report["errors"],
            )
        for item in report["warnings"]:
            self.warnings.add(
                "SI_W_PLAN_" + str(item.get("code", "")).replace("FP_W_", ""),
                str(item.get("path", "$")),
                str(item.get("message", "")),
            )
        return plan

    def _background_value(self):
        if self.background is None:
            return None
        if isinstance(self.background, (list, tuple)):
            return list(self.background)
        color = _parse_color(str(self.background))
        if color is None:
            return None
        alpha = color[3]
        if alpha >= 0.999:
            return _rgb_hex(color)
        return _rgb_hex(color) + "%02x" % int(round(alpha * 255.0))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _read_source(source):
    """Return ``(root, meta)`` for an SVG path / text / bytes."""
    raw = None
    label = "<svg>"
    if isinstance(source, bytes):
        raw = source
    elif isinstance(source, os.PathLike):
        label = os.fspath(source)
        try:
            with open(label, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            raise SvgImportError("SI_E_IO", f"cannot read SVG file: {exc}", label) from exc
    elif isinstance(source, str):
        if "<" in source:
            raw = source.encode("utf-8")
            label = "<svg>"
        else:
            label = source
            if not os.path.exists(label):
                raise SvgImportError("SI_E_INPUT", f"SVG file not found: {label}", "<svg>")
            try:
                with open(label, "rb") as handle:
                    raw = handle.read()
            except OSError as exc:
                raise SvgImportError("SI_E_IO", f"cannot read SVG file: {exc}", label) from exc
    else:
        raise SvgImportError(
            "SI_E_INPUT",
            f"source must be an SVG path, text or bytes (got {type(source).__name__})",
            "<svg>",
        )
    if len(raw) > MAX_SOURCE_BYTES:
        raise SvgImportError(
            "SI_E_TOO_LARGE",
            f"SVG is {len(raw)} bytes (hard limit {MAX_SOURCE_BYTES} bytes)",
            "<svg>",
        )
    if not raw.strip():
        raise SvgImportError("SI_E_INPUT", "SVG source is empty", "<svg>")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SvgImportError("SI_E_PARSE", f"invalid SVG XML: {exc}", "<svg>") from exc
    if _local_name(root.tag) != "svg":
        raise SvgImportError(
            "SI_E_NOT_SVG", f"root element is <{_local_name(root.tag)}>, not <svg>", "<svg>"
        )
    meta = {
        "format": "svg",
        "name": os.path.basename(label) if label not in ("<svg>", "") else "<svg>",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }
    return root, meta


def _plan_name(explicit, meta):
    if explicit:
        text = str(explicit).strip()
    else:
        base = str(meta.get("name") or "svg_import")
        if base.lower().endswith(".svg"):
            base = base[:-4]
        text = base or "svg_import"
    cleaned = re.sub(r"[^\w.-]+", "_", text, flags=re.UNICODE).strip("_.-")
    return (cleaned or "svg_import")[:200]


def svg_to_fdsl(
    svg_text_or_path,
    *,
    canvas=None,
    name=None,
    background="#ffffff",
    target_size=384,
    max_paths=600,
    max_total_points=200_000,
    max_colors=32,
    max_points_per_path=4096,
    max_warnings_per_code=8,
    on_overflow="trim",
    overflow="area",
    fill_dilate_px=0.0,
    stroke_to_fill=False,
    stroke_to_fill_max_width=DEFAULT_STROKE_TO_FILL_WIDTH,
    flatten_tolerance=0.35,
    include_fill=True,
    include_stroke=True,
    brush_preset=None,
):
    """Convert an SVG document into a validated FDSL v2.1 plan.

    Parameters
    ----------
    svg_text_or_path:
        SVG text (``str`` starting with ``<`` or containing ``<``), raw ``bytes``
        or a filesystem path (``str`` / ``os.PathLike``).
    canvas:
        ``(width, height)`` or ``{"width": .., "height": ..}`` in FDSL pixels;
        ``None`` derives it from the SVG ``viewBox`` (long side = ``target_size``).
    max_paths / max_total_points / max_colors / max_points_per_path:
        conversion limits; see module docstring.
    on_overflow:
        ``"trim"`` (drop + warning, default) or ``"error"`` (raise
        :class:`SvgImportError`).
    overflow:
        ``"area"`` (default, keep largest individual ink areas) or
        ``"region"`` (rank whole same-colour groups by total ink weight).
    fill_dilate_px:
        Expand each fill ring by this many canvas px (holes shrink); ``0.0``
        keeps exact v1.0 geometry, ``0.5`` closes anti-aliasing seams.
    stroke_to_fill / stroke_to_fill_max_width:
        Convert strokes up to this canvas width into fill ribbons instead of
        Fourier strokes (opt-in).
    flatten_tolerance:
        curve flattening tolerance in canvas pixels.
    include_fill / include_stroke:
        allow disabling one half of the conversion.
    brush_preset:
        force a brush preset for strokes (default: ``brush_preset_for_width``).

    Returns
    -------
    ``(plan, warnings, stats)``
    """
    started = time.perf_counter()
    root, meta = _read_source(svg_text_or_path)
    importer = _Importer(
        canvas=canvas,
        target_size=target_size,
        background=background,
        max_paths=max_paths,
        max_total_points=max_total_points,
        max_colors=max_colors,
        max_points_per_path=max_points_per_path,
        max_warnings_per_code=max_warnings_per_code,
        on_overflow=on_overflow,
        overflow=overflow,
        fill_dilate_px=fill_dilate_px,
        stroke_to_fill=stroke_to_fill,
        stroke_to_fill_max_width=stroke_to_fill_max_width,
        flatten_tolerance=flatten_tolerance,
        include_fill=include_fill,
        include_stroke=include_stroke,
        brush_preset=brush_preset,
    )
    importer.import_document(root, meta)
    candidates = importer._build_candidates()
    if not candidates:
        raise SvgImportError(
            "SI_E_EMPTY",
            "SVG has no drawable fill/stroke geometry after import/degradation",
            "<svg>",
        )
    kept = importer._apply_limits(candidates)
    importer.palette.seed(_rank_palette(kept, importer.max_colors))
    plan = importer._assemble(kept, _plan_name(name, meta))
    warnings = importer.warnings.finish()
    stats = {
        "source": {
            "name": meta.get("name"),
            "bytes": meta.get("bytes"),
            "sha256": meta.get("sha256"),
            "viewBox": [round(value, 4) for value in importer.viewbox[0]],
        },
        "canvas": {"width": importer.width, "height": importer.height},
        "layers": len(importer.layers),
        "shapes_seen": importer.stats["shapes_seen"],
        "paths_seen": len(candidates),
        "elements": len(plan["elements"]),
        "fills": sum(1 for element in plan["elements"] if element["kind"] == "fill"),
        "strokes": sum(1 for element in plan["elements"] if element["kind"] == "stroke"),
        "paths_dropped": len(candidates) - len(kept),
        "points": sum(_count_points(element) for element in plan["elements"]),
        "colors": len(importer.palette.entries),
        "quantized_colors": importer.palette.quantized,
        "palette_ranking": "ink_weight",
        "fill_dilate_px": round(importer.fill_dilate_px, 3),
        "fills_dilated": importer.stats["fills_dilated"],
        "overflow": importer.overflow,
        "stroke_to_fill": bool(importer.stroke_to_fill),
        "strokes_to_fill": importer.stats["strokes_to_fill"],
        "warnings": len(warnings),
        "warning_counts": dict(sorted(importer.warnings.counts.items())),
        "seconds": round(time.perf_counter() - started, 3),
    }
    return plan, warnings, stats


def _count_points(element):
    if element["kind"] == "fill":
        return len(element["poly"]) + sum(len(hole) for hole in element.get("holes") or [])
    return len(element.get("points") or [])


#: Backwards-friendly alias used by early pipeline drafts.
import_svg = svg_to_fdsl


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_canvas_arg(text):
    if not text:
        return None
    parts = re.split(r"[xX,]", text.strip())
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("--canvas expects WxH, e.g. 384x384")
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid --canvas {text!r}") from exc


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m fourier_prompt.svg_import",
        description="Convert SVG to an FDSL v2.1 plan (and optionally render PNG/.fdraw).",
    )
    parser.add_argument("--svg", required=True, help="input SVG path")
    parser.add_argument("--out", help="output FDSL plan JSON (default: stdout)")
    parser.add_argument("--png", help="render a PNG after compiling the plan")
    parser.add_argument("--fdraw", help="pack a .fdraw container (render.json + source FDSL)")
    parser.add_argument("--canvas", type=_parse_canvas_arg, default=None,
                        help="canvas WxH (default derived from viewBox)")
    parser.add_argument("--target-size", type=int, default=384,
                        help="long side of the derived canvas (default 384)")
    parser.add_argument("--background", default="#ffffff",
                        help="canvas background colour (default #ffffff; 'none' to omit)")
    parser.add_argument("--max-paths", type=int, default=600)
    parser.add_argument("--max-total-points", type=int, default=200_000)
    parser.add_argument("--max-colors", type=int, default=64,
                        help="palette size cap, up to 64 (default 64)")
    parser.add_argument("--max-points-per-path", type=int, default=4096)
    parser.add_argument("--flatten", dest="flatten_tolerance", type=float, default=0.35,
                        help="curve flattening tolerance in canvas px (default 0.35)")
    parser.add_argument("--on-overflow", choices=("trim", "error"), default="trim")
    parser.add_argument("--overflow", choices=("area", "region"), default="area",
                        help="path-limit ranking: area keeps largest single paths "
                             "(default), region keeps whole same-colour fill groups")
    parser.add_argument("--fill-dilate", dest="fill_dilate_px", type=float,
                        default=DEFAULT_FILL_DILATE_PX,
                        help="expand fills by N px to close anti-alias seams "
                             "(0-4, default 0.5; 0 restores v1.0 geometry)")
    parser.add_argument("--no-fill", dest="include_fill", action="store_false")
    parser.add_argument("--no-stroke", dest="include_stroke", action="store_false",
                        help="drop all strokes (fastest; fills only)")
    parser.add_argument("--stroke-to-fill", dest="stroke_to_fill",
                        action="store_true",
                        help="convert thin strokes to fill ribbons instead of "
                             "Fourier strokes")
    parser.add_argument("--stroke-fill-max-width", type=float,
                        default=DEFAULT_STROKE_TO_FILL_WIDTH,
                        help="max canvas stroke width converted by "
                             "--stroke-to-fill (default 2.0 px)")
    parser.add_argument("--brush", default=None, help="force a brush preset for strokes")
    parser.add_argument("--resolution", type=int, default=256,
                        help="render resolution for --png/--fdraw (default 256)")
    parser.add_argument("--name", default=None, help="FDSL plan name")
    parser.add_argument("--json", action="store_true", help="print conversion stats as JSON")
    parser.add_argument("--quiet", action="store_true", help="suppress stderr warnings summary")
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    background = None if str(args.background).lower() == "none" else args.background
    try:
        plan, warnings, stats = svg_to_fdsl(
            args.svg,
            canvas=args.canvas,
            name=args.name,
            background=background,
            target_size=args.target_size,
            max_paths=args.max_paths,
            max_total_points=args.max_total_points,
            max_colors=args.max_colors,
            max_points_per_path=args.max_points_per_path,
            on_overflow=args.on_overflow,
            overflow=args.overflow,
            fill_dilate_px=args.fill_dilate_px,
            stroke_to_fill=args.stroke_to_fill,
            stroke_to_fill_max_width=args.stroke_fill_max_width,
            flatten_tolerance=args.flatten_tolerance,
            include_fill=args.include_fill,
            include_stroke=args.include_stroke,
            brush_preset=args.brush,
        )
    except SvgImportError as exc:
        print(f"svg_import: {exc.code}: {exc.message} ({exc.path})", file=sys.stderr)
        return 1

    payload = json.dumps(plan, ensure_ascii=False, indent=2)
    if args.out:
        try:
            directory = os.path.dirname(os.path.abspath(args.out))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload + "\n")
        except OSError as exc:
            print(f"svg_import: cannot write {args.out}: {exc}", file=sys.stderr)
            return 3
    else:
        print(payload)

    if args.png or args.fdraw:
        try:
            from .compile import CompileError, compile_plan
        except ImportError as exc:  # pragma: no cover - broken installation
            print(f"svg_import: compiler unavailable: {exc}", file=sys.stderr)
            return 3
        try:
            result = compile_plan(plan, resolution=args.resolution)
        except CompileError as exc:
            print(f"svg_import: {exc.code}: {exc.message} ({exc.path})", file=sys.stderr)
            return 3
        if args.png:
            try:
                from PIL import Image

                from .compat import skill_api

                rgb = skill_api.render(
                    result["fbs"], resolution=args.resolution, supersample=2
                )
                directory = os.path.dirname(os.path.abspath(args.png))
                if directory:
                    os.makedirs(directory, exist_ok=True)
                Image.fromarray(rgb, mode="RGB").save(args.png, format="PNG")
            except Exception as exc:  # noqa: BLE001 - PIL/skill failures are IO/compile errors
                print(f"svg_import: PNG render failed: {exc}", file=sys.stderr)
                return 3
        if args.fdraw:
            try:
                from .fdraw import FDrawError, pack

                directory = os.path.dirname(os.path.abspath(args.fdraw))
                if directory:
                    os.makedirs(directory, exist_ok=True)
                pack(
                    result["fbs"],
                    source_fdsl=plan,
                    meta={"title": plan["name"]},
                    out=args.fdraw,
                )
            except (FDrawError, OSError) as exc:
                print(f"svg_import: .fdraw pack failed: {exc}", file=sys.stderr)
                return 3

    if not args.quiet:
        print(
            f"svg_import: {stats['canvas']['width']}x{stats['canvas']['height']} canvas, "
            f"{stats['elements']} elements ({stats['fills']} fill / {stats['strokes']} stroke), "
            f"{stats['points']} points, {stats['colors']} colours, "
            f"{len(warnings)} warning(s)",
            file=sys.stderr,
        )
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
