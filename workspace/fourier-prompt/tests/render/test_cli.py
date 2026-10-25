"""Tests for the ``fd-render`` CLI (WP-F)."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from _fixtures import SAMPLE_DIR, synthetic_doc

from fourier_prompt import fdraw, fdrender_cli

PKG_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_FBS = SAMPLE_DIR / "03_golden_spiral.fbs"


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fdrender_cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class CLICase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.fbs = cls.tmp / "spiral.fbs"
        cls.fbs.write_text(SAMPLE_FBS.read_text(encoding="utf-8"), encoding="utf-8")
        doc = json.loads(cls.fbs.read_text(encoding="utf-8"))
        cls.fdraw_path = cls.tmp / "spiral.fdraw"
        fdraw.pack(doc, out=str(cls.fdraw_path))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_render_fbs_to_png_json(self):
        out = self.tmp / "render_fbs.png"
        code, stdout, stderr = run_cli([
            "render", "--fbs", str(self.fbs), "--out", str(out),
            "--resolution", "96", "--json",
        ])
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["quality"], "fast")
        self.assertEqual(payload["size"], [96, 96])
        self.assertTrue(out.is_file())
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")

    def test_render_fdraw_and_exact(self):
        out = self.tmp / "render_fdraw.png"
        code, stdout, stderr = run_cli([
            "render", "--fdraw", str(self.fdraw_path), "--out", str(out),
            "--resolution", "96", "--quality", "exact", "--json",
        ])
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["quality"], "exact")
        self.assertTrue(out.is_file())

    def test_render_is_deterministic_pixels(self):
        a = self.tmp / "det_a.png"
        b = self.tmp / "det_b.png"
        for path in (a, b):
            code, _stdout, stderr = run_cli([
                "render", "--fbs", str(self.fbs), "--out", str(path), "--resolution", "96",
            ])
            self.assertEqual(code, 0, stderr)
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_info_json(self):
        code, stdout, stderr = run_cli(["info", "--fdraw", str(self.fdraw_path), "--json"])
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["fast_supported"])
        self.assertEqual(payload["elements"], 7)
        self.assertEqual(payload["fills"], 1)

    def test_preview_fraction_and_frame_at(self):
        prev = self.tmp / "preview.png"
        code, stdout, stderr = run_cli([
            "preview", "--fdraw", str(self.fdraw_path), "--fraction", "0.3",
            "--out", str(prev), "--resolution", "96", "--json",
        ])
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["elements_kept"], 3)
        self.assertEqual(payload["elements_total"], 7)

        frame = self.tmp / "frame.png"
        code, stdout, stderr = run_cli([
            "frame", "--fdraw", str(self.fdraw_path), "--at", "1",
            "--out", str(frame), "--resolution", "96", "--json",
        ])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["elements_kept"], 1)
        self.assertNotEqual(prev.read_bytes(), frame.read_bytes())

    def test_compare_identical_and_different(self):
        a = self.tmp / "cmp_a.png"
        code, _stdout, _stderr = run_cli([
            "render", "--fbs", str(self.fbs), "--out", str(a), "--resolution", "96",
        ])
        self.assertEqual(code, 0)
        code, stdout, stderr = run_cli(["compare", "--a", str(a), "--b", str(a), "--json"])
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["max_abs_diff"], 0)
        self.assertTrue(payload["parity_pass"])

        b = self.tmp / "cmp_b.png"
        run_cli(["frame", "--fbs", str(self.fbs), "--at", "1", "--out", str(b), "--resolution", "96"])
        code, stdout, stderr = run_cli(["compare", "--a", str(a), "--b", str(b), "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertFalse(json.loads(stdout)["parity_pass"])

    def test_compare_human_output(self):
        a = self.tmp / "human_a.png"
        run_cli(["render", "--fbs", str(self.fbs), "--out", str(a), "--resolution", "64"])
        code, stdout, stderr = run_cli(["compare", "--a", str(a), "--b", str(a)])
        self.assertEqual(code, 0, stderr)
        self.assertIn("parity: PASS", stdout)

    def test_bench_json(self):
        code, stdout, stderr = run_cli([
            "bench", "--fbs", str(self.fbs), "--resolution", "96", "--repeats", "1", "--json",
        ])
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["command"], "bench")
        self.assertIn("fast_p50_s", payload)
        self.assertEqual(payload["repeats"], 1)

    def test_missing_input_exits_1(self):
        code, stdout, stderr = run_cli([
            "render", "--fdraw", str(self.tmp / "nope.fdraw"), "--out", str(self.tmp / "x.png"),
        ])
        self.assertEqual(code, 1)
        self.assertIn("FD_E_INPUT", stderr)
        self.assertEqual(stdout, "")

    def test_no_source_is_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            fdrender_cli.main(["render", "--out", str(self.tmp / "x.png")])
        self.assertEqual(ctx.exception.code, 2)

    def test_bad_background_is_input_error(self):
        code, _stdout, stderr = run_cli([
            "render", "--fbs", str(self.fbs), "--out", str(self.tmp / "bg.png"),
            "--background", "1,2",
        ])
        self.assertEqual(code, 1)
        self.assertIn("FD_E_USAGE", stderr)

    def test_bad_resolution_exits_1(self):
        code, _stdout, stderr = run_cli([
            "render", "--fbs", str(self.fbs), "--out", str(self.tmp / "r0.png"),
            "--resolution", "0",
        ])
        self.assertEqual(code, 1)
        self.assertIn("E_RENDER_RESOLUTION", stderr)

    def test_unsupported_document_reports_fallback(self):
        doc = synthetic_doc(shape="pencil")
        path = self.tmp / "pencil.fbs"
        path.write_text(json.dumps(doc), encoding="utf-8")
        out = self.tmp / "pencil.png"
        code, stdout, stderr = run_cli([
            "render", "--fbs", str(path), "--out", str(out), "--resolution", "96", "--json",
        ])
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["quality"], "exact")
        self.assertTrue(payload["fallback_reason"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
