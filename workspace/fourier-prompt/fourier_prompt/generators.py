"""Deterministic contour generators for FDSL ``fourier_gen`` elements (WP-D).

Pure ``math`` only (no numpy, no I/O): every generator takes semantic canvas
pixel parameters and returns an explicit ``[[x, y], ...]`` point list.  The
compiler applies ``transform`` afterwards and decides whether the contour is a
filled region (closed) or a stroked polyline (open).

All generators are deterministic and unit-agnostic: coordinates are canvas
pixels (origin top-left, y down, matching FDSL).  Closed contours are returned
*without* a duplicated final point.

Unknown generator or unknown parameter is an error (``GenError``), never a
silent guess.
"""

from __future__ import annotations

import math

__all__ = ["GenError", "GEN_NAMES", "OPEN_GENS", "is_open", "contour", "param_names"]

TAU = 2.0 * math.pi
DEG = math.pi / 180.0

#: Names accepted by :func:`contour` (same set as ``schema.GEN_LIBRARY``).
GEN_NAMES = (
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

#: Generators that produce open curves by default (arc/wave/spiral).
OPEN_GENS = ("arc", "spiral", "wave")

#: Allowed parameter keys per generator (``at`` is the canvas anchor).
_PARAMS = {
    "circle": ("at", "r", "phase"),
    "ellipse": ("at", "rx", "ry", "rot"),
    "arc": ("at", "r", "from", "to", "rot"),
    "regular_polygon": ("at", "r", "n", "rot"),
    "star": ("at", "r", "ri", "n", "rot"),
    "rose": ("at", "R", "r", "k", "phi", "rot"),
    "spiral": ("at", "r0", "r", "turns", "growth", "phase", "ccw"),
    "wave": ("at", "w", "amp", "cycles", "phase"),
    "lissajous": ("at", "A", "B", "a", "b", "delta", "rot"),
    "superellipse": ("at", "rx", "ry", "n", "rot"),
}


class GenError(ValueError):
    """Invalid generator name/parameters (fail loud, no fallback geometry)."""

    def __init__(self, gen: str, message: str, *, param: str | None = None):
        self.gen = str(gen)
        self.param = param
        detail = f"generator {self.gen!r}"
        if param:
            detail += f" parameter {param!r}"
        super().__init__(f"{detail}: {message}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _num(gen: str, params: dict, key: str, default):
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GenError(gen, f"must be a finite number (got {value!r})", param=key)
    value = float(value)
    if not math.isfinite(value):
        raise GenError(gen, "must be finite", param=key)
    return value


def _point(gen: str, params: dict, key: str = "at", default=(0.0, 0.0)):
    value = params.get(key, default)
    if not (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    ):
        raise GenError(gen, "must be a finite [x, y] pair", param=key)
    x, y = float(value[0]), float(value[1])
    if not (math.isfinite(x) and math.isfinite(y)):
        raise GenError(gen, "must be finite", param=key)
    return x, y


def _positive(gen: str, params: dict, key: str, default, *, allow_zero=False):
    value = _num(gen, params, key, default)
    if value < 0.0 or (value == 0.0 and not allow_zero):
        raise GenError(gen, "must be > 0" if not allow_zero else "must be >= 0", param=key)
    return value


def _integer(gen: str, params: dict, key: str, default, *, minimum=1, maximum=4096):
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GenError(gen, f"must be an integer (got {value!r})", param=key)
    if isinstance(value, float) and not value.is_integer():
        raise GenError(gen, f"must be an integer (got {value!r})", param=key)
    ivalue = int(value)
    if not minimum <= ivalue <= maximum:
        raise GenError(gen, f"must be in [{minimum}, {maximum}] (got {ivalue})", param=key)
    return ivalue


def _samples(samples: int, *, minimum: int = 8) -> int:
    if isinstance(samples, bool) or not isinstance(samples, int):
        raise GenError("*", f"samples must be an integer (got {samples!r})")
    if not minimum <= samples <= 8192:
        raise GenError("*", f"samples must be in [{minimum}, 8192] (got {samples})")
    return samples


def _check_params(gen: str, params: dict) -> None:
    if not isinstance(params, dict):
        raise GenError(gen, f"params must be an object (got {type(params).__name__})")
    allowed = set(_PARAMS[gen])
    for key in params:
        if key not in allowed:
            raise GenError(gen, f"unknown parameter {key!r}; expected {sorted(allowed)}")


def param_names(gen: str) -> tuple[str, ...]:
    """Accepted parameter names for ``gen`` (raises for an unknown generator)."""
    if gen not in _PARAMS:
        raise GenError(str(gen), f"unknown generator; expected {list(GEN_NAMES)}")
    return tuple(_PARAMS[gen])


def is_open(gen: str) -> bool:
    """True when ``gen`` is an open curve by nature (arc/wave/spiral)."""
    if gen not in _PARAMS:
        raise GenError(str(gen), f"unknown generator; expected {list(GEN_NAMES)}")
    return gen in OPEN_GENS


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


def _circle(gen, params, samples):
    cx, cy = _point(gen, params)
    r = _positive(gen, params, "r", 100.0)
    phase = _num(gen, params, "phase", 0.0) * DEG
    return [
        [cx + r * math.cos(TAU * i / samples + phase), cy + r * math.sin(TAU * i / samples + phase)]
        for i in range(samples)
    ]


def _ellipse(gen, params, samples):
    cx, cy = _point(gen, params)
    rx = _positive(gen, params, "rx", 120.0)
    ry = _positive(gen, params, "ry", 80.0)
    rot = _num(gen, params, "rot", 0.0) * DEG
    cos_r, sin_r = math.cos(rot), math.sin(rot)
    points = []
    for i in range(samples):
        t = TAU * i / samples
        x, y = rx * math.cos(t), ry * math.sin(t)
        points.append([cx + x * cos_r - y * sin_r, cy + x * sin_r + y * cos_r])
    return points


def _arc(gen, params, samples):
    cx, cy = _point(gen, params)
    r = _positive(gen, params, "r", 100.0)
    a0 = _num(gen, params, "from", 0.0) * DEG
    a1 = _num(gen, params, "to", 180.0) * DEG
    rot = _num(gen, params, "rot", 0.0) * DEG
    a0 += rot
    a1 += rot
    if abs(a1 - a0) <= 1e-12:
        raise GenError(gen, "degenerate arc: from == to", param="to")
    return [
        [cx + r * math.cos(a0 + (a1 - a0) * i / samples), cy + r * math.sin(a0 + (a1 - a0) * i / samples)]
        for i in range(samples + 1)
    ]


def _regular_polygon(gen, params, samples):
    cx, cy = _point(gen, params)
    r = _positive(gen, params, "r", 200.0)
    n = _integer(gen, params, "n", 5, minimum=3, maximum=256)
    rot = _num(gen, params, "rot", 0.0) * DEG
    return [
        [cx + r * math.cos(rot + TAU * j / n), cy + r * math.sin(rot + TAU * j / n)]
        for j in range(n)
    ]


def _star(gen, params, samples):
    cx, cy = _point(gen, params)
    r = _positive(gen, params, "r", 200.0)
    ri = params.get("ri", None)
    ri = r * 0.45 if ri is None else _positive(gen, params, "ri", r * 0.45)
    if ri > r:
        raise GenError(gen, "ri must be <= r", param="ri")
    n = _integer(gen, params, "n", 5, minimum=3, maximum=256)
    rot = _num(gen, params, "rot", 0.0) * DEG
    points = []
    for j in range(2 * n):
        radius = r if j % 2 == 0 else ri
        angle = rot + TAU * j / (2 * n)
        points.append([cx + radius * math.cos(angle), cy + radius * math.sin(angle)])
    return points


def _rose(gen, params, samples):
    cx, cy = _point(gen, params)
    radius = _positive(gen, params, "R", 200.0) if "R" in params else _positive(gen, params, "r", 200.0)
    k = _num(gen, params, "k", 5.0)
    if abs(k) < 1e-9:
        raise GenError(gen, "k must be non-zero", param="k")
    phi = _num(gen, params, "phi", 0.0) * DEG
    rot = _num(gen, params, "rot", 0.0) * DEG
    points = []
    for i in range(samples):
        theta = TAU * i / samples
        rr = radius * math.cos(k * theta + phi)
        points.append([cx + rr * math.cos(theta + rot), cy + rr * math.sin(theta + rot)])
    return points


def _spiral(gen, params, samples):
    cx, cy = _point(gen, params)
    r0 = _positive(gen, params, "r0", 30.0) if "r0" in params else _positive(gen, params, "r", 30.0)
    turns = _num(gen, params, "turns", 2.0)
    growth = _positive(gen, params, "growth", 6.8535)
    if abs(turns) < 1e-9:
        raise GenError(gen, "turns must be non-zero", param="turns")
    phase = _num(gen, params, "phase", 0.0) * DEG
    ccw = params.get("ccw", True)
    if not isinstance(ccw, bool):
        raise GenError(gen, "must be a boolean", param="ccw")
    log_growth = math.log(growth)
    points = []
    for i in range(samples + 1):
        s = i / samples
        radius = r0 * math.exp(log_growth * s)
        angle = phase + (1.0 if ccw else -1.0) * TAU * turns * s
        points.append([cx + radius * math.cos(angle), cy + radius * math.sin(angle)])
    return points


def _wave(gen, params, samples):
    cx, cy = _point(gen, params)
    width = _positive(gen, params, "w", 400.0)
    amp = _num(gen, params, "amp", 40.0)
    cycles = _num(gen, params, "cycles", 2.0)
    phase = _num(gen, params, "phase", 0.0) * DEG
    return [
        [
            cx + width * i / samples,
            cy + amp * math.sin(TAU * cycles * i / samples + phase),
        ]
        for i in range(samples + 1)
    ]


def _lissajous(gen, params, samples):
    cx, cy = _point(gen, params)
    amp_a = _positive(gen, params, "A", 200.0)
    amp_b = _positive(gen, params, "B", 200.0)
    a = _integer(gen, params, "a", 3, minimum=1, maximum=256)
    b = _integer(gen, params, "b", 2, minimum=1, maximum=256)
    delta = _num(gen, params, "delta", 0.0) * DEG
    rot = _num(gen, params, "rot", 0.0) * DEG
    cos_r, sin_r = math.cos(rot), math.sin(rot)
    points = []
    for i in range(samples):
        t = TAU * i / samples
        x = amp_a * math.sin(a * t + delta)
        y = amp_b * math.sin(b * t)
        points.append([cx + x * cos_r - y * sin_r, cy + x * sin_r + y * cos_r])
    return points


def _superellipse(gen, params, samples):
    cx, cy = _point(gen, params)
    rx = _positive(gen, params, "rx", 140.0)
    ry = _positive(gen, params, "ry", 100.0)
    n = _num(gen, params, "n", 4.0)
    if n < 0.5:
        raise GenError(gen, "n must be >= 0.5", param="n")
    rot = _num(gen, params, "rot", 0.0) * DEG
    exponent = 2.0 / n
    cos_r, sin_r = math.cos(rot), math.sin(rot)
    points = []
    for i in range(samples):
        t = TAU * i / samples
        c, s = math.cos(t), math.sin(t)
        x = rx * math.copysign(abs(c) ** exponent, c)
        y = ry * math.copysign(abs(s) ** exponent, s)
        points.append([cx + x * cos_r - y * sin_r, cy + x * sin_r + y * cos_r])
    return points


_GENS = {
    "circle": _circle,
    "ellipse": _ellipse,
    "arc": _arc,
    "regular_polygon": _regular_polygon,
    "star": _star,
    "rose": _rose,
    "spiral": _spiral,
    "wave": _wave,
    "lissajous": _lissajous,
    "superellipse": _superellipse,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def contour(gen: str, params: dict | None = None, *, samples: int = 256) -> list[list[float]]:
    """Return an explicit contour for generator ``gen`` in canvas pixels.

    - closed generators return ``samples`` points (no duplicate end point);
    - open generators (``arc``/``wave``/``spiral``) return ``samples + 1``
      points;
    - ``regular_polygon``/``star`` return their exact vertices (sharp corners
      are not resampled).

    Raises :class:`GenError` for an unknown generator, unknown/invalid
    parameter or degenerate geometry.
    """
    name = str(gen)
    fn = _GENS.get(name)
    if fn is None:
        raise GenError(name, f"unknown generator; expected {list(GEN_NAMES)}")
    params = {} if params is None else params
    _check_params(name, params)
    count = _samples(samples)
    points = fn(name, params, count)
    if not points:
        raise GenError(name, "produced no points")
    return [[float(x), float(y)] for x, y in points]
