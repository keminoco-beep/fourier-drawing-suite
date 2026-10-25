"""Fixtures for the fast-render backend tests (WP-F)."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parents[1]  # fourier-prompt/
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

SAMPLE_DIR = PKG_ROOT / "out" / "wp-d"
SAMPLES = {
    "rose": SAMPLE_DIR / "01_rose_medallion.fbs",
    "star": SAMPLE_DIR / "02_star_badge.fbs",
    "spiral": SAMPLE_DIR / "03_golden_spiral.fbs",
    "waves": SAMPLE_DIR / "04_ocean_waves.fbs",
    "aurora": SAMPLE_DIR / "05_aurora.fbs",
    "cat": SAMPLE_DIR / "06_cat_tophat.fbs",
}
SUPPORTED_SAMPLES = ("rose", "star", "spiral", "waves")
FALLBACK_SAMPLES = ("aurora", "cat")


def circle_stroke(
    stroke_id: int = 0,
    *,
    center=(0.5, 0.5),
    radius: float = 0.1,
    width: float = 0.02,
    layer: int = 0,
    closed: bool = True,
) -> dict:
    """One circular Fourier stroke (k=1), valid for both exact and fast backends."""
    import math

    return {
        "id": int(stroke_id),
        "closed": bool(closed),
        "closure": {"kind": "none"},
        "N": 1,
        "arc_length": float(2.0 * math.pi * radius),
        "layer": int(layer),
        "coef": [[0, float(center[0]), float(center[1])], [1, float(radius), 0.0]],
        "brush": {"style": 0, "width": {"interp": "constant", "keys": [[0.0, float(width)]]}},
    }


def synthetic_doc(
    *,
    shape: str = "round",
    width: float = 0.02,
    strokes: list[dict] | None = None,
    mode: str = "per_stroke",
    method: str = "synthetic",
    contract_width_scale: float | None = None,
    color=(30, 40, 60),
    base_color=(255, 255, 255),
) -> dict:
    """Minimal but render-valid ``.fbs`` v2 document."""
    if strokes is None:
        strokes = [circle_stroke(0, width=width)]
    contract = {"target_resolution": 256, "epsilon_px": 0.5}
    if contract_width_scale is not None:
        contract["width_scale"] = float(contract_width_scale)
    return {
        "format": "fbs",
        "version": 2,
        "mode": mode,
        "canvas": {
            "width": 1.0,
            "height": 1.0,
            "long_side_units": 1.0,
            "origin": "top-left",
            "y_axis": "down",
        },
        "render_contract": contract,
        "decomposition": {"method": method, "params": {"fill_soft_px": 0.5}},
        "base_color": [int(base_color[0]), int(base_color[1]), int(base_color[2]), 255],
        "brush_styles": [
            {
                "color": {
                    "interp": "constant",
                    "stops": [{"t": 0.0, "rgb": [int(color[0]), int(color[1]), int(color[2])], "a": 1.0}],
                },
                "alpha": {"interp": "constant", "keys": [[0.0, 1.0]]},
                "shape": {"interp": "step", "keys": [[0.0, str(shape)]]},
                "jitter": 0.0,
            }
        ],
        "strokes": strokes,
        "stats": {},
        "quality": {},
        "warnings": [],
        "accelerators": {},
    }


def sample_path(name: str) -> Path:
    return SAMPLES[name]
