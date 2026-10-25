"""``list_brushes`` tool: the 20 active presets (+11 planned, not renderable)."""

from __future__ import annotations

from .common import ToolError, jsonable, load_brush_catalog

__all__ = ["list_brushes", "TOOL_NAME", "DESCRIPTION", "INPUT_SCHEMA"]

TOOL_NAME = "list_brushes"
DESCRIPTION = (
    "List brush presets from assets/brush/brush_catalog.json: id/name/category/"
    "shape/status/renderable/roles/width_eff. 20 active presets are renderable; "
    "11 planned presets are reported with renderable=false."
)
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["active", "planned", "all"],
            "description": "filter by preset status (default: all)",
            "default": "all",
        },
        "role": {
            "type": "string",
            "description": "only presets whose roles include this role (e.g. 'bed', 'line_taper')",
        },
        "category": {
            "type": "string",
            "description": "only presets in this category (e.g. 'basic', 'ink', 'watercolor')",
        },
    },
    "additionalProperties": False,
}

_FIELDS = (
    "id",
    "name",
    "name_en",
    "name_zh",
    "category",
    "shape",
    "status",
    "renderable",
    "roles",
    "width_eff",
    "width_eff_source",
    "width_range_px",
    "sizes_px",
    "default_opacity",
    "traits",
    "preview",
)


def _pick(preset: dict) -> dict:
    item = {key: preset.get(key) for key in _FIELDS if key in preset}
    item["id"] = preset.get("id")
    item["status"] = preset.get("status")
    item["renderable"] = bool(preset.get("renderable"))
    if not item["renderable"]:
        item["unrenderable_reason"] = "planned preset: no skill render implementation"
    return item


def list_brushes(
    *, status: str = "all", role: str | None = None, category: str | None = None
) -> dict:
    """Return catalog presets, filtered optionally by status/role/category."""
    if status not in ("active", "planned", "all"):
        raise ToolError(
            "TOOL_E_ARG",
            f"status must be 'active', 'planned' or 'all' (got {status!r})",
            {"status": status},
        )
    for name, value in (("role", role), ("category", category)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ToolError(
                "TOOL_E_ARG", f"{name} filter must be a non-empty string (got {value!r})", {name: value}
            )

    catalog = load_brush_catalog()
    presets = catalog["presets"]
    counts = catalog.get("counts") or {}
    roles_available = sorted(
        {str(item) for preset in presets.values() for item in (preset.get("roles") or [])}
    )
    categories_available = sorted(
        {str(preset.get("category")) for preset in presets.values() if preset.get("category")}
    )

    selected = []
    for preset in presets.values():
        if not isinstance(preset, dict):
            continue
        preset_status = preset.get("status", "active")
        if status != "all" and preset_status != status:
            continue
        if role is not None and role not in (preset.get("roles") or []):
            continue
        if category is not None and preset.get("category") != category:
            continue
        selected.append(_pick(preset))
    selected.sort(key=lambda item: (item.get("status") != "active", item.get("id") or ""))

    active_count = sum(1 for item in selected if item["status"] == "active")
    planned_count = len(selected) - active_count
    return jsonable(
        {
            "ok": True,
            "tool": TOOL_NAME,
            "catalog": {
                "path": catalog.get("_path"),
                "version": catalog.get("catalog_version") or catalog.get("schema"),
            },
            "counts": {
                "total_catalog": int(counts.get("total", len(presets))),
                "catalog_active": int(counts.get("active", 0)) or None,
                "catalog_planned": int(counts.get("planned", 0)) or None,
                "returned": len(selected),
                "active": active_count,
                "planned": planned_count,
            },
            "filters": {"status": status, "role": role, "category": category},
            "roles_available": roles_available,
            "categories_available": categories_available,
            "brushes": selected,
        }
    )
