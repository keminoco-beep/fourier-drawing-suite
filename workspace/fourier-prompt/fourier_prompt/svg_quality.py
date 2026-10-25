"""SVG quality gate for agent-authored complex drawings (WP-W).

The gate is deliberately *cheap and deterministic*: it parses the SVG with the
same importer used by the pipeline (:mod:`fourier_prompt.svg_import`), then
rasterises the resulting FDSL geometry on a coarse grid with Pillow to estimate
ink coverage / ink bounding box.  It never calls FFT compilation or the real
renderer, so it can run before every expensive conversion.

Public API
----------
``evaluate_svg(source, *, thresholds=None, raster=256, target_size=384)``
    -> report dict with ``ok``, ``score``, ``metrics``, ``checks``,
    ``errors`` and ``warnings``.

CLI::

    python -m fourier_prompt.svg_quality --svg X.svg --json
    python -m fourier_prompt.svg_quality --svg X.svg --strict
    python -m fourier_prompt.svg_quality --svg X.svg --config gate.json

Exit codes: 0 pass, 1 quality failure, 2 usage / unreadable / malformed input.

Metrics
-------
``paths``      drawable paths after fill/stroke splitting (importer ``paths_seen``)
``shapes``     source drawable shapes (path/rect/circle/ellipse/polygon/...)
``elements``   FDSL elements (may be trimmed by the 600-element budget)
``points``     total FDSL points
``colors``     palette entries after quantisation
``ink_ratio``  non-background pixel ratio (raster approximation)
``bbox_ratio`` ink bbox area / canvas area
``bbox``       ink bbox in canvas coordinates ``[x0, y0, x1, y1]``
``unsupported`` counts of gradient/text/image/filter/clip/mask/pattern/use...
``empty`` / ``degenerate`` / ``dropped_ratio``

The raster approximation ignores fill holes and alpha compositing between
shapes (painter's algorithm only); that is sufficient for the gate's purpose
(detect empty / tiny / over-solid drawings) and is documented, not silent.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, fields, replace

from .svg_import import SvgImportError, svg_to_fdsl

__all__ = [
    "DEFAULT_THRESHOLDS",
    "QualityThresholds",
    "evaluate_svg",
    "main",
    "score_report",
]

#: element names that the importer can draw (and that count towards "shapes")
_SHAPE_TAGS = {
    "path",
    "rect",
    "circle",
    "ellipse",
    "line",
    "polyline",
    "polygon",
}

#: tags that the importer degrades/skips — each occurrence is a quality risk
_UNSUPPORTED_TAGS = {
    "lineargradient": "gradient",
    "radialgradient": "gradient",
    "gradient": "gradient",
    "text": "text",
    "tspan": "text",
    "textpath": "text",
    "image": "image",
    "foreignobject": "foreignObject",
    "use": "use",
    "filter": "filter",
    "clippath": "clipPath",
    "mask": "mask",
    "pattern": "pattern",
    "marker": "marker",
}

#: attributes that reference unsupported features
_UNSUPPORTED_ATTRS = {
    "filter": "filter",
    "clip-path": "clipPath",
    "mask": "mask",
    "marker-start": "marker",
    "marker-mid": "marker",
    "marker-end": "marker",
}

#: containers whose children must not be counted as visible geometry
_NON_RENDERED = {"defs", "symbol", "clippath", "mask", "marker", "pattern"}

_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3,8})$")


@dataclass(frozen=True)
class QualityThresholds:
    """Gate thresholds.  All ratios are fractions in ``[0, 1]``."""

    min_paths: int = 10
    min_shapes: int = 10
    min_points: int = 150
    min_ink: float = 0.01
    max_ink: float = 0.92
    min_bbox: float = 0.30
    max_bbox: float = 1.0
    max_colors: int = 64
    max_unsupported: int = 2
    max_dropped_ratio: float = 0.35
    #: long side of the approximate raster used for ink / bbox
    raster: int = 256
    #: importer canvas long side (should match the production conversion)
    target_size: int = 384
    #: colour distance from the background above which a pixel counts as ink
    ink_distance: int = 24

    def __post_init__(self):
        if self.min_paths < 0 or self.min_shapes < 0 or self.min_points < 0:
            raise ValueError("minimum counts must be >= 0")
        if not 0.0 <= self.min_ink <= self.max_ink <= 1.0:
            raise ValueError("require 0 <= min_ink <= max_ink <= 1")
        if not 0.0 <= self.min_bbox <= self.max_bbox <= 1.0:
            raise ValueError("require 0 <= min_bbox <= max_bbox <= 1")
        if self.max_colors < 1:
            raise ValueError("max_colors must be >= 1")
        if self.max_unsupported < 0:
            raise ValueError("max_unsupported must be >= 0")
        if self.raster < 32:
            raise ValueError("raster must be >= 32")
        if self.target_size < 32:
            raise ValueError("target_size must be >= 32")

    def as_dict(self):
        return asdict(self)


DEFAULT_THRESHOLDS = QualityThresholds()


def _local(tag):
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _scan_xml(root):
    """Count shapes / unsupported features / top-level groups in an SVG tree."""
    counts = {
        "shapes": 0,
        "path": 0,
        "rect": 0,
        "circle": 0,
        "ellipse": 0,
        "line": 0,
        "polyline": 0,
        "polygon": 0,
        "groups": 0,
        "unsupported": {},
    }

    def walk(node, rendered=True):
        name = _local(node.tag)
        if not name:
            return
        if name in _SHAPE_TAGS and rendered:
            counts["shapes"] += 1
            counts[name] += 1
        if name == "g" and rendered:
            counts["groups"] += 1
        if name in _UNSUPPORTED_TAGS:
            kind = _UNSUPPORTED_TAGS[name]
            counts["unsupported"][kind] = counts["unsupported"].get(kind, 0) + 1
        for attr, kind in _UNSUPPORTED_ATTRS.items():
            if attr in node.attrib:
                counts["unsupported"][kind] = counts["unsupported"].get(kind, 0) + 1
        # Children of defs/symbol/clipPath/mask/marker/pattern are not drawn,
        # but unsupported features declared there still matter (e.g. gradients).
        child_rendered = rendered and name not in _NON_RENDERED
        for child in list(node):
            walk(child, child_rendered)

    walk(root)
    return counts


def _parse_color(name, palette):
    """Resolve an FDSL palette name (or literal) to ``(r, g, b, a)``."""
    if name is None:
        return None
    if isinstance(name, (list, tuple)) and len(name) >= 3:
        rgb = [max(0, min(255, int(round(float(v))))) for v in name[:3]]
        alpha = float(name[3]) if len(name) > 3 else 1.0
        return (rgb[0], rgb[1], rgb[2], alpha)
    text = str(name).strip()
    if text in palette:
        text = palette[text]
    match = _HEX_RE.match(text)
    if not match:
        return None
    body = match.group(1)
    if len(body) in (3, 4):
        body = "".join(ch * 2 for ch in body)
    if len(body) not in (6, 8):
        return None
    rgb = tuple(int(body[index : index + 2], 16) for index in (0, 2, 4))
    alpha = int(body[6:8], 16) / 255.0 if len(body) == 8 else 1.0
    return (rgb[0], rgb[1], rgb[2], alpha)


def _blend(color, background, extra_alpha=1.0):
    alpha = max(0.0, min(1.0, float(color[3]) * float(extra_alpha)))
    if alpha >= 0.999:
        return tuple(color[:3])
    return tuple(
        int(round(color[index] * alpha + background[index] * (1.0 - alpha)))
        for index in range(3)
    )


def _rasterize(plan, *, raster, ink_distance):
    """Approximate raster of the plan; returns (ink_mask, background, canvas, scale).

    Uses Pillow polygons/lines (no anti-aliasing, painter's algorithm).  Fill
    holes are ignored: they only affect ink in coloured fills, which the
    template forbids (line art uses white fills + ink strokes).
    """
    import numpy as np
    from PIL import Image, ImageDraw

    canvas = plan["canvas"]
    width = max(1.0, float(canvas["width"]))
    height = max(1.0, float(canvas["height"]))
    background_hex = str(canvas.get("background") or "#ffffff")
    background = _parse_color(background_hex, {})
    background = tuple(background[:3]) if background else (255, 255, 255)

    scale = float(raster) / max(width, height)
    grid_w = max(1, min(int(raster * 2), int(round(width * scale))))
    grid_h = max(1, min(int(raster * 2), int(round(height * scale))))
    image = Image.new("RGB", (grid_w, grid_h), background)
    draw = ImageDraw.Draw(image)

    palette = {
        str(key): str(value) for key, value in (plan.get("palette") or {}).items()
    }

    def project(points):
        return [(float(x) * scale, float(y) * scale) for x, y in points]

    for element in plan.get("elements") or []:
        opacity = float(element.get("opacity", 1.0) or 0.0)
        if opacity <= 0.0:
            continue
        if element.get("kind") == "fill":
            color = _parse_color(element.get("color"), palette)
            if color is None:
                continue
            fill = _blend(color, background, opacity)
            polygon = element.get("poly") or []
            if len(polygon) >= 3:
                draw.polygon(project(polygon), fill=fill)
            # Holes are intentionally not subtracted (documented approximation).
        else:
            brush = element.get("brush") or {}
            color = _parse_color(brush.get("color"), palette)
            points = element.get("points") or []
            if color is None or len(points) < 2:
                continue
            fill = _blend(color, background, opacity * float(brush.get("alpha", 1.0) or 1.0))
            line_width = max(1, int(round(float(brush.get("width", 1.0) or 1.0) * scale)))
            draw.line(project(points), fill=fill, width=line_width, joint="curve")

    array = np.asarray(image, dtype=np.int16)
    background_array = np.array(background, dtype=np.int16)
    distance = np.abs(array - background_array).max(axis=2)
    return distance >= int(ink_distance), background, (width, height), scale


def evaluate_svg(
    source,
    *,
    thresholds=None,
    raster=None,
    target_size=None,
    max_paths=2000,
):
    """Evaluate one SVG (path / text / bytes) and return a gate report.

    Parameters
    ----------
    thresholds:
        :class:`QualityThresholds` or a dict of overrides.
    raster, target_size:
        optional convenience overrides (``dataclasses.replace``).
    max_paths:
        importer path budget for the quality scan; defaults to the hard cap
        (2000) so the gate sees as much of the document as possible.
    """
    if thresholds is None:
        gate = DEFAULT_THRESHOLDS
    elif isinstance(thresholds, QualityThresholds):
        gate = thresholds
    elif isinstance(thresholds, dict):
        unknown = sorted(set(thresholds) - {field.name for field in fields(QualityThresholds)})
        if unknown:
            raise ValueError(f"unknown threshold(s): {', '.join(unknown)}")
        gate = replace(DEFAULT_THRESHOLDS, **thresholds)
    else:
        raise TypeError("thresholds must be QualityThresholds or dict")
    if raster is not None:
        gate = replace(gate, raster=int(raster))
    if target_size is not None:
        gate = replace(gate, target_size=int(target_size))

    report = {
        "ok": False,
        "score": 0.0,
        "svg": None,
        "thresholds": gate.as_dict(),
        "metrics": {},
        "checks": [],
        "errors": [],
        "warnings": [],
    }

    raw = None
    name = None
    if isinstance(source, bytes):
        raw = source
    elif isinstance(source, os.PathLike):
        name = os.fspath(source)
    elif isinstance(source, str):
        if "<" in source:
            raw = source.encode("utf-8")
        else:
            name = source
    else:
        raise TypeError("source must be an SVG path, text or bytes")

    if name is not None:
        report["svg"] = os.path.basename(name)
        try:
            with open(name, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            report["errors"].append(
                {"code": "SQ_E_IO", "message": f"cannot read SVG: {exc}"}
            )
            return report

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        report["errors"].append(
            {"code": "SQ_E_XML", "message": f"invalid XML: {exc}"}
        )
        return report

    scan = _scan_xml(root)
    unsupported = {key: value for key, value in sorted(scan["unsupported"].items()) if value}

    try:
        plan, svg_warnings, stats = svg_to_fdsl(
            raw,
            target_size=gate.target_size,
            max_paths=max(1, int(max_paths)),
            max_colors=64,
            max_total_points=200_000,
            on_overflow="trim",
        )
    except SvgImportError as exc:
        report["metrics"] = {
            "shapes": scan["shapes"],
            "paths": 0,
            "elements": 0,
            "points": 0,
            "colors": 0,
            "ink_ratio": 0.0,
            "bbox_ratio": 0.0,
            "bbox": None,
            "empty": exc.code == "SI_E_EMPTY",
            "degenerate": True,
            "dropped_ratio": 0.0,
            "unsupported": unsupported,
            "unsupported_total": sum(unsupported.values()),
            "import_error": exc.code,
        }
        report["errors"].append({"code": exc.code, "message": exc.message})
        report["checks"] = [
            {
                "id": "valid_import",
                "ok": False,
                "value": exc.code,
                "threshold": None,
                "message": f"svg_import failed: {exc.code}: {exc.message}",
            }
        ]
        return report

    ink_mask, _background, (canvas_w, canvas_h), scale = _rasterize(
        plan, raster=gate.raster, ink_distance=gate.ink_distance
    )
    import numpy as np

    ink_pixels = int(ink_mask.sum())
    ink_ratio = ink_pixels / float(ink_mask.size) if ink_mask.size else 0.0
    bbox = None
    bbox_ratio = 0.0
    if ink_pixels:
        rows = np.flatnonzero(ink_mask.any(axis=1))
        cols = np.flatnonzero(ink_mask.any(axis=0))
        y0, y1 = int(rows[0]), int(rows[-1]) + 1
        x0, x1 = int(cols[0]), int(cols[-1]) + 1
        bbox = [
            round(x0 / scale, 2),
            round(y0 / scale, 2),
            round(x1 / scale, 2),
            round(y1 / scale, 2),
        ]
        bbox_ratio = ((x1 - x0) * (y1 - y0)) / float(ink_mask.size)

    paths = int(stats.get("paths_seen") or 0)
    dropped = int(stats.get("paths_dropped") or 0)
    dropped_ratio = dropped / float(paths) if paths else 0.0
    points = int(stats.get("points") or 0)
    colors = int(stats.get("colors") or 0)
    empty = paths == 0 or int(stats.get("elements") or 0) == 0
    degenerate = empty or points == 0 or ink_ratio < 0.001 or bbox_ratio < 0.01

    metrics = {
        "shapes": int(scan["shapes"]),
        "paths": paths,
        "elements": int(stats.get("elements") or 0),
        "points": points,
        "colors": colors,
        "quantized_colors": int(stats.get("quantized_colors") or 0),
        "ink_ratio": round(ink_ratio, 5),
        "bbox_ratio": round(bbox_ratio, 5),
        "bbox": bbox,
        "canvas": {"width": int(canvas_w), "height": int(canvas_h)},
        "viewport": stats.get("source", {}).get("viewBox"),
        "paths_dropped": dropped,
        "dropped_ratio": round(dropped_ratio, 5),
        "warnings": int(stats.get("warnings") or 0),
        "warning_counts": stats.get("warning_counts") or {},
        "unsupported": unsupported,
        "unsupported_total": sum(unsupported.values()),
        "empty": empty,
        "degenerate": degenerate,
        "import_seconds": stats.get("seconds"),
    }

    checks = []

    def add(check_id, ok, value, threshold, message):
        checks.append(
            {
                "id": check_id,
                "ok": bool(ok),
                "value": value,
                "threshold": threshold,
                "message": message,
            }
        )

    add(
        "non_empty",
        not empty,
        paths,
        "> 0",
        "SVG has no drawable geometry" if empty else "drawable geometry present",
    )
    add(
        "min_paths",
        paths >= gate.min_paths,
        paths,
        f">= {gate.min_paths}",
        f"drawable paths {paths} < {gate.min_paths}" if paths < gate.min_paths else "ok",
    )
    add(
        "min_shapes",
        int(scan["shapes"]) >= gate.min_shapes,
        int(scan["shapes"]),
        f">= {gate.min_shapes}",
        (
            f"source shapes {scan['shapes']} < {gate.min_shapes}"
            if scan["shapes"] < gate.min_shapes
            else "ok"
        ),
    )
    add(
        "min_points",
        points >= gate.min_points,
        points,
        f">= {gate.min_points}",
        f"points {points} < {gate.min_points}" if points < gate.min_points else "ok",
    )
    add(
        "min_ink",
        ink_ratio >= gate.min_ink,
        round(ink_ratio, 5),
        f">= {gate.min_ink}",
        (
            f"ink {ink_ratio:.2%} < {gate.min_ink:.2%} (drawing too empty)"
            if ink_ratio < gate.min_ink
            else "ok"
        ),
    )
    add(
        "max_ink",
        ink_ratio <= gate.max_ink,
        round(ink_ratio, 5),
        f"<= {gate.max_ink}",
        (
            f"ink {ink_ratio:.2%} > {gate.max_ink:.2%} (solid silhouette / too dark)"
            if ink_ratio > gate.max_ink
            else "ok"
        ),
    )
    add(
        "min_bbox",
        bbox_ratio >= gate.min_bbox,
        round(bbox_ratio, 5),
        f">= {gate.min_bbox}",
        (
            f"ink bbox {bbox_ratio:.2%} of canvas < {gate.min_bbox:.2%} (too small/misplaced)"
            if bbox_ratio < gate.min_bbox
            else "ok"
        ),
    )
    add(
        "max_bbox",
        bbox_ratio <= gate.max_bbox,
        round(bbox_ratio, 5),
        f"<= {gate.max_bbox}",
        "ok" if bbox_ratio <= gate.max_bbox else "ink bbox exceeds canvas",
    )
    add(
        "max_colors",
        colors <= gate.max_colors,
        colors,
        f"<= {gate.max_colors}",
        f"colors {colors} > {gate.max_colors}" if colors > gate.max_colors else "ok",
    )
    unsupported_total = sum(unsupported.values())
    add(
        "max_unsupported",
        unsupported_total <= gate.max_unsupported,
        unsupported_total,
        f"<= {gate.max_unsupported}",
        (
            "unsupported features: "
            + ", ".join(f"{key}x{value}" for key, value in sorted(unsupported.items()))
            if unsupported_total > gate.max_unsupported
            else "ok"
        ),
    )
    add(
        "max_dropped",
        dropped_ratio <= gate.max_dropped_ratio,
        round(dropped_ratio, 5),
        f"<= {gate.max_dropped_ratio}",
        (
            f"{dropped} paths trimmed ({dropped_ratio:.0%}) > {gate.max_dropped_ratio:.0%}"
            if dropped_ratio > gate.max_dropped_ratio
            else ("ok" if not dropped else f"{dropped} paths trimmed within budget")
        ),
    )
    add(
        "non_degenerate",
        not degenerate,
        degenerate,
        "false",
        "degenerate drawing (no points / no ink)" if degenerate else "ok",
    )
    add(
        "valid_import",
        True,
        "ok",
        None,
        "svg_import succeeded",
    )

    report["checks"] = checks
    report["metrics"] = metrics
    report["ok"] = all(check["ok"] for check in checks)
    report["errors"] = [
        {"code": "SQ_E_" + check["id"].upper(), "message": check["message"]}
        for check in checks
        if not check["ok"]
    ]
    report["warnings"] = [
        {"code": item.get("code"), "message": item.get("message")}
        for item in svg_warnings
    ]
    report["score"] = score_report(report)
    return report


def score_report(report):
    """Deterministic 0–100 quality score for a report (higher is better)."""
    metrics = report.get("metrics") or {}
    if not metrics or not report.get("checks"):
        return 0.0
    paths = float(metrics.get("paths") or 0)
    points = float(metrics.get("points") or 0)
    ink = float(metrics.get("ink_ratio") or 0.0)
    bbox = float(metrics.get("bbox_ratio") or 0.0)
    unsupported = float(metrics.get("unsupported_total") or 0)
    dropped = float(metrics.get("dropped_ratio") or 0.0)
    warnings = float(metrics.get("warnings") or 0)

    structure = 25.0 * min(1.0, paths / 80.0)
    detail = 15.0 * min(1.0, points / 1600.0)
    if 0.02 <= ink <= 0.25:
        ink_score = 25.0
    elif ink < 0.02:
        ink_score = 25.0 * max(0.0, (ink - 0.005) / 0.015)
    else:
        ink_score = 25.0 * max(0.0, (0.95 - ink) / 0.70)
    bbox_score = 20.0 * min(1.0, bbox / 0.80)
    clean = 15.0 * max(
        0.0, 1.0 - 0.25 * unsupported - 0.5 * min(1.0, dropped) - 0.02 * warnings
    )
    score = structure + detail + ink_score + bbox_score + clean
    return round(max(0.0, min(100.0, score)), 1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m fourier_prompt.svg_quality",
        description="Quality gate for agent-authored SVG drawings (no rendering).",
    )
    parser.add_argument("--svg", required=True, help="input SVG path")
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    parser.add_argument("--config", help="JSON file with threshold overrides")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="template budget: >=30 shapes, >=400 points, no unsupported features",
    )
    parser.add_argument("--raster", type=int, default=None, help="ink raster long side")
    parser.add_argument("--min-paths", type=int)
    parser.add_argument("--min-shapes", type=int)
    parser.add_argument("--min-points", type=int)
    parser.add_argument("--min-ink", type=float)
    parser.add_argument("--max-ink", type=float)
    parser.add_argument("--min-bbox", type=float)
    parser.add_argument("--max-colors", type=int)
    parser.add_argument("--max-unsupported", type=int)
    parser.add_argument("--max-dropped-ratio", type=float)
    parser.add_argument("--quiet", action="store_true")
    return parser


def _load_overrides(args):
    overrides = {}
    if args.config:
        try:
            with open(args.config, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"svg_quality: cannot read --config: {exc}")
        if not isinstance(loaded, dict):
            raise SystemExit("svg_quality: --config must be a JSON object")
        overrides.update(loaded)
    mapping = {
        "min_paths": args.min_paths,
        "min_shapes": args.min_shapes,
        "min_points": args.min_points,
        "min_ink": args.min_ink,
        "max_ink": args.max_ink,
        "min_bbox": args.min_bbox,
        "max_colors": args.max_colors,
        "max_unsupported": args.max_unsupported,
        "max_dropped_ratio": args.max_dropped_ratio,
    }
    overrides.update({key: value for key, value in mapping.items() if value is not None})
    if args.raster is not None:
        overrides["raster"] = args.raster
    if args.strict:
        overrides.setdefault("min_shapes", 30)
        overrides.setdefault("min_points", 400)
        overrides.setdefault("max_unsupported", 0)
        overrides.setdefault("min_paths", 30)
    return overrides


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    overrides = _load_overrides(args)
    try:
        thresholds = replace(DEFAULT_THRESHOLDS, **overrides) if overrides else DEFAULT_THRESHOLDS
    except (TypeError, ValueError) as exc:
        print(f"svg_quality: invalid thresholds: {exc}", file=sys.stderr)
        return 2

    try:
        report = evaluate_svg(args.svg, thresholds=thresholds)
    except (OSError, ValueError) as exc:
        print(f"svg_quality: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif not args.quiet:
        metrics = report.get("metrics") or {}
        print(f"svg:    {report.get('svg')}")
        print(
            "metrics: paths={paths} shapes={shapes} points={points} colors={colors} "
            "ink={ink_ratio} bbox={bbox_ratio} dropped={dropped_ratio}".format(
                paths=metrics.get("paths", "-"),
                shapes=metrics.get("shapes", "-"),
                points=metrics.get("points", "-"),
                colors=metrics.get("colors", "-"),
                ink_ratio=metrics.get("ink_ratio", "-"),
                bbox_ratio=metrics.get("bbox_ratio", "-"),
                dropped_ratio=metrics.get("dropped_ratio", "-"),
            )
        )
        print(f"score:  {report.get('score')}  result: {'PASS' if report.get('ok') else 'FAIL'}")
        for check in report.get("checks") or []:
            if not check["ok"]:
                print(f"  FAIL {check['id']}: {check['message']}")
        for error in report.get("errors") or []:
            if error["code"].startswith("SQ_E_") and not any(
                error["message"] == check["message"]
                for check in report.get("checks") or []
                if not check["ok"]
            ):
                print(f"  {error['code']}: {error['message']}")

    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
