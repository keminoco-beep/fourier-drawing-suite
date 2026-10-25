"""CLI tests for fourier-prompt (WP-A core)."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for candidate in (str(HERE), str(ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from _fixtures import clone, valid_plan, write_json, write_text  # noqa: E402

from fourier_prompt.cli import main  # noqa: E402


def run_cli(*argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(list(argv))
    return code, stdout.getvalue(), stderr.getvalue()


class CliCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def plan_file(self, payload=None, name="plan.json"):
        return write_json(self.tmp, name, payload if payload is not None else valid_plan())

    # -- meta ---------------------------------------------------------------
    def test_version(self):
        code, out, _err = run_cli("version")
        self.assertEqual(code, 0)
        self.assertIn("0.1.0", out)
        self.assertIn("fdsl/2.1", out)

    def test_help(self):
        code, out, _err = run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("schema", out)
        self.assertIn("selftest", out)

    def test_selftest(self):
        code, out, _err = run_cli("selftest")
        self.assertEqual(code, 0, out)
        self.assertIn("checks passed", out)
        self.assertNotIn("FAIL", out)

    def test_module_help_subprocess(self):
        result = subprocess.run(
            [sys.executable, "-m", "fourier_prompt.cli", "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)

    # -- schema: success ----------------------------------------------------
    def test_schema_valid_human(self):
        path = self.plan_file()
        code, out, _err = run_cli("schema", "--plan", path)
        self.assertEqual(code, 0, out)
        self.assertIn("valid: yes", out)
        self.assertIn("elements=3", out)

    def test_schema_valid_json(self):
        path = self.plan_file()
        code, out, _err = run_cli("schema", "--plan", path, "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["counts"]["elements"], 3)
        self.assertEqual(payload["schema"], "fdsl/2.1")

    def test_schema_warning_without_strict_passes(self):
        plan = valid_plan()
        plan["future_field"] = True
        path = self.plan_file(plan)
        code, out, _err = run_cli("schema", "--plan", path)
        self.assertEqual(code, 0, out)
        self.assertIn("FP_W_UNKNOWN_FIELD", out)

    # -- schema: failures ---------------------------------------------------
    def test_schema_invalid_human_exits_1(self):
        plan = valid_plan()
        plan["schema"] = "fdsl/9"
        path = self.plan_file(plan)
        code, out, _err = run_cli("schema", "--plan", path)
        self.assertEqual(code, 1)
        self.assertIn("FP_E_SCHEMA_VERSION", out)
        self.assertIn("valid: no", out)

    def test_schema_invalid_json_exits_1(self):
        plan = valid_plan()
        plan["elements"][1]["gen"] = "unicorn"
        path = self.plan_file(plan)
        code, out, _err = run_cli("schema", "--plan", path, "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        codes = [item["code"] for item in payload["errors"]]
        self.assertIn("FP_E_GEN_UNKNOWN", codes)

    def test_schema_strict_rejects_warnings(self):
        plan = valid_plan()
        plan["future_field"] = True
        path = self.plan_file(plan)
        code, out, _err = run_cli("schema", "--plan", path, "--strict")
        self.assertEqual(code, 1, out)
        self.assertIn("promoted warning", out)

    def test_schema_strict_json_marks_promoted(self):
        plan = valid_plan()
        plan["future_field"] = True
        path = self.plan_file(plan)
        code, out, _err = run_cli("schema", "--plan", path, "--strict", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertTrue(any(item.get("promoted") for item in payload["errors"]))

    def test_missing_plan_file_json_payload(self):
        code, out, _err = run_cli(
            "schema", "--plan", str(Path(self.tmp) / "nope.json"), "--json"
        )
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["errors"][0]["code"], "FP_E_LOAD")

    def test_missing_plan_file_exits_2(self):
        code, _out, err = run_cli("schema", "--plan", str(Path(self.tmp) / "nope.json"))
        self.assertEqual(code, 2)
        self.assertIn("FP_E_LOAD", err)

    def test_invalid_json_exits_2(self):
        path = write_text(self.tmp, "broken.json", "{not json")
        code, _out, err = run_cli("schema", "--plan", path)
        self.assertEqual(code, 2)
        self.assertIn("FP_E_JSON", err)

    def test_missing_plan_argument_exits_2(self):
        code, _out, err = run_cli("schema")
        self.assertEqual(code, 2)
        self.assertIn("--plan", err)

    # -- reserved commands --------------------------------------------------
    def test_reserved_command_fails_loudly(self):
        # plan2fbs/draw were implemented in batch 2 (WP-D); pack/eval remain reserved.
        for name in ("pack", "eval"):
            code, _out, err = run_cli(name)
            self.assertEqual(code, 3, name)
            self.assertIn("NotImplementedError", err)
            self.assertIn("not implemented", err)

    def test_compiler_commands_are_wired(self):
        # Implemented commands must validate required args (exit 2), not fail as reserved.
        for name in ("plan2fbs", "draw"):
            code, _out, err = run_cli(name)
            self.assertEqual(code, 2, name)
            self.assertIn("--plan", err)


if __name__ == "__main__":
    unittest.main()
