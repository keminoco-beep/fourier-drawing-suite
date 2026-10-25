"""``compare`` tool: image metrics + parity gate between two PNGs."""

from __future__ import annotations

import math

from .common import ToolError, jsonable, png_to_rgb, resolve_file, sha256_file

__all__ = ["compare", "TOOL_NAME", "DESCRIPTION", "INPUT_SCHEMA"]

TOOL_NAME = "compare"
DESCRIPTION = (
    "Compare two PNGs: mean/max absolute difference, RMSE, PSNR, within-1 and "
    "within-2 percentages and the parity gate (max|delta| <= 2 and PSNR >= 42 dB). "
    "An optional pixel region restricts the comparison."
)
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "a": {"type": "string", "description": "first PNG path"},
        "b": {"type": "string", "description": "second PNG path"},
        "region": {
            "type": "object",
            "description": "optional pixel region {x, y, width, height}",
            "properties": {
                "x": {"type": "integer", "minimum": 0},
                "y": {"type": "integer", "minimum": 0},
                "width": {"type": "integer", "minimum": 1},
                "height": {"type": "integer", "minimum": 1},
            },
            "required": ["x", "y", "width", "height"],
            "additionalProperties": False,
        },
    },
    "required": ["a", "b"],
    "additionalProperties": False,
}

PARITY_MAX_ABS_DIFF = 2
PARITY_PSNR_DB = 42.0


def _parse_region(region, height: int, width: int):
    if region is None:
        return None
    if isinstance(region, dict):
        keys = ("x", "y", "width", "height")
        missing = [key for key in keys if key not in region]
        if missing:
            raise ToolError(
                "TOOL_E_ARG",
                f"region is missing {missing}",
                {"region": region},
            )
        values = [region[key] for key in keys]
    elif isinstance(region, (list, tuple)):
        if len(region) != 4:
            raise ToolError(
                "TOOL_E_ARG", "region must be [x, y, width, height]", {"region": list(region)}
            )
        values = list(region)
    else:
        raise ToolError(
            "TOOL_E_ARG",
            f"region must be an object or [x, y, width, height] (got {type(region).__name__})",
            {"region": region},
        )
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(
                "TOOL_E_ARG", f"region values must be integers (got {values})", {"region": values}
            )
    x, y, w, h = values
    if x < 0 or y < 0 or w < 1 or h < 1:
        raise ToolError(
            "TOOL_E_ARG", f"region out of range: {values}", {"region": values}
        )
    if x + w > width or y + h > height:
        raise ToolError(
            "TOOL_E_ARG",
            f"region {values} exceeds image size {width}x{height}",
            {"region": values, "image": {"width": width, "height": height}},
        )
    return {"x": x, "y": y, "width": w, "height": h}


def _local_metrics(diff) -> dict:
    import numpy as np

    absolute = np.abs(diff.astype(np.int32))
    max_abs = int(absolute.max()) if absolute.size else 0
    mean_abs = float(absolute.mean()) if absolute.size else 0.0
    rmse = float(math.sqrt(float(np.mean(np.square(diff.astype(np.float64)))))) if diff.size else 0.0
    psnr = None if rmse == 0.0 else float(10.0 * math.log10((255.0 * 255.0) / (rmse * rmse)))
    return {
        "max_abs_diff": max_abs,
        "mean_abs_diff": mean_abs,
        "rmse": rmse,
        "psnr_db": psnr,
        "within1_pct": float(100.0 * np.mean(absolute <= 1)) if diff.size else 100.0,
        "within2_pct": float(100.0 * np.mean(absolute <= 2)) if diff.size else 100.0,
        "identical": bool(np.all(absolute == 0)),
    }


def compare(a: str, b: str, region=None) -> dict:
    """Return metrics and the parity verdict for two PNGs."""
    path_a = resolve_file(a, what="image a")
    path_b = resolve_file(b, what="image b")
    rgb_a = png_to_rgb(path_a)
    rgb_b = png_to_rgb(path_b)
    if rgb_a.shape != rgb_b.shape:
        raise ToolError(
            "TOOL_E_COMPARE",
            f"image sizes differ: {rgb_a.shape[1]}x{rgb_a.shape[0]} vs "
            f"{rgb_b.shape[1]}x{rgb_b.shape[0]}",
            {
                "a": {"path": str(path_a), "shape": list(rgb_a.shape)},
                "b": {"path": str(path_b), "shape": list(rgb_b.shape)},
            },
        )
    height, width = rgb_a.shape[:2]
    parsed_region = _parse_region(region, height, width)
    if parsed_region is not None:
        x, y, w, h = (
            parsed_region["x"],
            parsed_region["y"],
            parsed_region["width"],
            parsed_region["height"],
        )
        crop_a = rgb_a[y : y + h, x : x + w]
        crop_b = rgb_b[y : y + h, x : x + w]
    else:
        crop_a, crop_b = rgb_a, rgb_b

    diff = crop_a.astype("int16") - crop_b.astype("int16")
    metrics, backend = _fast_or_local_metrics(crop_a, crop_b, diff)
    psnr = metrics.get("psnr_db")
    parity_pass = bool(
        metrics["max_abs_diff"] <= PARITY_MAX_ABS_DIFF
        and (psnr is None or float(psnr) >= PARITY_PSNR_DB)
        and (psnr is not None or metrics["max_abs_diff"] == 0)
    )
    return jsonable(
        {
            "ok": True,
            "tool": TOOL_NAME,
            "a": {
                "path": str(path_a),
                "sha256": sha256_file(path_a),
                "width": int(width),
                "height": int(height),
            },
            "b": {
                "path": str(path_b),
                "sha256": sha256_file(path_b),
                "width": int(width),
                "height": int(height),
            },
            "region": parsed_region,
            "shape": [int(height), int(width)],
            "metrics": metrics,
            "parity_pass": parity_pass,
            "thresholds": {
                "max_abs_diff": PARITY_MAX_ABS_DIFF,
                "psnr_db": PARITY_PSNR_DB,
            },
            "backend": backend,
        }
    )


def _fast_or_local_metrics(crop_a, crop_b, diff) -> tuple[dict, str]:
    """Use ``render_fast.compare_images`` when it is importable (and sane)."""
    try:
        from ..render_fast import compare_images
    except Exception:  # noqa: BLE001 - WP-F optional
        return _local_metrics(diff), "local"
    if not callable(compare_images):
        return _local_metrics(diff), "local"
    try:
        result = compare_images(crop_a, crop_b)
    except Exception:  # noqa: BLE001 - never let an optional backend break compare
        return _local_metrics(diff), "local"
    required = ("max_abs_diff", "mean_abs_diff", "psnr_db", "within1_pct", "within2_pct")
    if not isinstance(result, dict) or any(key not in result for key in required):
        return _local_metrics(diff), "local"
    local = _local_metrics(diff)
    metrics = {
        "max_abs_diff": int(result["max_abs_diff"]),
        "mean_abs_diff": float(result["mean_abs_diff"]),
        "rmse": float(result.get("rmse", local["rmse"])),
        "psnr_db": None if result["psnr_db"] is None else float(result["psnr_db"]),
        "within1_pct": float(result["within1_pct"]),
        "within2_pct": float(result["within2_pct"]),
        "identical": bool(result.get("identical", local["identical"])),
    }
    return metrics, "render_fast"
