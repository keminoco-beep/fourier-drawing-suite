"""``preview`` tool: render only the first ``fraction`` of the elements."""

from __future__ import annotations

import math

from .common import (
    ToolError,
    check_fraction,
    check_int,
    encode_png_base64,
    ensure_out_path,
    jsonable,
    load_document,
    render_document,
    sha256_bytes,
    slice_document,
    write_png,
)

__all__ = ["preview", "TOOL_NAME", "DESCRIPTION", "INPUT_SCHEMA"]

TOOL_NAME = "preview"
DESCRIPTION = (
    "Render only the first `fraction` of the elements/strokes to a PNG (a "
    "truncated in-memory copy; the input file is untouched). Useful for "
    "'show me the first 30% of the drawing' previews."
)
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "input .fdraw / .fbs / FDSL plan path"},
        "out": {"type": "string", "description": "output PNG path"},
        "fraction": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": 1,
            "description": "fraction of elements to keep (0 < fraction <= 1)",
            "default": 0.3,
        },
        "resolution": {
            "type": "integer",
            "minimum": 16,
            "maximum": 1024,
            "description": "long-side pixels (default 512)",
            "default": 512,
        },
        "quality": {
            "type": "string",
            "enum": ["fast", "exact"],
            "default": "fast",
            "description": "render backend preference",
        },
        "include_image": {
            "type": "boolean",
            "description": "also return the PNG as base64 (long side <= 768)",
            "default": False,
        },
    },
    "required": ["path", "out"],
    "additionalProperties": False,
}


def preview(
    path: str,
    out: str,
    fraction: float = 0.3,
    resolution: int = 512,
    quality: str = "fast",
    include_image: bool = False,
) -> dict:
    """Render the element prefix covering ``fraction`` of the document."""
    ratio = check_fraction(fraction)
    check_int(resolution, name="resolution", minimum=16, maximum=1024)
    if not isinstance(include_image, bool):
        raise ToolError(
            "TOOL_E_ARG",
            f"include_image must be a boolean (got {include_image!r})",
            {"include_image": include_image},
        )
    target = ensure_out_path(out, input_path=path)

    envelope = load_document(path)
    doc = envelope["doc"]
    strokes = doc.get("strokes")
    if not isinstance(strokes, list) or not strokes:
        raise ToolError(
            "TOOL_E_INPUT",
            "document has no strokes to preview",
            {"path": envelope["path"]},
        )
    total = len(strokes)
    # ceil keeps at least one element for tiny fractions and never exceeds total
    keep = min(total, max(1, int(math.ceil(total * ratio))))
    truncated, kept, total = slice_document(doc, keep)

    result = render_document(
        truncated, resolution=resolution, quality=quality, supersample=2
    )
    rgb = result["rgb"]
    written = write_png(rgb, target)

    payload = {
        "ok": True,
        "tool": TOOL_NAME,
        "input": {
            "path": envelope["path"],
            "kind": envelope["kind"],
            "sha256": envelope["sha256"],
        },
        "out": written["out"],
        "width": result["width"],
        "height": result["height"],
        "resolution": resolution,
        "quality": quality,
        "backend": result["backend"],
        "fallback": result["fallback"],
        "fallback_reason": result["fallback_reason"],
        "fast_fallback_reason": result["fast_fallback_reason"],
        "fast_approximations": result["fast_approximations"],
        "elapsed_ms": result["elapsed_ms"],
        "png_bytes": written["png_bytes"],
        "pixel_sha256": sha256_bytes(rgb.tobytes()),
        "fraction": ratio,
        "kept": kept,
        "total": total,
        "percent": round(100.0 * kept / total, 3) if total else 0.0,
        "truncated": kept < total,
        "full_preview": kept == total,
        "preview_doc": {
            "strokes_kept": kept,
            "texts_dropped": kept < total and bool(doc.get("texts")),
        },
    }
    if include_image:
        payload["image"] = encode_png_base64(rgb)
    return jsonable(payload)
