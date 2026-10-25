"""Fixtures for the progressive-video tests (WP-J)."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parents[1]  # fourier-prompt/
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

VERIFY_DIR = PKG_ROOT / "out" / "verify"
ROSE_FDRAW = VERIFY_DIR / "01.fdraw"  # 6 layers / 51 elements, fast-supported


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
        "fill_role": "medial_branch",
        "N": 1,
        "arc_length": float(2.0 * math.pi * radius),
        "layer": int(layer),
        "coef": [[0, float(center[0]), float(center[1])], [1, float(radius), 0.0]],
        "brush": {"style": 0, "width": {"interp": "constant", "keys": [[0.0, float(width)]]}},
    }


def synthetic_doc(
    *, strokes: list[dict] | None = None, color=(30, 40, 60), shape: str = "round"
) -> dict:
    """Minimal render-valid ``.fbs`` v2 document (one brush style)."""
    if strokes is None:
        strokes = [circle_stroke(0)]
    return {
        "format": "fourier-brushstroke",
        "version": 2,
        "mode": "per_stroke",
        "canvas": {
            "width": 1.0,
            "height": 1.0,
            "long_side_units": 1.0,
            "origin": "top-left",
            "y_axis": "down",
        },
        "render_contract": {"target_resolution": 256, "epsilon_px": 0.5},
        "decomposition": {"method": "synthetic", "params": {"fill_soft_px": 0.5}},
        "base_color": [255, 255, 255, 255],
        "fingerprint": {"params_sha256": "0" * 64},
        "brush_styles": [
            {
                "color": {
                    "interp": "constant",
                    "stops": [
                        {"t": 0.0, "rgb": [int(color[0]), int(color[1]), int(color[2])], "a": 1.0}
                    ],
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


def multi_doc(count: int = 6) -> dict:
    """A square canvas with ``count`` separated circles across two layers."""
    strokes = []
    for index in range(count):
        column = index % 3
        row = index // 3
        strokes.append(
            circle_stroke(
                index,
                center=(0.2 + 0.3 * column, 0.25 + 0.5 * row),
                radius=0.08,
                layer=index % 2,
            )
        )
    return synthetic_doc(strokes=strokes)


def decode_frames(path) -> dict:
    """Decode a video with imageio_ffmpeg: frame count, size, first-frame length."""
    import imageio_ffmpeg

    generator = imageio_ffmpeg.read_frames(str(path))
    meta = dict(next(generator))
    frames = 0
    first_len = None
    try:
        for frame in generator:
            if isinstance(frame, (bytes, bytearray)):
                frames += 1
                if first_len is None:
                    first_len = len(frame)
    finally:
        try:
            generator.close()
        except Exception:  # noqa: BLE001 - best effort
            pass
    width, height = int(meta["size"][0]), int(meta["size"][1])
    return {
        "frames": frames,
        "size": [width, height],
        "fps": float(meta.get("fps") or 0.0),
        "duration_s": float(meta.get("duration") or 0.0),
        "first_frame_bytes": first_len,
        "expected_frame_bytes": width * height * 3,
    }
