"""``frame`` tool: freeze the drawing after the first ``at`` elements."""

from __future__ import annotations

from .common import (
    ToolError,
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

__all__ = ["frame", "TOOL_NAME", "DESCRIPTION", "INPUT_SCHEMA"]

TOOL_NAME = "frame"
DESCRIPTION = (
    "Freeze a frame after the first `at` elements/strokes and render it to PNG. "
    "The document is truncated in memory only; the input file is untouched."
)
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "input .fdraw / .fbs / FDSL plan path"},
        "out": {"type": "string", "description": "output PNG path"},
        "at": {
            "type": "integer",
            "minimum": 1,
            "description": "number of leading elements to draw (default 20)",
            "default": 20,
        },
        "resolution": {
            "type": "integer",
            "minimum": 16,
            "maximum": 1024,
            "default": 512,
            "description": "long-side pixels",
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


def frame(
    path: str,
    out: str,
    at: int = 20,
    resolution: int = 512,
    quality: str = "fast",
    include_image: bool = False,
) -> dict:
    """Render the first ``at`` elements of ``path`` to ``out``."""
    check_int(at, name="at", minimum=1, maximum=1_000_000)
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
            "TOOL_E_INPUT", "document has no strokes to freeze", {"path": envelope["path"]}
        )
    total = len(strokes)
    truncated, kept, total = slice_document(doc, at)

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
        "at": at,
        "kept": kept,
        "total": total,
        "truncated": kept < total,
        "clamped": at > total,
        "note": "at exceeds the element count; the full drawing was rendered"
        if at > total
        else None,
    }
    if include_image:
        payload["image"] = encode_png_base64(rgb)
    return jsonable(payload)
