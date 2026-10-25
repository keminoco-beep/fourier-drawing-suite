#!/usr/bin/env python
"""v1.35 human-like underpainting tests: pure-colour single coat / no ground.

Frozen interface (Lead 2026-10-24):
  * ``--base-color-mode {toned,none,single,quantized,source}`` default ``toned``;
  * toned = ONE flat colour, ONE thin coat: kind ``base_toned``, layer 0, alpha 1,
    no gradient, no per-stroke colour jitter; row sweep at ``--base-coat-angle``,
    pitch ``--base-coat-spacing`` (0 => ``--coarse``), width
    ``--base-coat-width-factor`` x pitch (1.04), midline jitter 0.5px;
  * ``--base-color auto|#RRGGBB|"r,g,b"`` (auto = whole-image per-channel median);
  * ``none`` = no pass1 strokes; legacy single/quantized/source unchanged;
  * report flat keys: base_color_mode/base_color/base_coat_angle/base_coat_spacing/
    base_coat_width_factor/base_coat_jitter/base_coat_width/base_gradient_strokes/
    base_strokes/base_coverage/base_overlap_frac/base_color_std;
  * new default error percentiles: --mid-err-pct 60 --fine-err-pct 45
    --pass4-err-pct 45.

Raster coverage/overlap is measured with ``_support.stroke_footprints`` (the
skill's own renderer, one stroke at a time, magic colour) - the task requires the
FACTUAL fg coverage >= 99% and overlap <= 5%, not the analytic formula.

Run from the skill root::

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import math
import os
import tempfile
import unittest

import numpy as np
from scipy import ndimage as ndi

try:  # imported as a top-level module by ``unittest discover -s tests``
    import _support as S
except ImportError:  # imported as ``tests.test_base_coat``
    from tests import _support as S

WORK_RES = 128
COAT_KIND = "base_toned"
LEGACY_KIND = "base_pass1"
COVERAGE_MIN = 0.99
OVERLAP_MAX = 0.05

FROZEN_BASE_DEFAULTS = {
    "--base-color-mode": "toned",
    "--base-color": "auto",
    "--base-coat-angle": 0.0,
    "--base-coat-spacing": 0.0,
    "--base-coat-width-factor": 1.04,
    "--base-coat-jitter": 0.5,
    "--base-gradient": False,
    "--base-width-factor": 1.5,
    "--base-length-factor": 2.0,
    "--base-blur": 4.0,
    "--max-curves": 100000,
}

FROZEN_ERR_PCT_DEFAULTS = {
    "--mid-err-pct": 60.0,
    "--fine-err-pct": 45.0,
    "--pass4-err-pct": 45.0,
}

FROZEN_BASE_MODE_CHOICES = ["toned", "none", "single", "quantized", "source"]

REPORT_BASE_KEYS = [
    "base_color_mode", "base_color", "base_coat_angle", "base_coat_spacing",
    "base_coat_width_factor", "base_coat_width_factor_requested", "base_coat_jitter", "base_coat_width", "base_overlap_canvas_frac",
    "base_gradient_strokes", "base_strokes", "base_coverage",
    "base_overlap_frac", "base_color_std", "base_coat_pattern",
    "capped_100k", "cap_adjustments",
    "pre_cap_strokes", "max_curves",
]


def _coat_strokes(strokes, kind=COAT_KIND):
    return [s for s in strokes if str(s.get("kind", "")) == kind]


def _run_scene(tmpdir, extra, scene=None, work_res=WORK_RES):
    os.makedirs(tmpdir, exist_ok=True)
    inp = os.path.join(tmpdir, "scene.png")
    S.write_scene(inp, S.scene_canvas(192, 144) if scene is None else scene)
    outdir = os.path.join(tmpdir, "out")
    S.run_layered_ok(
        None, ["--input", inp, "--outdir", outdir, "--work-res", str(work_res)]
        + list(extra), label="base-coat")
    report = S.load_json(os.path.join(outdir, "layered_base_report.json"))
    strokes = S.load_json(os.path.join(outdir, "layered_base_strokes.json"))
    rgb = S.load_rgb(inp, work_res)
    return {"report": report, "strokes": strokes, "rgb": rgb,
            "fg": S.fg_mask(rgb), "outdir": outdir, "input": inp}


def _coverage(run, kind=COAT_KIND):
    base = _coat_strokes(run["strokes"], kind)
    H, W = run["fg"].shape
    counts = S.stroke_footprints(base, H, W)
    return base, counts, S.coverage_metrics(counts, run["fg"])


def _interior(covered):
    return ndi.binary_erosion(covered, iterations=1)


class TestBaseCoatInterface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.help_text = S.script_help(S.DEFAULT_LAYERED)
        cls.defaults = S.argparse_defaults(S.DEFAULT_LAYERED)
        cls.choices = S.argparse_choices(S.DEFAULT_LAYERED)

    def test_help_lists_new_options(self):
        for option in list(FROZEN_BASE_DEFAULTS)[:6] + ["--base-color-mode"]:
            self.assertIn(option, self.help_text,
                          f"{option} missing from layered_base.py --help")

    def test_frozen_base_defaults(self):
        problems = []
        for option, expected in FROZEN_BASE_DEFAULTS.items():
            if option not in self.defaults:
                problems.append(f"{option}: missing")
            elif self.defaults[option] != expected:
                problems.append(f"{option}: {self.defaults[option]!r} != {expected!r}")
        self.assertFalse(problems, "\n".join(problems))

    def test_frozen_err_pct_defaults(self):
        problems = []
        for option, expected in FROZEN_ERR_PCT_DEFAULTS.items():
            if self.defaults.get(option) != expected:
                problems.append(f"{option}: {self.defaults.get(option)!r} != {expected!r}")
        self.assertFalse(problems, "\n".join(problems))

    def test_base_mode_choices(self):
        self.assertEqual(self.choices.get("--base-color-mode"), FROZEN_BASE_MODE_CHOICES)
        self.assertEqual(self.defaults.get("--base-color-mode"), "toned")


class TestTonedDefault(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="base_coat_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.scene_run = _run_scene(cls._tmp.name, [])
        cls.report = cls.scene_run["report"]
        cls.base, cls.counts, cls.metrics = _coverage(cls.scene_run)
        cls.defaults = S.argparse_defaults(S.DEFAULT_LAYERED)

    def test_default_mode_is_toned(self):
        report = self.report
        self.assertEqual(report["base_color_mode"], "toned")
        self.assertEqual(report["base_coat_pattern"], "wash")
        self.assertGreater(int(report["base_strokes"]), 0)
        self.assertEqual(int(report["base_strokes"]), len(self.base))
        self.assertEqual(int(report["pass1_full"]), len(self.base))
        for key in REPORT_BASE_KEYS:
            self.assertIn(key, report, f"report missing {key}")
        self.assertEqual([len(report["base_color"])], [3])

    def test_report_coat_geometry(self):
        report = self.report
        # v1.36 default ground is the wash (diagonal/curved sweeps):
        # the row-coat spacing/width are not used, but wash_angle/brush are.
        self.assertEqual(report["base_coat_pattern"], "wash")
        self.assertAlmostEqual(float(report["base_coat_angle"]),
                               float(self.defaults["--wash-angle"]))
        self.assertIsNone(report["base_coat_spacing"])
        self.assertGreater(float(report["base_coat_width"]), 0.0)
        self.assertAlmostEqual(float(report["base_coat_width_factor_requested"]), 1.04)
        self.assertGreaterEqual(float(report["base_coverage"]), COVERAGE_MIN)
        self.assertLessEqual(float(report["base_overlap_frac"]), 0.15 + 1e-9)

    def test_single_pure_colour_no_gradient(self):
        report = self.report
        stats = S.stroke_color_stats(self.base)
        self.assertEqual(stats["unique"], 1, "toned base must be ONE flat colour")
        self.assertLessEqual(max(stats["std"]), 1e-6)
        self.assertEqual(int(report["base_color_std"]), 0)
        expected = np.asarray(report["base_color"], dtype=np.float64)
        for stroke in self.base:
            self.assertNotIn("gradient", stroke)
            self.assertEqual(int(stroke["layer"]), 0)
            self.assertEqual(float(stroke["alpha"]), 1.0)
            np.testing.assert_allclose(np.asarray(stroke["color"], dtype=np.float64),
                                       expected, atol=1e-5)
        self.assertEqual(int(report["base_gradient_strokes"]), 0)

    def test_single_coat_coverage_and_overlap(self):
        metrics = self.metrics
        print(f"[base-coat] raster coverage={metrics['coverage']:.4f} "
              f"overlap/fg={metrics['overlap_frac_of_fg']:.4f} "
              f"over2={metrics['over2_px']} max_count={metrics['max_count']} "
              f"report(cov={self.report['base_coverage']}, "
              f"ovl={self.report['base_overlap_frac']})")
        self.assertGreaterEqual(metrics["coverage"], COVERAGE_MIN,
                                "single coat must cover >=99% of the foreground")
        self.assertEqual(self.report["base_coat_pattern"], "wash")
        self.assertLessEqual(metrics["overlap_frac_of_fg"], 0.15 + 0.01,
                             "wash overlap <=15% (+1pp raster tolerance)")
        self.assertLessEqual(metrics["max_count"], 3)
        # legacy row coat must keep its old <=5% overlap / <=2 hit contract
        rows_run = _run_scene(
            os.path.join(self._tmp.name, "rows_explicit"),
            ["--base-coat-pattern", "rows"])
        rows_base = [s for s in rows_run["strokes"]
                     if str(s.get("kind", "")) == "base_toned"]
        rows_metrics = _coverage(rows_run)[2]
        self.assertEqual(rows_run["report"]["base_coat_pattern"], "rows")
        self.assertGreaterEqual(rows_metrics["coverage"], COVERAGE_MIN)
        self.assertLessEqual(rows_metrics["overlap_frac_of_fg"], 0.05 + 0.01)
        self.assertLessEqual(rows_metrics["max_count"], 2)

    def test_base_only_render_is_flat(self):
        # v1.36 wash sweeps are polylines with varying widths, so the old
        # 3-point interior test is replaced by a full-coverage alpha mask:
        # render all ground strokes with a magic colour and keep only pixels
        # that reached alpha >= 0.99 (no white-paper antialiasing).
        H, W = self.scene_run["fg"].shape
        magic = []
        for stroke in self.base:
            probe = dict(stroke)
            probe["color"] = [1.0, 0.0, 1.0]
            probe["alpha"] = 1.0
            for key in ("gradient", "color_gradient", "color_start", "color_end"):
                probe.pop(key, None)
            probe["layer"] = 0
            magic.append(probe)
        magic_render = S.render_strokes(magic, H, W)
        full = magic_render[:, :, 1] < 1e-4
        self.assertGreater(int(full.sum()), 0)
        rendered = S.render_strokes(self.base, H, W)
        colors = np.round(np.asarray(rendered, dtype=np.float64)[full] * 255.0)
        expected = np.round(np.asarray(self.report["base_color"],
                                       dtype=np.float64) * 255.0)
        flat = np.all(np.abs(colors - expected) <= 1.0, axis=1)
        self.assertGreaterEqual(float(flat.mean()) if flat.size else 0.0, 0.995,
                                "base-only render must be one flat colour")

    def test_default_recipe_still_paints(self):
        report = self.report
        self.assertGreater(int(report["strokes_out"]), len(self.base))
        self.assertGreater(int(report["pass2_local"]) + int(report["pass3_local"]), 0)
        self.assertTrue(math.isfinite(float(report["psnr"])))
        self.assertGreater(float(report["psnr"]), 8.0)
        # v1.35 hard-cap contract: default run must stay <= 100k
        self.assertLessEqual(int(report["strokes_out"]), 100000)
        self.assertIn("capped_100k", report)
        self.assertFalse(bool(report["capped_100k"]))
        self.assertIsNone(report["cap_adjustments"])
        self.assertEqual(int(report["pre_cap_strokes"]), int(report["strokes_out"]))
        self.assertEqual(int(report["max_curves"]), 100000)


class TestBaseColorParsing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="base_color_")
        cls.addClassCleanup(cls._tmp.cleanup)

    def _check(self, spec, expected):
        run = _run_scene(os.path.join(self._tmp.name, "case_" + str(abs(hash(spec)))),
                         ["--base-color", spec])
        self.assertEqual(run["report"]["base_color_mode"], "toned")
        reported = np.asarray(run["report"]["base_color"], dtype=np.float64)
        np.testing.assert_allclose(reported, expected, atol=1e-5)
        base = _coat_strokes(run["strokes"])
        self.assertGreater(len(base), 0)
        for stroke in base:
            np.testing.assert_allclose(np.asarray(stroke["color"], dtype=np.float64),
                                       expected, atol=1e-5)
        self.assertLessEqual(max(S.stroke_color_stats(base)["std"]), 1e-6)

    def test_hex_rrggbb(self):
        self._check("#3366CC", [0.2, 0.4, 0.8])

    def test_rgb_0_255(self):
        self._check("10,200,30", [10 / 255.0, 200 / 255.0, 30 / 255.0])


class TestBaseCoatAngle(unittest.TestCase):
    def test_angle_90_is_vertical(self):
        with tempfile.TemporaryDirectory(prefix="base_angle_") as td:
            run = _run_scene(td, ["--base-coat-pattern", "rows",
                                  "--base-coat-angle", "90"])
        report = run["report"]
        self.assertAlmostEqual(float(report["base_coat_angle"]), 90.0)
        base, _, metrics = _coverage(run)
        self.assertGreater(len(base), 0)
        verticality = []
        for stroke in base:
            pts = np.asarray(stroke["points"], dtype=np.float64)
            d = pts[-1] - pts[0]
            verticality.append(abs(float(d[1])) / max(1e-9, float(np.hypot(*d))))
        self.assertGreaterEqual(float(np.mean(verticality)), 0.9)
        self.assertGreaterEqual(metrics["coverage"], COVERAGE_MIN)


class TestNoBase(unittest.TestCase):
    def test_none_mode_has_no_underpainting(self):
        with tempfile.TemporaryDirectory(prefix="base_none_") as td:
            run = _run_scene(td, ["--base-color-mode", "none"])
        report = run["report"]
        self.assertEqual(report["base_color_mode"], "none")
        self.assertEqual(int(report["base_strokes"]), 0)
        self.assertEqual(int(report["pass1_full"]), 0)
        for key in ("base_color", "base_coat_angle", "base_coat_spacing",
                    "base_coat_width", "base_coat_width_factor", "base_coat_jitter"):
            self.assertIsNone(report[key], f"{key} should be None in none mode")
        self.assertFalse(_coat_strokes(run["strokes"]))
        self.assertFalse(_coat_strokes(run["strokes"], LEGACY_KIND))
        self.assertGreater(int(report["strokes_out"]), 0)
        self.assertTrue(math.isfinite(float(report["psnr"])))


class TestLegacyModes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="base_legacy_")
        cls.addClassCleanup(cls._tmp.cleanup)

    def test_quantized_legacy_multi_colour(self):
        run = _run_scene(os.path.join(self._tmp.name, "q"), ["--base-color-mode", "quantized"])
        report = run["report"]
        self.assertEqual(report["base_color_mode"], "quantized")
        base = _coat_strokes(run["strokes"], LEGACY_KIND)
        self.assertGreater(len(base), 0)
        self.assertEqual(int(report["base_strokes"]), len(base))
        self.assertGreater(S.stroke_color_stats(base)["unique"], 1,
                           "legacy quantized ground must be multi-colour")
        self.assertGreater(float(report["base_color_std"]), 1e-4)
        self.assertIsNone(report["base_color"])
        self.assertIsNone(report["base_coverage"])
        self.assertIsNone(report["base_overlap_frac"])

    def test_source_legacy_smoke(self):
        run = _run_scene(os.path.join(self._tmp.name, "s"), ["--base-color-mode", "source"])
        report = run["report"]
        self.assertEqual(report["base_color_mode"], "source")
        base = _coat_strokes(run["strokes"], LEGACY_KIND)
        self.assertGreater(len(base), 0)
        self.assertGreater(S.stroke_color_stats(base)["unique"], 1)
        self.assertGreater(float(report["base_color_std"]), 1e-4)


class TestHardCap(unittest.TestCase):
    """v1.35 hard-cap: over-budget recipes are deterministically thinned."""

    def test_cap_adjusts_and_preserves_base(self):
        with tempfile.TemporaryDirectory(prefix="base_cap_") as td:
            inp = os.path.join(td, "scene.png")
            S.write_scene(inp, S.scene_canvas(192, 144))
            outdir = os.path.join(td, "out")
            proc = S.run_layered(["--input", inp, "--outdir", outdir,
                                  "--work-res", str(WORK_RES),
                                  "--max-curves", "120"])
            report = S.load_json(os.path.join(outdir, "layered_base_report.json"))
            strokes = S.load_json(os.path.join(outdir, "layered_base_strokes.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertLessEqual(int(report["strokes_out"]), 120)
        self.assertEqual(int(report["strokes_out"]), len(strokes))
        self.assertTrue(bool(report["capped_100k"]))
        self.assertGreater(int(report["pre_cap_strokes"]), int(report["strokes_out"]))
        adjustments = report["cap_adjustments"]
        self.assertIsInstance(adjustments, dict)
        self.assertTrue(adjustments, "cap_adjustments must list the thinned layers")
        for layer, info in adjustments.items():
            self.assertIn("removed", info)
            self.assertIn("frac", info)
            self.assertGreater(int(info["removed"]), 0)
            self.assertGreater(float(info["frac"]), 0.0)
            self.assertLessEqual(float(info["frac"]), 1.0)
        self.assertIn("WARNING over --max-curves", proc.stdout)
        # the underpainting ground is never thinned, only formal/texture layers
        base = [s for s in strokes if str(s.get("kind", "")) in (COAT_KIND, LEGACY_KIND)]
        self.assertEqual(len(base), int(report["pass1_full"]))
        self.assertGreater(len(base), 0)

class TestLineArtSmoke(unittest.TestCase):
    """Real line-art image at small resolution (skipped when image is absent)."""

    @classmethod
    def setUpClass(cls):
        cls.image = S.lineart_image()
        if cls.image is None:
            raise unittest.SkipTest("work/input2/img_20261016.png not present")
        cls._tmp = tempfile.TemporaryDirectory(prefix="base_lineart_")
        cls.addClassCleanup(cls._tmp.cleanup)

    def test_lineart_none_mode(self):
        outdir = os.path.join(self._tmp.name, "out_none")
        S.run_layered_ok(None, ["--input", self.image, "--outdir", outdir,
                                "--work-res", "128", "--base-color-mode", "none"],
                         label="lineart none")
        report = S.load_json(os.path.join(outdir, "layered_base_report.json"))
        strokes = S.load_json(os.path.join(outdir, "layered_base_strokes.json"))
        self.assertEqual(report["base_color_mode"], "none")
        self.assertEqual(int(report["base_strokes"]), 0)
        self.assertFalse(_coat_strokes(strokes, COAT_KIND))
        self.assertGreater(int(report["strokes_out"]), 0)
        self.assertTrue(math.isfinite(float(report["psnr"])))

    def test_lineart_toned_auto_colour_is_near_white(self):
        outdir = os.path.join(self._tmp.name, "out_toned")
        S.run_layered_ok(None, ["--input", self.image, "--outdir", outdir,
                                "--work-res", "128"], label="lineart toned")
        report = S.load_json(os.path.join(outdir, "layered_base_report.json"))
        color = np.asarray(report["base_color"], dtype=np.float64)
        print(f"[base-coat] lineart auto colour={color.tolist()}")
        self.assertGreaterEqual(float(color.min()), 0.85,
                                "line-art auto tone should be close to white paper")
        self.assertLessEqual(float(report["base_color_std"]), 1e-6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
