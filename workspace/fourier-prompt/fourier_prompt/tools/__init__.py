"""Agent tool surface for fourier-prompt (WP-H).

Eight JSON-serializable tools: ``status``, ``list_brushes``, ``inspect``,
``render``, ``preview``, ``frame``, ``compare``, ``validate``.  They only
compose ``schema`` / ``compile`` / ``fdraw`` / ``compat.skill_api`` (plus WP-F
``render_fast`` when available) and never modify their input files.

Failures raise :class:`ToolError` with ``code`` / ``message`` / ``evidence``.
``list_tools()`` returns MCP-shaped ``{name, description, inputSchema}`` entries;
``call_tool(name, arguments)`` validates the arguments and dispatches.
"""

from __future__ import annotations

from . import compare as _compare
from . import frame as _frame
from . import inspect as _inspect
from . import list_brushes as _list_brushes
from . import preview as _preview
from . import render as _render
from . import status as _status
from . import validate as _validate
from .common import TOOL_VERSION, ToolError, jsonable

__all__ = [
    "TOOL_VERSION",
    "ToolError",
    "TOOLS",
    "TOOL_NAMES",
    "ALIASES",
    "list_tools",
    "call_tool",
    "tool_names",
    "status",
    "list_brushes",
    "inspect",
    "render",
    "preview",
    "frame",
    "compare",
    "validate",
]

status = _status.status
list_brushes = _list_brushes.list_brushes
inspect = _inspect.inspect
render = _render.render
preview = _preview.preview
frame = _frame.frame
compare = _compare.compare
validate = _validate.validate

#: Canonical tool registry (order is the ``tools/list`` order).
TOOLS = {
    _status.TOOL_NAME: {
        "function": status,
        "description": _status.DESCRIPTION,
        "inputSchema": _status.INPUT_SCHEMA,
    },
    _list_brushes.TOOL_NAME: {
        "function": list_brushes,
        "description": _list_brushes.DESCRIPTION,
        "inputSchema": _list_brushes.INPUT_SCHEMA,
    },
    _inspect.TOOL_NAME: {
        "function": inspect,
        "description": _inspect.DESCRIPTION,
        "inputSchema": _inspect.INPUT_SCHEMA,
    },
    _render.TOOL_NAME: {
        "function": render,
        "description": _render.DESCRIPTION,
        "inputSchema": _render.INPUT_SCHEMA,
    },
    _preview.TOOL_NAME: {
        "function": preview,
        "description": _preview.DESCRIPTION,
        "inputSchema": _preview.INPUT_SCHEMA,
    },
    _frame.TOOL_NAME: {
        "function": frame,
        "description": _frame.DESCRIPTION,
        "inputSchema": _frame.INPUT_SCHEMA,
    },
    _compare.TOOL_NAME: {
        "function": compare,
        "description": _compare.DESCRIPTION,
        "inputSchema": _compare.INPUT_SCHEMA,
    },
    _validate.TOOL_NAME: {
        "function": validate,
        "description": _validate.DESCRIPTION,
        "inputSchema": _validate.INPUT_SCHEMA,
    },
}

TOOL_NAMES = tuple(TOOLS)

#: Convenience aliases from the analysis doc (``fdraw_status`` ...).
ALIASES = {f"fdraw_{name}": name for name in TOOL_NAMES}


def tool_names() -> tuple[str, ...]:
    """Canonical tool names, in ``tools/list`` order."""
    return TOOL_NAMES


def list_tools() -> list[dict]:
    """MCP ``tools/list`` payload entries."""
    return [
        {
            "name": name,
            "description": TOOLS[name]["description"],
            "inputSchema": TOOLS[name]["inputSchema"],
        }
        for name in TOOL_NAMES
    ]


def _resolve(name):
    if not isinstance(name, str):
        raise ToolError("TOOL_E_ARG", f"tool name must be a string (got {name!r})", {"name": name})
    key = ALIASES.get(name, name)
    if key not in TOOLS:
        raise ToolError(
            "TOOL_E_ARG",
            f"unknown tool {name!r}",
            {"name": name, "known": list(TOOL_NAMES)},
        )
    return key


def _type_ok(value, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_arguments(name: str, arguments) -> dict:
    """Validate ``arguments`` against the tool's inputSchema (shallow, object-rooted)."""
    key = _resolve(name)
    schema = TOOLS[key]["inputSchema"]
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolError(
            "TOOL_E_ARG",
            f"arguments for {key!r} must be an object (got {type(arguments).__name__})",
            {"tool": key},
        )
    properties = schema.get("properties") or {}
    for field in schema.get("required") or ():
        if field not in arguments:
            raise ToolError(
                "TOOL_E_ARG", f"missing required argument {field!r} for {key}", {"tool": key}
            )
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise ToolError(
                "TOOL_E_ARG",
                f"unknown argument(s) for {key}: {unknown}",
                {"tool": key, "unknown": unknown},
            )
    for field, value in arguments.items():
        spec = properties.get(field)
        if not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        if expected and not _type_ok(value, expected):
            raise ToolError(
                "TOOL_E_ARG",
                f"argument {field!r} must be {expected} (got {type(value).__name__})",
                {"tool": key, "argument": field},
            )
        if "enum" in spec and value not in spec["enum"]:
            raise ToolError(
                "TOOL_E_ARG",
                f"argument {field!r} must be one of {spec['enum']} (got {value!r})",
                {"tool": key, "argument": field},
            )
        if expected in ("integer", "number"):
            if "minimum" in spec and value < spec["minimum"]:
                raise ToolError(
                    "TOOL_E_ARG",
                    f"argument {field!r} must be >= {spec['minimum']} (got {value})",
                    {"tool": key, "argument": field},
                )
            if "maximum" in spec and value > spec["maximum"]:
                raise ToolError(
                    "TOOL_E_ARG",
                    f"argument {field!r} must be <= {spec['maximum']} (got {value})",
                    {"tool": key, "argument": field},
                )
            if "exclusiveMinimum" in spec and value <= spec["exclusiveMinimum"]:
                raise ToolError(
                    "TOOL_E_ARG",
                    f"argument {field!r} must be > {spec['exclusiveMinimum']} (got {value})",
                    {"tool": key, "argument": field},
                )
    return dict(arguments)


def call_tool(name: str, arguments=None) -> dict:
    """Validate arguments and run the tool; returns a JSON-serializable dict."""
    key = _resolve(name)
    kwargs = validate_arguments(key, arguments)
    try:
        result = TOOLS[key]["function"](**kwargs)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize unexpected failures
        raise ToolError(
            "TOOL_E_INTERNAL",
            f"{key} failed: {type(exc).__name__}: {exc}",
            {"tool": key},
        ) from exc
    if not isinstance(result, dict):
        raise ToolError(
            "TOOL_E_INTERNAL", f"{key} returned {type(result).__name__}, expected dict", {"tool": key}
        )
    result.setdefault("tool", key)
    return jsonable(result)
