#!/usr/bin/env python
"""v1.34 ``--texture-flat`` fine-texture pass tests.

The v1.34 feature is opt-in: ``layered_base.py`` may add a dense layer of short,
thin, reference-coloured strokes (kind ``oil_flat_texture``, layer 19) over
areas where the *current render* is locally flat (3x3 luma std below
``--texture-flat-lstd``).  It breaks the large flat colour-band blocks of the
layered underpainting (user's red-circle A region) without touching the default
v1.33 behaviour.

Frozen interface (Lead/fixer-core, 2026-10-24):
  * kinds: ``oil_flat_texture``; layer default 19; alpha = 1;
  * default OFF -> zero texture strokes, report ``texture_flat_params == None``;
  * sizes are given in px at a 1280 long side and scaled by ``rs = max(H,W)/1280``;
  * report: ``texture_flat_strokes`` / ``texture_flat_candidates`` /
    ``texture_flat_params`` (diagnostics requested/auto/planned/candidates/
    budget/emitted/capped_100k/spacing_px/ref_std_thr/flat_frac_fg);
  * total strokes are hard-capped at ``_MAX_STROKES = 100000``.

The pass ranks locally-flat candidates by reference texture energy, so on a
synthetic image whose render is flat on both halves the strokes deliberately go
to the *textured* reference side (that is where banding is visible).  The
"only locally flat areas" contract is therefore asserted at unit level with a
controlled render (flat left / noisy right => >=70% on the flat side); the CLI
tests assert report/geometry/budget integration.

Run from the skill root::

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import unittest

import numpy as np

try:  # imported as a top-level module by ``unittest discover -s tests``
    import _support as S
except ImportError:  # imported as ``tests.test_texture_flat``
    from tests import _support as S

LB = S.import_layered_base()
TEX_KIND = S.TEXTURE_FLAT_KIND
MAX_TOTAL = S.MAX_TOTAL_STROKES
REF_RES = S.TEXTURE_FLAT_REF_RES
HAS_TEXTURE_API = (hasattr(LB, "texture_flat_pass")
                   and S.has_cli_arg("--texture-flat")
                   and S.has_cli_arg("--texture-flat-count"))

FROZEN_TEXTURE_DEFAULTS = {
    "--texture-flat": False,
    "--texture-flat-count": 8000,
    "--texture-flat-spacing": 2.5,
    "--texture-flat-len-min": 5.0,
    "--texture-flat-len-max": 9.0,
    "--texture-flat-width-min": 2.5,
    "--texture-flat-width-max": 3.5,
    "--texture-flat-lstd": 0.01,
    "--texture-flat-ref-std": 0.0,
    "--texture-flat-layer": 19,
    "--texture-flat-jitter": 0.01,
}

BASE_MODE_CHOICES_V135 = ["toned", "none", "single", "quantized", "source"]
FROZEN_BASE_V134_DEFAULTS = {
    "--base-blur": 4.0,
    "--base-gradient": False,
    "--base-width-factor": 1.5,
    "--base-length-factor": 2.0,
}


def _defaults():
    return S.argparse_defaults(S.DEFAULT_LAYERED)


def _scene_path(tmpdir):
    path = os.path.join(tmpdir, "flat_noise.png")
    S.write_scene(path, S.scene_flat_noise(144, 192))
    return path


def _stroke_length_px(stroke, scale):
    points = np.asarray(stroke["points"], dtype=np.float64) * scale
    return float(np.linalg.norm(points[0] - points[-1]))


def _stroke_width_px(stroke, scale):
    return float(np.asarray(stroke["widths"], dtype=np.float64)[0] * scale)


@unittest.skipUnless(HAS_TEXTURE_API,
                     "texture-flat API not present in this layered_base.py")
class TestTextureFlatInterface(unittest.TestCase):
    """``--help`` and frozen defaults of the v1.34 opt-in flags."""

    @classmethod
    def setUpClass(cls):
        cls.help_text = S.script_help(S.DEFAULT_LAYERED)
        cls.defaults = _defaults()
        cls.choices = S.argparse_choices(S.DEFAULT_LAYERED)

    def test_help_lists_texture_flat_options(self):
        for option in FROZEN_TEXTURE_DEFAULTS:
            self.assertIn(option, self.help_text,
                          f"{option} missing from layered_base.py --help")

    def test_frozen_texture_flat_defaults(self):
        problems = []
        for option, expected in FROZEN_TEXTURE_DEFAULTS.items():
            if option not in self.defaults:
                problems.append(f"{option}: missing from argparse")
            elif self.defaults[option] != expected:
                problems.append(f"{option}: default {self.defaults[option]!r} "
                                f"!= frozen {expected!r}")
        self.assertFalse(problems, "texture-flat defaults drifted:\n" +
                         "\n".join(problems))

    def test_frozen_v134_base_defaults(self):
        problems = []
        for option, expected in FROZEN_BASE_V134_DEFAULTS.items():
            if option not in self.defaults:
                problems.append(f"{option}: missing from argparse")
            elif self.defaults[option] != expected:
                problems.append(f"{option}: default {self.defaults[option]!r} "
                                f"!= frozen {expected!r}")
        if self.choices.get("--base-color-mode") != BASE_MODE_CHOICES_V135:
            problems.append(f"--base-color-mode choices: "
                            f"{self.choices.get('--base-color-mode')!r} "
                            f"!= {BASE_MODE_CHOICES_V135}")
        self.assertFalse(problems, "v1.34 pass1 defaults drifted:\n" +
                         "\n".join(problems))


@unittest.skipUnless(HAS_TEXTURE_API,
                     "texture-flat API not present in this layered_base.py")
class TestTextureFlatUnit(unittest.TestCase):
    """Direct ``texture_flat_pass`` invariants (controlled render, deterministic)."""

    H, W = 96, 128
    COUNT = 400

    @classmethod
    def setUpClass(cls):
        cls.rgb = S.scene_flat_noise(cls.H, cls.W)
        cls.fg = S.fg_mask(cls.rgb)
        cls.ren = S.flat_render(cls.H, cls.W)      # flat left, noisy right
        cls.scale = float(max(cls.H, cls.W))
        cls.rs = cls.scale / REF_RES
        cls.strokes, cls.info = cls._pass(np.random.default_rng(7), cls.COUNT)

    @classmethod
    def _pass(cls, rng, count, *, hard_cap=MAX_TOTAL, ref_std_thr=0.0, ren=None):
        return LB.texture_flat_pass(
            cls.rgb, cls.fg, cls.ren if ren is None else ren, rng,
            count=count, spacing=2.5, lstd_thr=0.01, ref_std_thr=ref_std_thr,
            len_min=5.0, len_max=9.0, width_min=2.5, width_max=3.5,
            layer=19, jitter=0.01, hard_cap=hard_cap)

    def test_emits_requested_strokes_and_info(self):
        self.assertEqual(len(self.strokes), self.COUNT)
        self.assertEqual(len(self.strokes), int(self.info["emitted"]))
        self.assertGreaterEqual(int(self.info["candidates"]), self.COUNT)
        self.assertEqual(int(self.info["budget"]), MAX_TOTAL)
        self.assertFalse(bool(self.info["capped_100k"]))
        self.assertAlmostEqual(float(self.info["flat_frac_fg"]), 0.5, delta=0.1,
                               msg="flat left half should be ~half of the fg")
        for key in ("requested", "auto", "planned", "candidates", "budget",
                    "emitted", "capped_100k", "spacing_px", "ref_std_thr",
                    "flat_frac_fg"):
            self.assertIn(key, self.info)

    def test_stroke_fields(self):
        for stroke in self.strokes:
            self.assertEqual(stroke["kind"], TEX_KIND)
            self.assertEqual(stroke["layer"], 19)
            self.assertEqual(stroke["alpha"], 1.0)
            self.assertIs(stroke["closed"], False)
            self.assertNotIn("gradient", stroke)
            points = np.asarray(stroke["points"], dtype=np.float64)
            self.assertEqual(points.shape, (3, 2))
            self.assertGreaterEqual(float(points.min()), 0.0)
            self.assertLessEqual(float(points.max()), 1.0)
            widths = np.asarray(stroke["widths"], dtype=np.float64)
            self.assertEqual(widths.shape, (3,))
            self.assertTrue(np.all(widths > 0.0))
            np.testing.assert_allclose(widths, widths[0], atol=0.0)
            color = np.asarray(stroke["color"], dtype=np.float64)
            self.assertEqual(color.shape, (3,))
            self.assertGreaterEqual(float(color.min()), 0.0)
            self.assertLessEqual(float(color.max()), 1.0)

    def test_scaled_geometry_matches_defaults(self):
        len_lo, len_hi = S.expected_texture_length_px(self.scale)
        wid_lo, wid_hi = S.expected_texture_width_px(self.scale)
        for stroke in self.strokes:
            length = _stroke_length_px(stroke, self.scale)
            self.assertGreaterEqual(length, len_lo - 1e-3)
            self.assertLessEqual(length, len_hi + 1e-3)
            width = _stroke_width_px(stroke, self.scale)
            self.assertGreaterEqual(width, wid_lo - 1e-3)
            self.assertLessEqual(width, wid_hi + 1e-3)

    def test_only_locally_flat_areas_are_painted(self):
        """Flat left / noisy right render -> >=70% of strokes on the flat side."""
        centers = np.array([s["points"][1][0] for s in self.strokes])
        flat_side_ratio = float((centers < 0.5).mean())
        print(f"[texture-flat] unit flat-side ratio={flat_side_ratio:.4f} "
              f"candidates={self.info['candidates']} flat_frac={self.info['flat_frac_fg']}")
        self.assertGreaterEqual(flat_side_ratio, 0.70,
                                "texture pass painted the non-flat render side")

    def test_ref_std_gate_restricts_to_textured_reference(self):
        flat_ren = np.full_like(self.rgb, 0.35)
        strokes, info = self._pass(np.random.default_rng(7), 200,
                                   ref_std_thr=0.02, ren=flat_ren)
        self.assertGreater(len(strokes), 0)
        centers = np.array([s["points"][1][0] for s in strokes])
        textured_side_ratio = float((centers >= 0.5).mean())
        print(f"[texture-flat] ref_std gate: strokes={len(strokes)} "
              f"textured-side ratio={textured_side_ratio:.4f}")
        self.assertGreaterEqual(textured_side_ratio, 0.70,
                                "--texture-flat-ref-std must only paint where "
                                "the reference has local texture")

    def test_hard_cap_is_enforced(self):
        strokes, info = self._pass(np.random.default_rng(7), 1000, hard_cap=37)
        self.assertEqual(len(strokes), 37)
        self.assertEqual(int(info["emitted"]), 37)
        self.assertEqual(int(info["budget"]), 37)
        self.assertTrue(bool(info["capped_100k"]))

    def test_zero_budget_returns_nothing(self):
        strokes, info = self._pass(np.random.default_rng(7), 1000, hard_cap=0)
        self.assertEqual(strokes, [])
        self.assertEqual(int(info["emitted"]), 0)
        self.assertTrue(bool(info["capped_100k"]))

    def test_auto_count_scales_with_canvas_area(self):
        strokes, info = self._pass(np.random.default_rng(7), 0)
        expected_planned = int(round(6000.0 * self.H * self.W / (1536.0 * 1536.0)))
        self.assertTrue(bool(info["auto"]))
        self.assertEqual(int(info["planned"]), expected_planned)
        self.assertEqual(len(strokes), min(expected_planned, int(info["candidates"])))

    def test_deterministic_for_same_seed(self):
        again, _ = self._pass(np.random.default_rng(7), self.COUNT)
        self.assertEqual(len(again), len(self.strokes))
        for a, b in zip(self.strokes, again):
            np.testing.assert_allclose(np.asarray(a["points"]),
                                       np.asarray(b["points"]), atol=0.0)
            np.testing.assert_allclose(np.asarray(a["widths"]),
                                       np.asarray(b["widths"]), atol=0.0)
            np.testing.assert_allclose(np.asarray(a["color"]),
                                       np.asarray(b["color"]), atol=0.0)


class _TextureFlatCLIBase(unittest.TestCase):
    WORK_RES = 128

    @classmethod
    def setUpClass(cls):
        if not HAS_TEXTURE_API:
            raise unittest.SkipTest("texture-flat API not present")
        cls._tmp = tempfile.TemporaryDirectory(prefix="texture_flat_cli_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.input_png = _scene_path(cls._tmp.name)
        cls.outdir = None

    @classmethod
    def _run(cls, extra, label):
        cls.outdir = os.path.join(cls._tmp.name, "out_" + label)
        args = ["--input", cls.input_png, "--outdir", cls.outdir,
                "--work-res", str(cls.WORK_RES)] + list(extra)
        S.run_layered_ok(cls, args, label=label)
        report = S.load_json(os.path.join(cls.outdir, "layered_base_report.json"))
        strokes = S.load_json(os.path.join(cls.outdir, "layered_base_strokes.json"))
        return report, strokes


class TestTextureFlatCLIDefaultOff(_TextureFlatCLIBase):
    """Default OFF must keep the v1.33 behaviour byte-for-byte in shape."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.report, cls.strokes = cls._run([], "default_off")

    def test_default_off_emits_no_texture_strokes(self):
        self.assertEqual(int(self.report["texture_flat_strokes"]), 0)
        self.assertIsNone(self.report["texture_flat_params"])
        self.assertIsNone(self.report["texture_flat_candidates"])
        self.assertEqual(S.texture_flat_strokes(self.strokes), [])
        pass_total = sum(int(self.report[key]) for key in
                         ("pass1_full", "pass2_local", "pass3_local", "pass4_local"))
        self.assertEqual(pass_total + int(self.report["detail"]),
                         int(self.report["strokes_out"]))
        self.assertGreaterEqual(int(self.report["strokes_out"]), 1)
        self.assertTrue(math.isfinite(float(self.report["psnr"])))


class TestTextureFlatCLIOn(_TextureFlatCLIBase):
    """Opt-in run: report echo, stroke fields, budget accounting."""

    COUNT = 300

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.report, cls.strokes = cls._run(
            ["--texture-flat", "--texture-flat-count", str(cls.COUNT)],
            "texture_on")

    def test_report_fields_and_param_echo(self):
        report = self.report
        tex = int(report["texture_flat_strokes"])
        self.assertEqual(tex, self.COUNT)
        self.assertGreaterEqual(int(report["texture_flat_candidates"]), tex)
        params = report["texture_flat_params"]
        self.assertIsNotNone(params)
        self.assertEqual(int(params["count"]), self.COUNT)
        self.assertAlmostEqual(float(params["spacing"]), 2.5)
        self.assertAlmostEqual(float(params["lstd"]), 0.01)
        self.assertAlmostEqual(float(params["ref_std"]), 0.0)
        self.assertEqual([float(v) for v in params["len_px"]], [5.0, 9.0])
        self.assertEqual([float(v) for v in params["width_px"]], [2.5, 3.5])
        self.assertEqual(int(params["layer"]), 19)
        self.assertAlmostEqual(float(params["jitter"]), 0.01)
        self.assertEqual(int(params["requested"]), self.COUNT)
        self.assertEqual(int(params["emitted"]), tex)
        self.assertFalse(bool(params["capped_100k"]))
        self.assertEqual(int(report["strokes_out"]),
                         sum(int(report[key]) for key in
                             ("pass1_full", "pass2_local", "pass3_local",
                              "pass4_local")) + int(report["detail"]) + tex)
        self.assertEqual(int(params["budget"]), MAX_TOTAL - (int(report["strokes_out"]) - tex))
        self.assertLessEqual(int(report["strokes_out"]), MAX_TOTAL)
        self.assertTrue(math.isfinite(float(report["psnr"])))

    def test_texture_strokes_geometry_and_fields(self):
        tex = S.texture_flat_strokes(self.strokes)
        self.assertEqual(len(tex), int(self.report["texture_flat_strokes"]))
        self.assertGreater(len(tex), 0)
        scale = float(max(self.report["size"]))  # L = max(W, H)
        len_lo, len_hi = S.expected_texture_length_px(scale)
        wid_lo, wid_hi = S.expected_texture_width_px(scale)
        for stroke in tex:
            self.assertEqual(stroke["layer"], 19)
            self.assertEqual(stroke["alpha"], 1.0)
            self.assertIs(stroke["closed"], False)
            points = np.asarray(stroke["points"], dtype=np.float64)
            self.assertEqual(points.shape, (3, 2))
            self.assertGreaterEqual(float(points.min()), 0.0)
            self.assertLessEqual(float(points.max()), 1.0)
            color = np.asarray(stroke["color"], dtype=np.float64)
            self.assertGreaterEqual(float(color.min()), 0.0)
            self.assertLessEqual(float(color.max()), 1.0)
            length = _stroke_length_px(stroke, scale)
            width = _stroke_width_px(stroke, scale)
            self.assertGreaterEqual(length, len_lo - 1e-3)
            self.assertLessEqual(length, len_hi + 1e-3)
            self.assertGreaterEqual(width, wid_lo - 1e-3)
            self.assertLessEqual(width, wid_hi + 1e-3)


class TestTextureFlatCLIRecipe(_TextureFlatCLIBase):
    """Lead/fixer recommended v1.34 recipe smoke: source ground, no --pass4."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.report, cls.strokes = cls._run(
            ["--base-color-mode", "source", "--base-blur", "4",
             "--coarse", "12", "--texture-flat",
             "--texture-flat-count", "400",
             "--texture-flat-ref-std", "0.015"],
            "recipe")

    def test_recipe_report(self):
        report = self.report
        self.assertEqual(report["base_color_mode"], "source")
        self.assertAlmostEqual(float(report["base_blur"]), 4.0)
        self.assertFalse(bool(report["base_gradient"]))
        self.assertAlmostEqual(float(report["base_width_factor"]), 1.5)
        self.assertAlmostEqual(float(report["base_length_factor"]), 2.0)
        self.assertGreater(int(report["texture_flat_strokes"]), 0)
        self.assertLessEqual(int(report["texture_flat_strokes"]), 400)
        self.assertAlmostEqual(float(report["texture_flat_params"]["ref_std"]), 0.015)
        self.assertEqual(int(report["pass4_local"]), 0)  # recipe drops --pass4
        self.assertLessEqual(int(report["strokes_out"]), MAX_TOTAL)
        self.assertTrue(math.isfinite(float(report["psnr"])))
        self.assertTrue(math.isfinite(float(report["ssim"])))


class TestTextureFlatCLIBudget(_TextureFlatCLIBase):
    """Huge requested count: warning + truncation, total stays <= 100k."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.proc = None
        cls.outdir = os.path.join(cls._tmp.name, "out_budget")
        args = ["--input", cls.input_png, "--outdir", cls.outdir,
                "--work-res", str(cls.WORK_RES),
                "--texture-flat", "--texture-flat-count", "200000"]
        cls.proc = S.run_layered(args, timeout=300)
        cls.report = S.load_json(os.path.join(cls.outdir, "layered_base_report.json"))
        cls.strokes = S.load_json(os.path.join(cls.outdir, "layered_base_strokes.json"))

    def test_total_stroke_cap_and_warning(self):
        self.assertEqual(self.proc.returncode, 0,
                         f"layered_base failed: {self.proc.stderr[-2000:]}")
        report = self.report
        self.assertLessEqual(int(report["strokes_out"]), MAX_TOTAL)
        params = report["texture_flat_params"]
        self.assertEqual(int(params["requested"]), 200000)
        self.assertLessEqual(int(report["texture_flat_strokes"]), int(params["budget"]))
        self.assertLessEqual(int(report["strokes_out"]), MAX_TOTAL)
        self.assertTrue(bool(params["capped_100k"]))
        self.assertIn("WARNING texture-flat count", self.proc.stdout,
                      "the 100k truncation warning is missing from stdout")


if __name__ == "__main__":
    unittest.main(verbosity=2)
