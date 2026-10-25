"""End-to-end tests for ``scripts/demo.py`` (subprocess, small resolution)."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from _support import DEMO, PKG_ROOT, run_demo, sha256

from fourier_prompt import fdraw

R = "64"
SIMPLE = "skill/examples/simple_plan.json"


class DemoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _assert_ok(self, result, label):
        self.assertEqual(
            result.returncode,
            0,
            f"{label} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_demo_simple_plan_produces_all_core_artifacts(self):
        out = self.tmp / "demo"
        result = run_demo("--plan", SIMPLE, "--outdir", out, "--resolution", R)
        self._assert_ok(result, "simple demo")

        for marker in (
            "[1/6] schema",
            "[2/6] plan2fbs",
            "[3/6] draw",
            "[4/6] fdraw pack",
            "[5/6] layer export skipped",
            "[6/6] video        skipped",
            "determinism:",
        ):
            self.assertIn(marker, result.stdout)

        fbs = out / "simple_plan.fbs"
        png = out / "simple_plan_R64.png"
        container = out / "simple_plan.fdraw"
        for path in (fbs, png, container):
            self.assertTrue(path.is_file(), path)
            self.assertGreater(path.stat().st_size, 0, path)

        self.assertGreater(json.loads(fbs.read_text(encoding="utf-8"))["strokes"].__len__(), 0)
        self.assertEqual(fdraw.validate(str(container)), [])
        info = fdraw.inspect(str(container))
        self.assertTrue(info["root_sha256_ok"])
        self.assertEqual(info["render"]["target_resolution"], 64)
        self.assertEqual(info["source_schema"], "fdsl/2.1")

    def test_demo_is_byte_deterministic(self):
        a = self.tmp / "a"
        b = self.tmp / "b"
        first = run_demo("--plan", SIMPLE, "--outdir", a, "--resolution", R)
        second = run_demo("--plan", SIMPLE, "--outdir", b, "--resolution", R)
        self._assert_ok(first, "first run")
        self._assert_ok(second, "second run")

        for name in ("simple_plan.fbs", "simple_plan_R64.png", "simple_plan.fdraw"):
            with self.subTest(artifact=name):
                self.assertEqual(sha256(a / name), sha256(b / name))

        # The demo drops the wall-clock ``created`` metadata to stay reproducible.
        self.assertNotIn("created", json.loads((a / "simple_plan.fbs").read_text(encoding="utf-8")))

    def test_demo_psd_and_kra_export(self):
        out = self.tmp / "layered"
        result = run_demo(
            "--plan", SIMPLE, "--outdir", out, "--resolution", R, "--psd", "--kra"
        )
        self._assert_ok(result, "layered demo")
        self.assertIn("[5/6] layer export ok", result.stdout)
        self.assertIn("psd=", result.stdout)
        self.assertIn("kra=", result.stdout)

        layers = out / "layers"
        psd = layers / "simple_plan.psd"
        kra = layers / "simple_plan.kra"
        self.assertTrue(psd.is_file(), psd)
        self.assertTrue(kra.is_file(), kra)
        self.assertEqual(psd.read_bytes()[:4], b"8BPS")
        self.assertTrue(zipfile.is_zipfile(kra))
        self.assertGreater(psd.stat().st_size, 0)
        self.assertGreater(kra.stat().st_size, 0)

        # The container must remain untouched by the export step.
        self.assertEqual(fdraw.validate(str(out / "simple_plan.fdraw")), [])

    def test_demo_missing_plan_is_usage_error(self):
        out = self.tmp / "missing"
        result = run_demo(
            "--plan", self.tmp / "does_not_exist.json", "--outdir", out, "--resolution", R
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn("FP_E_LOAD", combined)
        self.assertIn("demo: FAILED at schema", combined)
        self.assertFalse(out.exists())

    def test_demo_bad_resolution_is_usage_error(self):
        out = self.tmp / "badres"
        for value in ("8", "257"):
            with self.subTest(resolution=value):
                result = run_demo("--plan", SIMPLE, "--outdir", out, "--resolution", value)
                self.assertEqual(result.returncode, 2, result.stderr)
                combined = result.stdout + result.stderr
                self.assertIn("FP_E_USAGE", combined)
                self.assertIn("--resolution", combined)

    def test_demo_works_from_outside_package_root(self):
        out = self.tmp / "outside"
        result = run_demo(
            "--plan",
            (PKG_ROOT / SIMPLE).resolve(),
            "--outdir",
            out,
            "--resolution",
            R,
            "--psd",
            cwd=PKG_ROOT.parent,
        )
        self._assert_ok(result, "outside-cwd demo")
        self.assertIn("[5/6] layer export ok", result.stdout)
        self.assertTrue((out / "simple_plan.fbs").is_file())
        self.assertTrue((out / "simple_plan_R64.png").is_file())
        self.assertTrue((out / "simple_plan.fdraw").is_file())
        self.assertTrue((out / "layers" / "simple_plan.psd").is_file())

    def test_demo_script_only_calls_public_interfaces(self):
        source = (PKG_ROOT / "scripts" / "demo.py").read_text(encoding="utf-8")
        self.assertTrue(DEMO.is_file())
        for token in (
            "fourier_prompt.compile",
            "fourier_prompt.compat",
            "fdraw.pack",
            "fourier_prompt.layer_cli",
            "fourier_prompt.fdvideo_cli",
        ):
            self.assertIn(token, source)
        # No re-implementation of Fourier fitting / rendering internals.
        for forbidden in ("fbs_core", "np.fft", "def _fit", "from fbs_core"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
