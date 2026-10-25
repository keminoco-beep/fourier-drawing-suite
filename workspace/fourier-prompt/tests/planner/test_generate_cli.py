"""generate_cli tests (WP-N): outputs, exit codes, benchmark."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for candidate in (str(HERE), str(ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from fourier_prompt.fdraw import validate as validate_fdraw  # noqa: E402
from fourier_prompt.generate_cli import main  # noqa: E402
from fourier_prompt.schema import validate_plan  # noqa: E402


def run_cli(*argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(list(argv))
    return code, stdout.getvalue(), stderr.getvalue()


class CliOutputCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_plan_json_output(self):
        out = self.tmp / "plans" / "cat.json"
        code, stdout, _stderr = run_cli(
            "--prompt", "画一只黑色的猫，线稿风格，居中", "--out", str(out)
        )
        self.assertEqual(code, 0)
        self.assertTrue(out.is_file())
        plan = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(validate_plan(plan), [])
        self.assertIn("plan_hash:", stdout)
        self.assertIn("mono_ink", stdout)

    def test_png_output(self):
        png = self.tmp / "cat.png"
        code, _stdout, _stderr = run_cli(
            "--prompt", "画一个红色的爱心，水彩风格", "--png", str(png), "--resolution", "64"
        )
        self.assertEqual(code, 0)
        self.assertTrue(png.is_file())
        from PIL import Image

        with Image.open(png) as image:
            self.assertEqual(image.size, (64, 64))
            self.assertEqual(image.mode, "RGB")

    def test_fdraw_output(self):
        fdraw = self.tmp / "star.fdraw"
        code, _stdout, _stderr = run_cli(
            "--prompt", 'draw a golden star with text "STAR", minimal style',
            "--fdraw", str(fdraw), "--resolution", "64",
        )
        self.assertEqual(code, 0)
        self.assertTrue(fdraw.is_file())
        self.assertEqual(validate_fdraw(str(fdraw)), [])

    def test_seed_is_reported_and_repeatable(self):
        first = self.tmp / "a.json"
        second = self.tmp / "b.json"
        args = ["--prompt", "一条龙", "--seed", "11", "--resolution", "64"]
        self.assertEqual(run_cli(*args, "--out", str(first))[0], 0)
        self.assertEqual(run_cli(*args, "--out", str(second))[0], 0)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_json_report(self):
        out = self.tmp / "plan.json"
        code, stdout, _stderr = run_cli(
            "--prompt", "画三颗心，红粉，重复排列，横版",
            "--out", str(out), "--json", "--resolution", "64",
        )
        self.assertEqual(code, 0)
        report = json.loads(stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["elements"], 6)
        self.assertEqual(report["style"], "minimal")
        self.assertTrue(report["plan_hash"])

    def test_no_output_prints_plan_summary(self):
        code, stdout, _stderr = run_cli("--prompt", "画一个圆", "--resolution", "64")
        self.assertEqual(code, 0)
        self.assertIn("elements=", stdout)


class CliExitCodeCase(unittest.TestCase):
    def test_planner_error_exit_code(self):
        code, _stdout, stderr = run_cli("--prompt", "!!!???")
        self.assertEqual(code, 3)
        self.assertIn("FP_E_PROMPT_UNPARSEABLE", stderr)

    def test_missing_prompt_exit_code(self):
        code, _stdout, stderr = run_cli("--resolution", "64")
        self.assertEqual(code, 2)
        self.assertIn("--prompt is required", stderr)

    def test_bad_resolution_exit_code(self):
        code, _stdout, stderr = run_cli("--prompt", "画一个圆", "--resolution", "4096")
        self.assertEqual(code, 2)
        self.assertIn("--resolution", stderr)

    def test_help_exit_code(self):
        code, stdout, _stderr = run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("Exit codes", stdout)

    def test_unknown_flag_exit_code(self):
        code, _stdout, _stderr = run_cli("--nope")
        self.assertEqual(code, 2)


class BenchmarkCase(unittest.TestCase):
    def test_two_prompt_benchmark(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            prompts_path = tmp_path / "prompts.jsonl"
            prompts_path.write_text(
                "\n".join(
                    [
                        json.dumps({"id": "b1", "prompt": "画一个红色的圆"}),
                        json.dumps({"id": "b2", "prompt": "一条龙，线稿", "seed": 3}),
                    ]
                ),
                encoding="utf-8",
            )
            out_dir = tmp_path / "out"
            code, stdout, _stderr = run_cli(
                "--benchmark", str(prompts_path), "--out-dir", str(out_dir),
                "--resolution", "64",
            )
            self.assertEqual(code, 0)
            payload = json.loads((out_dir / "benchmark.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["total"], 2)
            self.assertEqual(payload["summary"]["rendered"], 2)
            self.assertEqual(payload["summary"]["deterministic"], 2)
            self.assertTrue((out_dir / "contact_sheet.png").is_file())
            self.assertTrue((out_dir / "png" / "b1.png").is_file())
            self.assertIn("summary:", stdout)
            self.assertTrue(any(row["warnings"] for row in payload["rows"]))


if __name__ == "__main__":
    unittest.main()
