"""Minimal MCP server for fourier-prompt — stdlib JSON-RPC 2.0 over stdio (WP-H).

Implemented methods: ``initialize``, ``notifications/initialized``,
``tools/list``, ``tools/call`` and ``ping``.  One JSON-RPC message per line;
stdout carries **only** protocol JSON, all logging goes to stderr.

Run::

    python -m fourier_prompt.mcp_server

Every tool result is a text block with the JSON payload; results that carry an
``image`` field add an MCP ``image`` content block (base64 PNG, long side
``<= 768``).  Tool failures become JSON-RPC errors (unknown tool / bad
arguments -> ``-32602``, execution failures -> ``-32603``); protocol failures
use ``-32700`` / ``-32600`` / ``-32601``.
"""

from __future__ import annotations

import json
import sys
import traceback

from . import __version__
from .tools import TOOL_VERSION, ToolError, call_tool, list_tools

__all__ = ["main", "SERVER_NAME", "PROTOCOL_VERSION", "handle_message"]

SERVER_NAME = "fourier-prompt"
SERVER_TITLE = "fourier-prompt tools"
PROTOCOL_VERSION = "2024-11-05"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: Tools whose results can carry an MCP image content block.
IMAGE_TOOLS = ("render", "preview", "frame")

_ARG_ERROR_CODES = {
    "TOOL_E_ARG",
    "TOOL_E_NOT_FOUND",
    "TOOL_E_INPUT",
    "TOOL_E_LOAD",
    "TOOL_E_CATALOG",
}


def _log(message: str) -> None:
    """Diagnostics only — never stdout (that would corrupt the protocol)."""
    try:
        print(f"[fourier-prompt-mcp] {message}", file=sys.stderr, flush=True)
    except Exception:  # pragma: no cover - stderr may be closed
        pass


def _ok(request_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str, data=None) -> dict:
    error = {"code": int(code), "message": str(message)}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _initialize(params: dict) -> dict:
    requested = params.get("protocolVersion")
    version = requested if isinstance(requested, str) and requested else PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {
            "name": SERVER_NAME,
            "title": SERVER_TITLE,
            "version": f"{__version__}+tools{TOOL_VERSION}",
        },
        "instructions": (
            "fourier-prompt agent tools: status, list_brushes, inspect, render, "
            "preview, frame, compare, validate. Inputs are .fdraw containers, .fbs "
            "documents or FDSL plans; render/preview/frame write PNGs and also "
            "return an inline image."
        ),
    }


def _image_blocks(result: dict) -> tuple[dict, list]:
    """Split the ``image`` field into a text-safe stub plus MCP image blocks."""
    image = result.get("image")
    if not isinstance(image, dict) or not isinstance(image.get("data"), str):
        return result, []
    payload = dict(result)
    payload["image"] = {
        key: value for key, value in image.items() if key != "data"
    }
    payload["image"]["attached"] = True
    block = {
        "type": "image",
        "data": image["data"],
        "mimeType": image.get("mimeType") or "image/png",
    }
    return payload, [block]


def _tool_result(result: dict) -> dict:
    payload, images = _image_blocks(result)
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    content = [{"type": "text", "text": text}]
    content.extend(images)
    return {"content": content, "isError": False}


def _call_tool(params: dict) -> dict:
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise _RpcError(INVALID_PARAMS, "tools/call requires a string 'name'", {"params": params})
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise _RpcError(
            INVALID_PARAMS,
            f"'arguments' must be an object (got {type(arguments).__name__})",
            {"tool": name},
        )
    arguments = dict(arguments)
    if name in IMAGE_TOOLS:
        arguments.setdefault("include_image", True)
    try:
        result = call_tool(name, arguments)
    except ToolError as exc:
        code = INVALID_PARAMS if exc.code in _ARG_ERROR_CODES else INTERNAL_ERROR
        raise _RpcError(
            code,
            f"{exc.code}: {exc.message}",
            {"tool_code": exc.code, "evidence": exc.evidence, "tool": name},
        ) from exc
    return _tool_result(result)


class _RpcError(Exception):
    """Internal carrier for a JSON-RPC error raised while handling a request."""

    def __init__(self, code: int, message: str, data=None):
        self.code = int(code)
        self.message = str(message)
        self.data = data
        super().__init__(self.message)


def handle_message(message):
    """Handle one decoded JSON-RPC message; returns a response dict or ``None``."""
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "request must be a JSON object")
    request_id = message.get("id")
    is_notification = "id" not in message
    version = message.get("jsonrpc")
    if version != "2.0":
        return None if is_notification else _error(
            request_id, INVALID_REQUEST, "'jsonrpc' must be '2.0'"
        )
    method = message.get("method")
    if not isinstance(method, str) or not method:
        return None if is_notification else _error(
            request_id, INVALID_REQUEST, "'method' must be a non-empty string"
        )
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return None if is_notification else _error(
            request_id, INVALID_PARAMS, "'params' must be an object"
        )
    try:
        if method == "initialize":
            result = _initialize(params)
        elif method == "notifications/initialized":
            return None
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": list_tools()}
        elif method == "tools/call":
            result = _call_tool(params)
        else:
            if is_notification:
                return None
            return _error(request_id, METHOD_NOT_FOUND, f"method not found: {method!r}")
    except _RpcError as exc:
        return None if is_notification else _error(request_id, exc.code, exc.message, exc.data)
    except Exception as exc:  # noqa: BLE001 - never crash the server loop
        _log("unhandled error:\n" + traceback.format_exc())
        return None if is_notification else _error(
            request_id,
            INTERNAL_ERROR,
            f"internal error: {type(exc).__name__}: {exc}",
        )
    if is_notification:
        return None
    return _ok(request_id, result)


def _handle_line(line: str):
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        return _error(None, PARSE_ERROR, f"parse error: {exc}")
    if isinstance(message, list):
        if not message:
            return _error(None, INVALID_REQUEST, "empty batch")
        responses = [
            response
            for response in (handle_message(item) for item in message)
            if response is not None
        ]
        return responses or None
    return handle_message(message)


def serve(stdin=None, stdout=None) -> int:
    """Read JSON-RPC lines until EOF; returns a process exit code."""
    input_stream = stdin if stdin is not None else sys.stdin
    output_stream = stdout if stdout is not None else sys.stdout
    if input_stream is None:  # pragma: no cover - closed stdin
        return 0
    for raw_line in input_stream:
        line = raw_line.strip()
        if not line:
            continue
        response = _handle_line(line)
        if response is None:
            continue
        targets = response if isinstance(response, list) else [response]
        for item in targets:
            output_stream.write(json.dumps(item, ensure_ascii=True, allow_nan=False) + "\n")
        output_stream.flush()
    return 0


def main(argv=None) -> int:
    """Entry point (``python -m fourier_prompt.mcp_server``)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # pragma: no cover - exotic streams
                pass
    if argv and argv[0] in ("--version", "-V"):
        print(f"{SERVER_NAME} {__version__} (tools {TOOL_VERSION}, {PROTOCOL_VERSION})")
        return 0
    if argv and argv[0] in ("--help", "-h"):
        print(
            "usage: python -m fourier_prompt.mcp_server\n"
            "Minimal MCP (JSON-RPC 2.0 over stdio). Methods: initialize, ping, "
            "tools/list, tools/call. Logs go to stderr.",
            file=sys.stderr,
        )
        return 0
    _log(f"serving {SERVER_NAME} {__version__} tools={TOOL_VERSION}")
    try:
        return serve()
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except BrokenPipeError:  # pragma: no cover
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
