"""Subprocess tests for ``python -m fourier_prompt.mcp_server`` (WP-H).

The server is driven over real stdio: one JSON-RPC message per line.  These
tests assert the handshake, ``tools/list``, successful ``tools/call`` results
(status / render with an inline image / compare) and JSON-RPC errors for
unknown tools, bad arguments, unknown methods and malformed lines.
"""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import unittest

from _fixtures import PKG_ROOT, Fixture, tools

PYTHON = sys.executable


def run_mcp(messages, *, raw_lines=(), timeout=120):
    """Spawn the MCP server, feed messages, return (responses, stderr, stdout)."""
    lines = [json.dumps(message) for message in messages]
    lines.extend(raw_lines)
    payload = "\n".join(lines) + "\n"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.run(
        [PYTHON, "-m", "fourier_prompt.mcp_server"],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(PKG_ROOT),
        env=env,
        timeout=timeout,
    )
    responses = []
    for line in process.stdout.splitlines():
        if not line.strip():
            continue
        responses.append(json.loads(line))  # stdout must be protocol JSON only
    return responses, process.stderr, process.stdout


def request(request_id, method, params=None):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def tool_call(request_id, name, arguments):
    return request(request_id, "tools/call", {"name": name, "arguments": arguments})


def by_id(responses, request_id):
    for response in responses:
        if response.get("id") == request_id:
            return response
    raise AssertionError(f"no response with id={request_id}: {responses!r}")


class McpServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture("fpt-tools-mcp-")
        cls.png_a = tools.render(
            str(cls.fx.fdraw_path), cls.fx.out("mcp_a.png"), resolution=48
        )["out"]
        cls.png_b = tools.preview(
            str(cls.fx.fdraw_path), cls.fx.out("mcp_b.png"), fraction=0.5, resolution=48
        )["out"]

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_initialize_handshake(self):
        responses, _stderr, _stdout = run_mcp(
            [
                request(
                    1,
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "wp-h-test", "version": "1"},
                    },
                )
            ]
        )
        self.assertEqual(len(responses), 1)
        response = responses[0]
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 1)
        result = response["result"]
        self.assertEqual(result["protocolVersion"], "2024-11-05")
        self.assertEqual(result["serverInfo"]["name"], "fourier-prompt")
        self.assertIn("tools", result["capabilities"])
        self.assertFalse(result["capabilities"]["tools"]["listChanged"])

    def test_tools_list_has_eight_tools_with_schemas(self):
        responses, _stderr, _stdout = run_mcp([request(2, "tools/list")])
        tools_list = by_id(responses, 2)["result"]["tools"]
        names = [item["name"] for item in tools_list]
        self.assertEqual(len(tools_list), 8)
        self.assertEqual(
            names,
            ["status", "list_brushes", "inspect", "render", "preview", "frame", "compare", "validate"],
        )
        for item in tools_list:
            self.assertEqual(item["inputSchema"]["type"], "object")
            self.assertIsInstance(item["description"], str)
            self.assertTrue(item["description"])
        render_schema = next(item for item in tools_list if item["name"] == "render")
        self.assertEqual(render_schema["inputSchema"]["required"], ["path", "out"])
        self.assertIn("resolution", render_schema["inputSchema"]["properties"])

    def test_tools_call_status(self):
        responses, _stderr, _stdout = run_mcp(
            [tool_call(3, "status", {"selftest": False})]
        )
        result = by_id(responses, 3)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][0]["type"], "text")
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tool"], "status")
        self.assertTrue(payload["skill"]["found"])

    def test_tools_call_render_returns_image(self):
        out = self.fx.out("mcp_render.png")
        responses, _stderr, _stdout = run_mcp(
            [tool_call(4, "render", {"path": str(self.fx.fdraw_path), "out": out, "resolution": 64})]
        )
        result = by_id(responses, 4)["result"]
        kinds = [block["type"] for block in result["content"]]
        self.assertEqual(kinds, ["text", "image"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["width"], 64)
        self.assertTrue(payload["image"]["attached"])
        image_block = result["content"][1]
        self.assertEqual(image_block["mimeType"], "image/png")
        raw = base64.b64decode(image_block["data"])
        self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
        self.assertTrue(os.path.isfile(out))
        with self.subTest("image long side <= 768"):
            from PIL import Image

            with Image.open(io.BytesIO(raw)) as image:
                self.assertLessEqual(max(image.size), 768)

    def test_tools_call_compare(self):
        responses, _stderr, _stdout = run_mcp(
            [tool_call(5, "compare", {"a": self.png_a, "b": self.png_b})]
        )
        payload = json.loads(by_id(responses, 5)["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertGreater(payload["metrics"]["max_abs_diff"], 0)
        self.assertFalse(payload["parity_pass"])
        self.assertEqual(payload["shape"], [48, 48])

        same, _stderr, _stdout = run_mcp(
            [tool_call(6, "compare", {"a": self.png_a, "b": self.png_a})]
        )
        identical = json.loads(by_id(same, 6)["result"]["content"][0]["text"])
        self.assertTrue(identical["parity_pass"])
        self.assertEqual(identical["metrics"]["max_abs_diff"], 0)

    def test_unknown_tool_returns_rpc_error(self):
        responses, _stderr, _stdout = run_mcp([tool_call(7, "draw_everything", {})])
        response = by_id(responses, 7)
        self.assertNotIn("result", response)
        error = response["error"]
        self.assertEqual(error["code"], -32602)
        self.assertIn("unknown tool", error["message"])
        self.assertEqual(error["data"]["tool_code"], "TOOL_E_ARG")

    def test_bad_arguments_return_rpc_error(self):
        cases = (
            (8, {"name": "render", "arguments": {"path": str(self.fx.fdraw_path)}}, "missing required"),
            (9, {"name": "status", "arguments": {"selftest": False, "verbose": True}}, "unknown argument"),
            (10, {"name": "compare", "arguments": {"a": self.png_a}}, "missing required"),
            (11, {"name": "render", "arguments": {"path": str(self.fx.fdraw_path), "out": self.fx.out("x.png"), "quality": "ultra"}}, "quality"),
        )
        responses, _stderr, _stdout = run_mcp(
            [request(request_id, "tools/call", params) for request_id, params, _hint in cases]
        )
        for request_id, _params, hint in cases:
            with self.subTest(hint=hint):
                error = by_id(responses, request_id)["error"]
                self.assertEqual(error["code"], -32602)
                self.assertIn(hint, error["message"])
                self.assertEqual(error["data"]["tool_code"], "TOOL_E_ARG")

        missing_path = run_mcp([tool_call(12, "inspect", {"path": str(self.fx.dir / "nope.fdraw")})])
        error = by_id(missing_path[0], 12)["error"]
        self.assertEqual(error["data"]["tool_code"], "TOOL_E_NOT_FOUND")

    def test_ping_and_unknown_method(self):
        responses, _stderr, _stdout = run_mcp(
            [request(13, "ping"), request(14, "prompts/list")]
        )
        self.assertEqual(by_id(responses, 13)["result"], {})
        error = by_id(responses, 14)["error"]
        self.assertEqual(error["code"], -32601)
        self.assertIn("method not found", error["message"])

    def test_notifications_produce_no_response(self):
        responses, _stderr, _stdout = run_mcp(
            [
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
                request(15, "ping"),
            ]
        )
        self.assertEqual(len(responses), 1)
        self.assertEqual(by_id(responses, 15)["result"], {})

    def test_parse_error_and_stdout_hygiene(self):
        responses, stderr, stdout = run_mcp(
            [request(16, "ping")], raw_lines=["{this is not json", "[]"]
        )
        parse_responses = [item for item in responses if item.get("id") is None]
        self.assertTrue(parse_responses)
        self.assertEqual(parse_responses[0]["error"]["code"], -32700)
        self.assertEqual(by_id(responses, 16)["result"], {})
        self.assertIn("[fourier-prompt-mcp]", stderr)
        for line in stdout.splitlines():
            if line.strip():
                json.loads(line)


if __name__ == "__main__":
    unittest.main()
