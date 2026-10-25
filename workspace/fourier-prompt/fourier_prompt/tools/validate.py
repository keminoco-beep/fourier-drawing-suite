"""``validate`` tool: FDSL plan / ``.fbs`` / ``.fdraw`` validation, three-way dispatch."""

from __future__ import annotations

import json

from .common import ToolError, jsonable, resolve_file

__all__ = ["validate", "TOOL_NAME", "DESCRIPTION", "INPUT_SCHEMA"]

TOOL_NAME = "validate"
DESCRIPTION = (
    "Validate an FDSL plan, a .fbs document or a .fdraw container. Returns "
    "{ok, kind, errors, warnings}; errors carry a code and a JSON path."
)
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "path to .fdraw, .fbs or an FDSL plan JSON"},
        "strict": {
            "type": "boolean",
            "description": "for plans: promote schema warnings to errors",
            "default": False,
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}


def _error(code: str, message: str, path: str = "$") -> dict:
    return {"code": code, "message": message, "path": path, "severity": "error"}


def _split_container_error(text: str) -> dict:
    code, _, message = str(text).partition(": ")
    if not message:
        code, message = "FD_E_INVALID", str(text)
    return _error(code, message, "$")


def _validate_plan(plan: dict, strict: bool) -> dict:
    from ..schema import validate_plan_detailed

    report = validate_plan_detailed(plan, strict=strict)
    return {
        "kind": "plan",
        "schema": plan.get("schema"),
        "valid": bool(report["ok"]),
        "errors": report["errors"],
        "warnings": report["warnings"],
        "checks": {
            "schema_version": str(plan.get("schema", "")).startswith("fdsl/"),
            "counts": {
                "elements": len(plan.get("elements") or []),
                "layers": len(plan.get("layers") or []),
                "texts": len(plan.get("texts") or []),
            },
        },
    }


def _validate_fbs(doc: dict, path: str) -> dict:
    from ..compat import skill_api

    api = skill_api.import_skill()
    try:
        errors = list(api.io_format.validate(doc) or [])
    except Exception as exc:  # noqa: BLE001 - malformed docs must report, not crash
        errors = [f"FB_E_INVALID: {type(exc).__name__}: {exc}"]
    return {
        "kind": "fbs",
        "schema": f"{doc.get('format')}/{doc.get('version')}",
        "valid": not errors,
        "errors": [_error("FB_E_INVALID", message) for message in errors],
        "warnings": [],
        "checks": {
            "format": doc.get("format") == "fourier-brushstroke",
            "version_major": str(doc.get("version", "0")).split(".")[0] == "2",
            "render_contract": isinstance(doc.get("render_contract"), dict),
        },
    }


def _validate_fdraw(path: str) -> dict:
    from .. import fdraw

    messages = list(fdraw.validate(path) or [])
    errors = [_split_container_error(message) for message in messages]
    try:
        info = fdraw.inspect(path)
    except fdraw.FDrawError:
        # damaged beyond inspection: the validate() errors above are the report
        checks = {}
    else:
        checks = {
            "spec_version": info.get("spec_version"),
            "root_sha256": info.get("root_sha256"),
            "root_sha256_ok": info.get("root_sha256_ok"),
            "entries_root_sha256_ok": info.get("entries_root_sha256_ok"),
            "manifest_sha256_ok": info.get("manifest_sha256_ok"),
            "capabilities": info.get("capabilities"),
            "font_count": info.get("font_count"),
            "has_source": info.get("has_source"),
        }
    return {
        "kind": "fdraw",
        "schema": (info.get("schema") if checks else None),
        "valid": not errors,
        "errors": errors,
        "warnings": [],
        "checks": checks,
    }


def validate(path: str, *, strict: bool = False) -> dict:
    """Validate ``path``; dispatches on file kind, returns errors/warnings."""
    if not isinstance(strict, bool):
        raise ToolError("TOOL_E_ARG", f"strict must be a boolean (got {strict!r})", {"strict": strict})
    resolved = resolve_file(path)
    suffix = resolved.suffix.lower()
    try:
        if suffix == ".fdraw":
            result = _validate_fdraw(str(resolved))
        elif suffix in (".fbs", ".fbs.json"):
            with open(resolved, "r", encoding="utf-8-sig") as handle:
                doc = json.load(handle)
            if not isinstance(doc, dict):
                raise ToolError(
                    "TOOL_E_LOAD", "a .fbs file must contain a JSON object", {"path": str(resolved)}
                )
            result = _validate_fbs(doc, str(resolved))
        elif suffix in (".json", ".fdsl"):
            with open(resolved, "r", encoding="utf-8-sig") as handle:
                plan = json.load(handle)
            if not isinstance(plan, dict):
                raise ToolError(
                    "TOOL_E_LOAD", "an FDSL plan must be a JSON object", {"path": str(resolved)}
                )
            result = _validate_plan(plan, strict)
        else:
            raise ToolError(
                "TOOL_E_INPUT",
                f"cannot validate {resolved.name!r}: expected .fdraw, .fbs or .json plan",
                {"path": str(resolved), "suffix": suffix},
            )
    except json.JSONDecodeError as exc:
        raise ToolError(
            "TOOL_E_LOAD", f"invalid JSON: {exc}", {"path": str(resolved)}
        ) from exc
    except OSError as exc:
        raise ToolError("TOOL_E_LOAD", f"cannot read input: {exc}", {"path": str(resolved)}) from exc

    from .common import sha256_file

    payload = {
        "ok": bool(result["valid"]),
        "tool": TOOL_NAME,
        "path": str(resolved),
        "file_bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
        "strict": strict,
    }
    payload.update(result)
    return jsonable(payload)
