"""``layer_cli`` export / inspect / exit-code tests (WP-I)."""

from __future__ import annotations

import json
import sys
import unittest

from _fixtures import PACKAGE_ROOT, SAMPLE_FDRAW, run_cli, tempdir

if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from fourier_prompt import kra, psd  # noqa: E402


class ExportCliCase(unittest.TestCase):
    def test_export_all_formats(self):
        with tempdir() as directory:
            code, stdout, stderr = run_cli(
                [
                    "export",
                    "--fdraw",
                    str(SAMPLE_FDRAW),
                    "--out-dir",
                    str(directory),
                    "--psd",
                    "--kra",
                    "--layers-png",
                    "--resolution",
                    "32",
                    "--quality",
                    "fast",
                    "--json",
                ]
            )
            self.assertEqual(code, 0, stderr)
            payload = json.loads(stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["layer_count"], 6)
            psd_path = directory / "01.psd"
            kra_path = directory / "01.kra"
            layer_dir = directory / "01_layers"
            self.assertTrue(psd_path.is_file())
            self.assertTrue(kra_path.is_file())
            self.assertTrue((layer_dir / "layers.json").is_file())
            self.assertEqual(payload["outputs"]["layers_png"]["count"], 6)
            self.assertEqual(payload["outputs"]["psd"]["bytes"], psd_path.stat().st_size)
            self.assertEqual(payload["outputs"]["kra"]["bytes"], kra_path.stat().st_size)
            # exported files are real formats
            with psd_path.open("rb") as handle:
                self.assertEqual(handle.read(4), b"8BPS")
            with kra_path.open("rb") as handle:
                self.assertEqual(handle.read(2), b"PK")

    def test_export_defaults_to_all_formats(self):
        with tempdir() as directory:
            code, stdout, stderr = run_cli(
                [
                    "export",
                    "--fdraw",
                    str(SAMPLE_FDRAW),
                    "--out-dir",
                    str(directory),
                    "--resolution",
                    "16",
                ]
            )
            self.assertEqual(code, 0, stderr)
            self.assertTrue((directory / "01.psd").is_file())
            self.assertTrue((directory / "01.kra").is_file())
            self.assertTrue((directory / "01_layers").is_dir())
            self.assertIn("layers:", stdout)

    def test_inspect_layers_json(self):
        code, stdout, stderr = run_cli(["inspect-layers", "--fdraw", str(SAMPLE_FDRAW), "--json"])
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["layer_count"], 6)
        self.assertEqual([layer["index"] for layer in payload["layers"]], list(range(6)))
        self.assertEqual(sum(layer["stroke_count"] + layer["fill_count"] for layer in payload["layers"]), 51)

    def test_inspect_layers_human(self):
        code, stdout, stderr = run_cli(["inspect-layers", "--fdraw", str(SAMPLE_FDRAW)])
        self.assertEqual(code, 0, stderr)
        self.assertIn("layers=6", stdout)
        self.assertIn("bbox", stdout)

    def test_export_bad_resolution_fails_with_1(self):
        with tempdir() as directory:
            code, _stdout, stderr = run_cli(
                [
                    "export",
                    "--fdraw",
                    str(SAMPLE_FDRAW),
                    "--out-dir",
                    str(directory),
                    "--resolution",
                    "1024",
                ]
            )
            self.assertEqual(code, 1)
            self.assertIn("FP_E_LAYER_RESOLUTION", stderr)

    def test_export_missing_file_fails_with_1(self):
        with tempdir() as directory:
            code, _stdout, stderr = run_cli(
                [
                    "export",
                    "--fdraw",
                    str(directory / "missing.fdraw"),
                    "--out-dir",
                    str(directory),
                ]
            )
            self.assertEqual(code, 1)
            self.assertIn("FD_E_INPUT", stderr)

    def test_bad_quality_is_usage_error_2(self):
        with tempdir() as directory:
            code, _stdout, _stderr = run_cli(
                [
                    "export",
                    "--fdraw",
                    str(SAMPLE_FDRAW),
                    "--out-dir",
                    str(directory),
                    "--quality",
                    "ultra",
                ]
            )
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
