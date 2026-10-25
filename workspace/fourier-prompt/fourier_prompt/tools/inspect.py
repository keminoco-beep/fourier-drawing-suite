"""``inspect`` tool: summarize an ``.fdraw`` / ``.fbs`` / FDSL plan without rendering."""

from __future__ import annotations

from .common import ToolError, jsonable, load_input, summarize_document

__all__ = ["inspect", "TOOL_NAME", "DESCRIPTION", "INPUT_SCHEMA"]

TOOL_NAME = "inspect"
DESCRIPTION = (
    "Summarize an .fdraw container, a .fbs document or an FDSL plan: version, "
    "canvas, element/stroke/fill/text/layer counts, fonts, root_sha256, sizes "
    "and validation status."
)
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "path to .fdraw, .fbs or an FDSL plan JSON"}
    },
    "required": ["path"],
    "additionalProperties": False,
}


def _plan_summary(plan: dict) -> dict:
    from ..schema import validate_plan_detailed

    report = validate_plan_detailed(plan)
    canvas = plan.get("canvas") or {}
    palette = plan.get("palette") or {}
    layers = plan.get("layers") if isinstance(plan.get("layers"), list) else []
    elements = plan.get("elements") if isinstance(plan.get("elements"), list) else []
    texts = plan.get("texts") if isinstance(plan.get("texts"), list) else []
    fonts = plan.get("fonts") if isinstance(plan.get("fonts"), list) else []
    kinds: dict[str, int] = {}
    for element in elements:
        kind = str((element or {}).get("kind")) if isinstance(element, dict) else "invalid"
        kinds[kind] = kinds.get(kind, 0) + 1
    return jsonable(
        {
            "schema": plan.get("schema"),
            "name": plan.get("name"),
            "canvas": {
                "width": canvas.get("width"),
                "height": canvas.get("height"),
                "background": canvas.get("background"),
            },
            "counts": {
                "elements": len(elements),
                "layers": len(layers),
                "texts": len(texts),
                "palette": len(palette),
                "fonts": len(fonts),
            },
            "element_kinds": kinds,
            "layers": [
                {"id": layer.get("id"), "name": layer.get("name"), "visible": layer.get("visible", True)}
                for layer in layers
                if isinstance(layer, dict)
            ],
            "constraints": plan.get("constraints"),
            "valid": bool(report["ok"]),
            "errors": report["errors"],
            "warnings": report["warnings"],
            "font_support": {"declared": len(fonts), "renderable": False},
        }
    )


def inspect(path: str) -> dict:
    """Return a JSON-serializable summary of ``path`` (never renders)."""
    envelope = load_input(path)
    kind = envelope["kind"]
    base = {
        "ok": True,
        "tool": TOOL_NAME,
        "kind": kind,
        "path": envelope["path"],
        "file_bytes": envelope["file_bytes"],
        "sha256": envelope["sha256"],
    }
    if kind == "plan":
        base["plan"] = _plan_summary(envelope["doc"])
        base["document"] = None
        return jsonable(base)

    document = summarize_document(envelope["doc"], kind=kind)
    base["document"] = document
    if kind == "fdraw":
        from .. import fdraw

        info = fdraw.inspect(envelope["path"])
        base["container"] = {
            "schema": info.get("schema"),
            "spec_version": info.get("spec_version"),
            "zip_bytes": info.get("zip_bytes"),
            "uncompressed_bytes": info.get("uncompressed_bytes"),
            "entry_count": info.get("entry_count"),
            "entries": info.get("entries"),
            "root_sha256": info.get("root_sha256"),
            "root_sha256_recorded": info.get("root_sha256_recorded"),
            "root_sha256_ok": info.get("root_sha256_ok"),
            "entries_root_sha256_ok": info.get("entries_root_sha256_ok"),
            "manifest_sha256_ok": info.get("manifest_sha256_ok"),
            "capabilities": info.get("capabilities"),
            "has_source": info.get("has_source"),
            "source_schema": info.get("source_schema"),
            "fonts": info.get("fonts"),
            "font_count": info.get("font_count"),
            "profiles": info.get("profiles"),
            "render": info.get("render"),
        }
        base["valid"] = bool(info.get("root_sha256_ok"))
        base["texts_supported"] = False
    else:
        base["container"] = None
        base["valid"] = True
        base["texts_supported"] = False
    return jsonable(base)
