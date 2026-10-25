"""CLI ``plan2fbs`` / ``draw`` exit-code and output tests (WP-D)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from _fixtures import (
    EXAMPLES,
    PACKAGE_ROOT,
    canonical_plan_shapes,
    run_cli,
    write_json,
)

if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from fourier_prompt.compat import skill_api  # noqa: E402


class CompileCliCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def plan_file(self, payload=None, name="plan.json"):
        return write_json(self.tmp, name, payload if payload is not None else canonical_plan_shapes())

    def test_plan2fbs_success(self):
        out = self.tmp / "plan.fbs"
        code, stdout, stderr = run_cli(
            "plan2fbs", "--plan", self.plan_file(), "--out", str(out), "--resolution", "128"
        )
        self.assertEqual(code, 0, stderr + stdout)
        self.assertTrue(out.is_file())
        self.assertIn("compile:", stdout)
        self.assertIn("records=4", stdout)
        reloaded = skill_api.import_skill().io_format.read_fbs(str(out))
        self.assertEqual(reloaded["format"], skill_api.import_skill().io_format.FORMAT)

    def test_plan2fbs_json_payload(self):
        out = self.tmp / "plan.fbs"
        code, stdout, stderr = run_cli(
            "plan2fbs",
            "--plan",
            self.plan_file(),
            "--out",
            str(out),
            "--resolution",
            "128",
            "--json",
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "plan2fbs")
        self.assertEqual(payload["stats"]["fill"], 2)
        self.assertTrue(payload["stats"]["fbs_sha256"])
        self.assertEqual(payload["out"], str(out.resolve()))

    def test_draw_success_writes_png(self):
        out = self.tmp / "plan.png"
        code, stdout, stderr = run_cli(
            "draw", "--plan", self.plan_file(), "--out", str(out), "--resolution", "128"
        )
        self.assertEqual(code, 0, stderr + stdout)
        self.assertTrue(out.is_file())
        self.assertEqual(out.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIn("png:", stdout)

    def test_draw_json_and_fbs_out(self):
        out = self.tmp / "plan.png"
        fbs_out = self.tmp / "plan.fbs"
        code, stdout, stderr = run_cli(
            "draw",
            "--plan",
            self.plan_file(),
            "--out",
            str(out),
            "--fbs-out",
            str(fbs_out),
            "--resolution",
            "128",
            "--json",
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["stats"]["png_sha256"]), 64)
        self.assertTrue(fbs_out.is_file())
        self.assertGreater(payload["stats"]["png_bytes"], 1000)

    def test_auto_legacy_conversion(self):
        out = self.tmp / "rose.png"
        code, stdout, stderr = run_cli(
            "draw",
            "--plan",
            str(EXAMPLES / "01_rose_medallion.json"),
            "--out",
            str(out),
            "--resolution",
            "128",
            "--json",
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["stats"]["legacy"])
        self.assertGreaterEqual(payload["stats"]["records"], 40)

    def test_cli_determinism(self):
        plan = self.plan_file()
        hashes = []
        for index in range(2):
            out = self.tmp / f"run{index}.png"
            code, stdout, stderr = run_cli(
                "draw", "--plan", plan, "--out", str(out), "--resolution", "128", "--json"
            )
            self.assertEqual(code, 0, stderr)
            hashes.append(json.loads(stdout)["stats"]["png_sha256"])
        self.assertEqual(hashes[0], hashes[1])


class CompileCliFailureCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_unknown_gen_exits_1(self):
        plan = canonical_plan_shapes()
        plan["elements"][1]["gen"] = "unicorn"
        code, _stdout, stderr = run_cli(
            "plan2fbs", "--plan", write_json(self.tmp, "p.json", plan), "--out", str(self.tmp / "x.fbs")
        )
        self.assertEqual(code, 1)
        self.assertIn("FP_E_GEN_UNKNOWN", stderr)

    def test_unknown_brush_exits_1_json(self):
        plan = canonical_plan_shapes()
        plan["elements"][3]["brush"]["preset"] = "unicorn_brush"
        code, stdout, _stderr = run_cli(
            "draw",
            "--plan",
            write_json(self.tmp, "p.json", plan),
            "--out",
            str(self.tmp / "x.png"),
            "--json",
        )
        self.assertEqual(code, 1)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["errors"][0]["code"], "FP_E_BRUSH_PRESET_UNKNOWN")

    def test_missing_plan_exits_2(self):
        code, _stdout, stderr = run_cli(
            "plan2fbs", "--plan", str(self.tmp / "nope.json"), "--out", str(self.tmp / "x.fbs")
        )
        self.assertEqual(code, 2)
        self.assertIn("FP_E_LOAD", stderr)

    def test_invalid_json_exits_2(self):
        path = self.tmp / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        code, _stdout, stderr = run_cli(
            "draw", "--plan", str(path), "--out", str(self.tmp / "x.png")
        )
        self.assertEqual(code, 2)
        self.assertIn("FP_E_JSON", stderr)

    def test_resolution_above_512_exits_2(self):
        code, _stdout, stderr = run_cli(
            "draw",
            "--plan",
            write_json(self.tmp, "p.json", canonical_plan_shapes()),
            "--out",
            str(self.tmp / "x.png"),
            "--resolution",
            "1024",
        )
        self.assertEqual(code, 2)
        self.assertIn("FP_E_USAGE", stderr)

    def test_text_only_plan_exits_1(self):
        plan = canonical_plan_shapes()
        plan["elements"] = [plan["elements"][4]]
        code, _stdout, stderr = run_cli(
            "plan2fbs",
            "--plan",
            write_json(self.tmp, "p.json", plan),
            "--out",
            str(self.tmp / "x.fbs"),
        )
        self.assertEqual(code, 1)
        self.assertIn("FP_E_COMPILE_EMPTY", stderr)

    def test_pack_remains_reserved(self):
        code, _stdout, stderr = run_cli("pack")
        self.assertEqual(code, 3)
        self.assertIn("NotImplementedError", stderr)


if __name__ == "__main__":
    unittest.main()
