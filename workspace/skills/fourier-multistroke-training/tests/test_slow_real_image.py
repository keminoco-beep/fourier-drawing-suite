#!/usr/bin/env python
"""Slow integration tests (real training image at 256px), opt-in via RUN_SLOW=1.

These are skipped by default so the normal suite stays fast (<90 s).  Enable with::

    $env:RUN_SLOW = "1"
    python -m unittest discover -s tests -v

They use a real image from ``<workspace>/training/input/`` when present; if no
suitable image exists the tests skip rather than fail.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest

import numpy as np

try:  # imported as a top-level module by ``unittest discover -s tests``
    import _support as S
except ImportError:  # imported as ``tests.test_slow_real_image``
    from tests import _support as S

RUN_SLOW = os.environ.get("RUN_SLOW", "") == "1"
WORK_RES = 256
GATE_TOL = 1e-3

#: Small real images (keep the slow test near ~30-60 s for both runs).
CANDIDATE_IMAGES = [
    os.path.join(S.WORKSPACE_ROOT, "training", "input", "Lvpi5.jpg"),
    os.path.join(S.WORKSPACE_ROOT, "training", "input", "ChibiMaid_user.png"),
    os.path.join(S.WORKSPACE_ROOT, "training", "input", "Sunflowers_user.jpg"),
]


def _pick_image():
    for path in CANDIDATE_IMAGES:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    return None


@unittest.skipUnless(RUN_SLOW, "slow real-image test; set RUN_SLOW=1 to enable")
class TestRealImageGradientGate(unittest.TestCase):
    """Gate on a real illustration: unsafe endpoints rejected, metrics reported."""

    @classmethod
    def setUpClass(cls):
        image = _pick_image()
        if image is None:
            raise unittest.SkipTest(f"no real test image under {S.WORKSPACE_ROOT}/training/input")
        cls.image = image
        cls._tmp = tempfile.TemporaryDirectory(prefix="slow_gradient_")
        cls.addClassCleanup(cls._tmp.cleanup)
        root = cls._tmp.name
        cls.input_png = image
        cls.outdir = os.path.join(root, "out")
        cls.seconds = None

    def _run_gate(self, gate):
        outdir = os.path.join(self._tmp.name, "out_" + gate)
        args = ["--input", self.input_png, "--outdir", outdir,
                "--work-res", str(WORK_RES), "--upper-gradient",
                "--upper-color-mode", "source", "--pass4"]
        if gate == "off":
            args += ["--gradient-gate", "off"]
        started = time.time()
        S.run_layered_ok(self, args, timeout=600, label=f"real image gate={gate}")
        self.seconds = time.time() - started
        report = S.load_json(os.path.join(outdir, "layered_base_report.json"))
        strokes = S.load_json(os.path.join(outdir, "layered_base_strokes.json"))
        rgb = S.load_rgb(self.input_png, WORK_RES)
        deltas = S.reconstruct_gradient_deltas(strokes, rgb)
        return {"report": report, "strokes": strokes,
                "rows": [r for r in deltas if r["d_end"] is not None],
                "png": os.path.join(outdir, f"layered_base_R{WORK_RES}.png")}

    def test_gate_on_real_image(self):
        defaults = S.argparse_defaults(S.DEFAULT_LAYERED)
        gate = float(defaults.get("--gradient-max-delta", S.GATE_DEFAULT_MAX_DELTA))
        run = self._run_gate("on")
        rows = run["rows"]
        self.assertGreater(len(rows), 0, "no gradient strokes on the real image")
        report = run["report"]
        # v1.37: silhouette endpoint trimming can leave 2-point gradient
        # strokes; their saved gradient fields are authoritative.
        unclipped = [r for r in rows if r.get("points", 3) >= 3]
        clipped = [r for r in rows if r.get("points", 3) < 3]
        strict = [r for r in unclipped if r["d_end"] > gate + GATE_TOL]
        strict_clipped = [r for r in clipped
                          if r["stored_max"] > gate + GATE_TOL]
        print(f"[slow] {os.path.basename(self.image)} gate=on "
              f"step_diag={S.has_step_diagnostics(report)} "
              f"gradients={len(rows)} clipped={len(clipped)} max_d_end="
              f"{max(r['d_end'] for r in rows):.4f} delta>gate={len(strict)} "
              f"seconds={self.seconds:.1f}")
        self.assertFalse(
            strict,
            f"{len(strict)} accepted gradient stroke(s) exceed --gradient-max-delta "
            f"{gate} on a real image (strict contract)")
        self.assertFalse(
            strict_clipped,
            f"{len(strict_clipped)} clipped gradient stroke(s) have stored delta "
            f"above --gradient-max-delta {gate}")
        for key in ("gradient_strokes", "gradient_rejected_delta",
                    "gradient_rejected_edge", "gradient_mean_delta",
                    "gradient_max_delta", "stray_color_px"):
            self.assertIn(key, report)
        self.assertGreater(int(report["gradient_rejected_delta"]) +
                           int(report["gradient_rejected_edge"]), 0,
                           "real image produced no rejected gradient candidates")
        self.assertGreater(int(report["strokes_out"]), 0)
        # Real photos/illustrations can legitimately keep a handful of white-ish
        # pixels inside the foreground; only runaway white holes are a defect.
        # v1.37: when the silhouette is enabled the loose fg mask keeps the soft
        # halo, so the un-painted halo ring is expected to be white.
        if report.get("silhouette_on"):
            fg_px = int(S.fg_mask(S.load_rgb(self.input_png, WORK_RES)).sum())
            self.assertGreater(fg_px, 0)
            expected = 1.0 - float(report["silhouette_area_frac"])
            self.assertAlmostEqual(int(report["white_px_in_fg"]) / fg_px,
                                   expected, delta=0.01)
        else:
            width, height = int(report["size"][0]), int(report["size"][1])
            white_budget = max(50, int(0.001 * width * height))
            self.assertLessEqual(int(report["white_px_in_fg"]), white_budget,
                                 f"white_px_in_fg={report['white_px_in_fg']} "
                                 f"exceeds 0.1% of the image ({white_budget} px)")

    def test_gate_off_real_image_has_unsafe_gradients(self):
        defaults = S.argparse_defaults(S.DEFAULT_LAYERED)
        gate = float(defaults.get("--gradient-max-delta", S.GATE_DEFAULT_MAX_DELTA))
        run = self._run_gate("off")
        rows = run["rows"]
        self.assertGreater(len(rows), 0)
        unsafe = [r for r in rows if r["d_end"] > gate + GATE_TOL]
        print(f"[slow] {os.path.basename(self.image)} gate=off "
              f"gradients={len(rows)} unsafe={len(unsafe)} "
              f"seconds={self.seconds:.1f}")
        self.assertGreater(len(unsafe), 0)


@unittest.skipUnless(RUN_SLOW, "slow real-image test; set RUN_SLOW=1 to enable")
class TestRealImageTextureFlat(unittest.TestCase):
    """v1.34 texture-flat pass on a real illustration (opt-in, 256px)."""

    @classmethod
    def setUpClass(cls):
        image = _pick_image()
        if image is None:
            raise unittest.SkipTest(f"no real test image under "
                                    f"{S.WORKSPACE_ROOT}/training/input")
        cls.image = image
        cls._tmp = tempfile.TemporaryDirectory(prefix="slow_texture_")
        cls.addClassCleanup(cls._tmp.cleanup)

    def test_texture_flat_real_image(self):
        if not S.has_cli_arg("--texture-flat"):
            self.skipTest("texture-flat not implemented in this layered_base.py")
        outdir = os.path.join(self._tmp.name, "out_texture")
        args = ["--input", self.image, "--outdir", outdir,
                "--work-res", str(WORK_RES),
                "--base-color-mode", "source", "--base-blur", "4",
                "--coarse", "12", "--texture-flat",
                "--texture-flat-count", "2000",
                "--texture-flat-ref-std", "0.015"]
        started = time.time()
        S.run_layered_ok(self, args, timeout=600, label="real image texture-flat")
        seconds = time.time() - started
        report = S.load_json(os.path.join(outdir, "layered_base_report.json"))
        strokes = S.load_json(os.path.join(outdir, "layered_base_strokes.json"))
        tex = S.texture_flat_strokes(strokes)
        print(f"[slow] {os.path.basename(self.image)} texture-flat: "
              f"strokes={report['strokes_out']} texture={len(tex)} "
              f"candidates={report['texture_flat_candidates']} "
              f"seconds={seconds:.1f}")
        self.assertEqual(report["base_color_mode"], "source")
        self.assertGreater(len(tex), 0)
        self.assertLessEqual(len(tex), 2000)
        self.assertEqual(len(tex), int(report["texture_flat_strokes"]))
        self.assertLessEqual(int(report["strokes_out"]), S.MAX_TOTAL_STROKES)
        self.assertGreaterEqual(int(report["texture_flat_candidates"]), len(tex))
        params = report["texture_flat_params"]
        self.assertEqual(params["layer"], 19)
        self.assertAlmostEqual(float(params["ref_std"]), 0.015)
        scale = float(max(report["size"]))
        len_lo, len_hi = S.expected_texture_length_px(scale)
        wid_lo, wid_hi = S.expected_texture_width_px(scale)
        for stroke in tex[:: max(1, len(tex) // 100)]:
            self.assertEqual(stroke["layer"], 19)
            self.assertEqual(stroke["alpha"], 1.0)
            points = np.asarray(stroke["points"], dtype=np.float64)
            self.assertGreaterEqual(float(points.min()), 0.0)
            self.assertLessEqual(float(points.max()), 1.0)
            length = float(np.linalg.norm((points[0] - points[-1]) * scale))
            width = float(np.asarray(stroke["widths"], dtype=np.float64)[0] * scale)
            self.assertGreaterEqual(length, len_lo - 1e-3)
            self.assertLessEqual(length, len_hi + 1e-3)
            self.assertGreaterEqual(width, wid_lo - 1e-3)
            self.assertLessEqual(width, wid_hi + 1e-3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
