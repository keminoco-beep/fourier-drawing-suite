"""``render`` tool: ``.fdraw`` / ``.fbs`` / plan -> PNG (fast backend, exact fallback)."""

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
    write_png,
)

__all__ = ["render", "TOOL_NAME", "DESCRIPTION", "INPUT_SCHEMA"]

TOOL_NAME = "render"
DESCRIPTION = (
    "Render an .fdraw container, a .fbs document or an FDSL plan to a PNG. Uses "
    "render_fast.render_auto when available and falls back to the skill's exact "
    "renderer (reported as fallback). Returns out path, size, elapsed time, "
    "quality and backend."
)
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "input .fdraw / .fbs / FDSL plan path"},
        "out": {"type": "string", "description": "output PNG path"},
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
            "description": "fast (default) or exact (skill renderer)",
            "default": "fast",
        },
        "supersample": {
            "type": "integer",
            "minimum": 1,
            "maximum": 4,
            "description": "exact-renderer supersampling factor (default 2)",
            "default": 2,
        },
        "include_image": {
            "type": "boolean",
            "description": "also return the PNG as base64 (long side <= 768) for MCP image content",
            "default": False,
        },
    },
    "required": ["path", "out"],
    "additionalProperties": False,
}


def render(
    path: str,
    out: str,
    resolution: int = 512,
    quality: str = "fast",
    supersample: int = 2,
    include_image: bool = False,
) -> dict:
    """Render ``path`` to ``out`` (PNG); the input file is never modified."""
    check_int(resolution, name="resolution", minimum=16, maximum=1024)
    check_int(supersample, name="supersample", minimum=1, maximum=4)
    if not isinstance(include_image, bool):
        raise ToolError(
            "TOOL_E_ARG",
            f"include_image must be a boolean (got {include_image!r})",
            {"include_image": include_image},
        )
    target = ensure_out_path(out, input_path=path)

    envelope = load_document(path)
    result = render_document(
        envelope["doc"], resolution=resolution, quality=quality, supersample=supersample
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
    }
    if envelope["kind"] == "plan":
        payload["compiled"] = {
            "stats": envelope.get("compile_stats"),
            "warnings": len(envelope.get("compile_warnings") or []),
        }
    if include_image:
        payload["image"] = encode_png_base64(rgb)
    return jsonable(payload)
