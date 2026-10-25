#!/usr/bin/env python
"""v1.36 human-paint tests: wash ground, form-following strokes, region order.

Frozen interface (Lead 2026-10-24, fixer-core):
  * ``--base-coat-pattern {wash,rows}`` default ``wash``; ``--wash-angle -22.0``;
    wash = big diagonal/curved sweeps, one flat colour, coverage>=99%, overlap<=15%;
    rows = old row scan (legacy);
  * ``--flow-strokes {on,off}`` default ``on``: jittered (non axis-aligned) sampling,
    direction from the structure-tensor tangent; report ``flow_orientation_frac``
    (<=20 deg) and ``axis_aligned_frac`` (<=10 deg vs horizontal/vertical);
  * ``--paint-order {region,scan,legacy}`` default ``region``, ``--region-count 12``;
    region = visit-order grouping, scan = old y order, legacy = generation order;
  * ``--thick-dabs`` int default 0;
  * legacy auto fallback: base-color-mode single/quantized/source without explicit
    --flow-strokes/--paint-order => flow off + paint-order legacy + rows pattern;
  * report: base_coat_pattern/wash_angle/flow_strokes/flow_orientation_frac/
    axis_aligned_frac/paint_order/paint_order_effective/order_y_spearman/
    order_adjacent_median_px/order_region_run_frac/region_count_used/thick_dabs;
  * 100k hard cap and pure flat-colour ground (std=0, no gradient) unchanged.

Metrics are recomputed from the saved strokes JSON with ``_support`` helpers, so
the assertions are independent of the report rounding.

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
except ImportError:  # imported as ``tests.test_human_paint``
    from tests import _support as S

WORK_RES = 128
WASH_KIND = "base_toned"
LEGACY_KIND = "base_pass1"
COVERAGE_MIN = 0.99
WASH_OVERLAP_MAX = 0.15
ROWS_OVERLAP_MAX = 0.05

FROZEN_HUMAN_PAINT_DEFAULTS = {
    "--base-coat-pattern": "wash",
    "--wash-angle": -22.0,
    "--flow-strokes": "on",
    "--paint-order": "region",
    "--region-count": 12,
    "--thick-dabs": 0,
    "--clip-to-fg": "auto",
}

REPORT_HUMAN_PAINT_KEYS = [
    "base_coat_pattern", "wash_angle", "flow_strokes", "flow_orientation_frac",
    "axis_aligned_frac", "direction_strokes", "paint_order",
    "paint_order_effective", "order_y_spearman", "order_adjacent_median_px",
    "order_region_run_frac", "region_count_used", "thick_dabs",
    "thick_dab_strokes", "texture_flat_strokes", "clip_to_fg",
    "base_metrics_mode", "base_metrics_scale",
]


def _run_scene(tmpdir, label, extra, scene=None, work_res=WORK_RES):
    os.makedirs(os.path.join(tmpdir, label), exist_ok=True)
    root = os.path.join(tmpdir, label)
    inp = os.path.join(root, "scene.png")
    S.write_scene(inp, S.scene_canvas(192, 144) if scene is None else scene)
    outdir = os.path.join(root, "out")
    S.run_layered_ok(None, ["--input", inp, "--outdir", outdir,
                            "--work-res", str(work_res)] + list(extra),
                     label=label)
    report = S.load_json(os.path.join(outdir, "layered_base_report.json"))
    strokes = S.load_json(os.path.join(outdir, "layered_base_strokes.json"))
    rgb = S.load_rgb(inp, work_res)
    return {"report": report, "strokes": strokes, "rgb": rgb,
            "fg": S.fg_mask(rgb), "outdir": outdir}


def _coat(run):
    return [s for s in run["strokes"] if str(s.get("kind", "")) == WASH_KIND]


def _raster(run, group):
    H, W = run["fg"].shape
    counts = S.stroke_footprints(group, H, W)
    return counts, S.coverage_metrics(counts, run["fg"])


def _angles(run, group):
    H, W = run["fg"].shape
    return S.stroke_direction_angles(group, H, W)


class TestHumanPaintInterface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.help_text = S.script_help(S.DEFAULT_LAYERED)
        cls.defaults = S.argparse_defaults(S.DEFAULT_LAYERED)
        cls.choices = S.argparse_choices(S.DEFAULT_LAYERED)

    def test_help_lists_new_options(self):
        for option in ("--base-coat-pattern", "--wash-angle", "--flow-strokes",
                       "--paint-order", "--region-count", "--thick-dabs",
                       "--clip-to-fg"):
            self.assertIn(option, self.help_text,
                          f"{option} missing from layered_base.py --help")

    def test_frozen_human_paint_defaults(self):
        problems = []
        for option, expected in FROZEN_HUMAN_PAINT_DEFAULTS.items():
            if option not in self.defaults:
                problems.append(f"{option}: missing")
            elif self.defaults[option] != expected:
                problems.append(f"{option}: {self.defaults[option]!r} != {expected!r}")
        self.assertFalse(problems, "\n".join(problems))

    def test_choices_and_legacy_auto(self):
        self.assertEqual(self.choices.get("--base-coat-pattern"), ["wash", "rows"])
        self.assertEqual(self.choices.get("--flow-strokes"), ["on", "off"])
        self.assertEqual(self.choices.get("--paint-order"),
                         ["region", "scan", "legacy"])
        self.assertEqual(self.choices.get("--clip-to-fg"), ["auto", "on", "off"])
        self.assertEqual(self.defaults.get("--base-color-mode"), "toned")


class TestWashBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="humanpaint_wash_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.default_run = _run_scene(cls._tmp.name, "default_wash", [])
        cls.report = cls.default_run["report"]
        cls.base = _coat(cls.default_run)

    def test_default_ground_is_wash(self):
        report = self.report
        for key in REPORT_HUMAN_PAINT_KEYS:
            self.assertIn(key, report, f"report missing {key}")
        self.assertEqual(report["base_coat_pattern"], "wash")
        self.assertEqual(report["flow_strokes"], True)
        self.assertEqual(report["paint_order"], "region")
        self.assertEqual(report["paint_order_effective"], "region")
        self.assertEqual(int(report["region_count_used"]), 12)
        self.assertEqual(int(report["thick_dabs"]), 0)
        self.assertEqual(int(report["thick_dab_strokes"]), 0)
        self.assertEqual(int(report["texture_flat_strokes"]), 0)
        self.assertIn(report.get("base_metrics_mode"), ("raster", "analytic"))
        self.assertEqual(report["base_metrics_mode"], "raster",
                         "small work-res (<768) must use the real raster metric")
        self.assertAlmostEqual(float(report["base_metrics_scale"]), 1.0)
        self.assertTrue(bool(report["clip_to_fg"]))
        self.assertGreater(int(report["direction_strokes"]), 0)
        self.assertGreater(len(self.base), 1)
        self.assertEqual(int(report["base_strokes"]), len(self.base))

    def test_wash_is_not_a_row_scan(self):
        angles, _ = _angles(self.default_run, self.base)
        axis = S.axis_aligned_frac(angles)
        entropy = S.direction_entropy(angles)
        print(f"[human-paint] wash base axis_aligned={axis:.4f} entropy={entropy:.4f} "
              f"strokes={len(self.base)}")
        self.assertLess(axis, 0.5,
                        "wash ground must not be a row scan")
        self.assertGreater(entropy, 0.0,
                           "wash directions must differ from the 1-bin row scan " 
                           "(v1.35 rows entropy was 0.0)")

    def test_wash_single_flat_colour_no_gradient(self):
        stats = S.stroke_color_stats(self.base)
        self.assertEqual(stats["unique"], 1)
        self.assertLessEqual(max(stats["std"]), 1e-6)
        for stroke in self.base:
            self.assertNotIn("gradient", stroke)
            self.assertEqual(int(stroke["layer"]), 0)
            self.assertEqual(float(stroke["alpha"]), 1.0)
        self.assertEqual(int(self.report["base_color_std"]), 0)
        self.assertEqual(int(self.report["base_gradient_strokes"]), 0)

    def test_wash_coverage_and_overlap(self):
        _, metrics = _raster(self.default_run, self.base)
        print(f"[human-paint] wash coverage={metrics['coverage']:.4f} "
              f"overlap_fg={metrics['overlap_frac_of_fg']:.4f} "
              f"max_count={metrics['max_count']} "
              f"report(cov={self.report['base_coverage']}, "
              f"ovl={self.report['base_overlap_frac']})")
        self.assertGreaterEqual(metrics["coverage"], COVERAGE_MIN)
        self.assertLessEqual(metrics["overlap_frac_of_fg"], WASH_OVERLAP_MAX + 0.01,
                             "wash overlap must stay <=15% (+1pp raster tolerance)")
        self.assertAlmostEqual(float(self.report["base_coverage"]),
                               metrics["coverage"], delta=0.002)
        self.assertAlmostEqual(float(self.report["base_overlap_frac"]),
                               metrics["overlap_frac_of_fg"], delta=0.01)

    def test_rows_pattern_is_kept_as_legacy_option(self):
        rows_run = _run_scene(self._tmp.name, "rows", ["--base-coat-pattern", "rows"])
        report = rows_run["report"]
        self.assertEqual(report["base_coat_pattern"], "rows")
        base = _coat(rows_run)
        self.assertGreater(len(base), 0)
        angles, _ = _angles(rows_run, base)
        self.assertGreaterEqual(S.axis_aligned_frac(angles), 0.99,
                                "explicit rows pattern must still be the old row scan")
        _, metrics = _raster(rows_run, base)
        self.assertGreaterEqual(metrics["coverage"], COVERAGE_MIN)
        self.assertLessEqual(metrics["overlap_frac_of_fg"], ROWS_OVERLAP_MAX + 0.01)


class TestFlowStrokes(unittest.TestCase):
    """Flow on/off on the split scene.

    ``scene_split`` has one large flat block next to textured content, i.e. many
    low-coherence areas where the old tile path falls back to the axis-aligned
    structure direction while the v1.36 flow path uses the region main direction
    rotated away from the axes.  On the fully axis-aligned ``scene_canvas`` both
    paths are axis-aligned (the source itself is), so that scene cannot show the
    claim and is not used here.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="humanpaint_flow_")
        cls.addClassCleanup(cls._tmp.cleanup)
        scene = S.scene_split(192, 144)
        cls.on_run = _run_scene(cls._tmp.name, "flow_on", [], scene=scene)
        cls.off_run = _run_scene(cls._tmp.name, "flow_off",
                                 ["--flow-strokes", "off"], scene=scene)

    def test_default_flow_on_metrics(self):
        report = self.on_run["report"]
        self.assertEqual(report["flow_strokes"], True)
        self.assertIsNotNone(report["flow_orientation_frac"])
        self.assertGreater(float(report["flow_orientation_frac"]), 0.5)
        self.assertIsNotNone(report["axis_aligned_frac"])
        formal = S.formal_strokes(self.on_run["strokes"])
        self.assertGreater(len(formal), 0)
        flow_frac = S.flow_orientation_frac(formal, self.on_run["rgb"])
        print(f"[human-paint] flow on: report flow={report['flow_orientation_frac']} "
              f"axis={report['axis_aligned_frac']} self_flow={flow_frac:.4f}")
        self.assertGreaterEqual(flow_frac, 0.5,
                                "flow=on strokes should follow the structure tensor")

    def test_flow_on_reduces_axis_alignment_vs_off(self):
        on_axis = float(self.on_run["report"]["axis_aligned_frac"])
        off_axis = float(self.off_run["report"]["axis_aligned_frac"])
        print(f"[human-paint] axis_aligned on={on_axis:.4f} off={off_axis:.4f}")
        self.assertEqual(self.off_run["report"]["flow_strokes"], False)
        self.assertLess(on_axis, off_axis - 0.05,
                        "flow=on should clearly cut the axis-aligned share vs "
                        "the old axis-aligned tile path on low-coherence areas")
        self.assertLessEqual(on_axis, 0.9)

    def test_flow_off_still_works(self):
        report = self.off_run["report"]
        self.assertGreater(int(report["strokes_out"]), 0)
        self.assertTrue(math.isfinite(float(report["psnr"])))


class TestPaintOrder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="humanpaint_order_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.region_run = _run_scene(cls._tmp.name, "region", [])
        cls.scan_run = _run_scene(cls._tmp.name, "scan", ["--paint-order", "scan"])
        cls.legacy_run = _run_scene(cls._tmp.name, "legacy", ["--paint-order", "legacy"])

    def test_region_order_is_spatial_not_y_scan(self):
        region = self.region_run["report"]
        scan = self.scan_run["report"]
        self.assertEqual(region["paint_order"], "region")
        self.assertEqual(region["paint_order_effective"], "region")
        self.assertEqual(scan["paint_order_effective"], "scan")
        ry = float(region["order_y_spearman"])
        sy = float(scan["order_y_spearman"])
        ra = float(region["order_adjacent_median_px"])
        sa = float(scan["order_adjacent_median_px"])
        rr = float(region["order_region_run_frac"])
        sr = float(scan["order_region_run_frac"])
        print(f"[human-paint] region order: y_spearman={ry:.4f} "
              f"adjacent={ra:.2f}px region_run={rr:.4f} | "
              f"scan: y_spearman={sy:.4f} adjacent={sa:.2f}px region_run={sr:.4f}")
        self.assertLess(ry, 0.5, "region order should not be a y scan")
        self.assertLess(ry, sy - 0.2, "region order must be far less y-ordered than scan")
        self.assertLess(ra, sa, "region order should visit nearby strokes")
        self.assertGreater(rr, sr, "region order should keep same-region runs")

    def test_self_computed_order_metrics_agree(self):
        strokes = self.region_run["strokes"]
        H, W = self.region_run["fg"].shape
        y_spear = S.sequence_y_spearman(strokes, H, W)
        adjacent = S.adjacent_center_distance_median(strokes, H, W)
        report = self.region_run["report"]
        self.assertLess(y_spear, 0.5)
        self.assertAlmostEqual(y_spear, float(report["order_y_spearman"]), delta=0.1)
        self.assertAlmostEqual(adjacent, float(report["order_adjacent_median_px"]),
                               delta=max(2.0, 0.1 * adjacent))

    def test_legacy_order_is_generation_order(self):
        report = self.legacy_run["report"]
        self.assertEqual(report["paint_order"], "legacy")
        self.assertEqual(report["paint_order_effective"], "legacy")
        self.assertIsNone(report["order_y_spearman"])
        self.assertIsNone(report["order_adjacent_median_px"])
        self.assertGreater(int(report["strokes_out"]), 0)

    def test_legacy_base_mode_auto_falls_back(self):
        run = _run_scene(self._tmp.name, "legacy_auto", ["--base-color-mode", "quantized"])
        report = run["report"]
        self.assertEqual(report["base_color_mode"], "quantized")
        self.assertEqual(report["flow_strokes"], False)
        self.assertEqual(report["paint_order_effective"], "legacy")
        self.assertIsNone(report["base_coat_pattern"])
        formal = S.formal_strokes(run["strokes"])
        self.assertGreater(len(formal), 0)
        self.assertTrue(math.isfinite(float(report["psnr"])))


class TestRealImageFlow(unittest.TestCase):
    """Real image at small resolution: strokes follow form, axis-aligned share is low."""

    @classmethod
    def setUpClass(cls):
        cls.image = S.lineart_image()  # placeholder; real photos preferred below
        for candidate in (
            os.path.join(S.WORKSPACE_ROOT, "training", "input", "Sunflowers_user.jpg"),
            os.path.join(S.WORKSPACE_ROOT, "training", "input", "Lvpi5.jpg"),
        ):
            if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                cls.image = candidate
                break
        if cls.image is None or not os.path.isfile(cls.image):
            raise unittest.SkipTest("no real test image available")
        cls._tmp = tempfile.TemporaryDirectory(prefix="humanpaint_real_")
        cls.addClassCleanup(cls._tmp.cleanup)
        outdir = os.path.join(cls._tmp.name, "real_img")
        os.makedirs(outdir, exist_ok=True)
        cls._run_real(outdir)

    @classmethod
    def _run_real(cls, outdir):
        S.run_layered_ok(None, ["--input", cls.image, "--outdir", outdir,
                                "--work-res", "192"], label="real flow")
        cls.report = S.load_json(os.path.join(outdir, "layered_base_report.json"))
        cls.strokes = S.load_json(os.path.join(outdir, "layered_base_strokes.json"))
        cls.rgb = S.load_rgb(cls.image, 192)

    def test_real_image_flow_and_axis(self):
        report = self.report
        axis = float(report["axis_aligned_frac"])
        flow = float(report["flow_orientation_frac"])
        formal = S.formal_strokes(self.strokes)
        self_axis = S.axis_aligned_frac(S.stroke_direction_angles(
            formal, *self.rgb.shape[:2])[0])
        print(f"[human-paint] real image axis_aligned={axis:.4f} "
              f"flow_orientation={flow:.4f} self_axis={self_axis:.4f} "
              f"image={os.path.basename(self.image)}")
        self.assertEqual(report["flow_strokes"], True)
        self.assertGreater(flow, 0.0)
        self.assertLess(axis, 0.5,
                        "form-following strokes should not be mostly axis-aligned")
        self.assertAlmostEqual(self_axis, axis, delta=0.05)

class TestDefaultRecipeBudget(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="humanpaint_budget_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.default_run = _run_scene(cls._tmp.name, "default", [])
        cls.report = cls.default_run["report"]

    def test_default_run_budget_and_quality(self):
        report = self.report
        self.assertEqual(int(report["base_color_mode"] == ""), 0)
        self.assertEqual(report["base_color_mode"], "toned")
        self.assertLessEqual(int(report["strokes_out"]), 100000)
        self.assertFalse(bool(report["capped_100k"]))
        self.assertEqual(int(report["pre_cap_strokes"]), int(report["strokes_out"]))
        self.assertEqual(int(report["max_curves"]), 100000)
        pass_total = sum(int(report[key]) for key in
                         ("pass1_full", "pass2_local", "pass3_local", "pass4_local"))
        # v1.37: silhouette clipping drops halo strokes after generation;
        # generated (passes + detail + texture + dabs) - dropped == saved.
        dropped = int(report.get("silhouette_dropped_strokes", 0))
        self.assertEqual(pass_total + int(report["detail"]) +
                         int(report.get("texture_flat_strokes", 0)) +
                         int(report.get("thick_dab_strokes", 0)) - dropped,
                         int(report["strokes_out"]))
        self.assertGreater(int(report["strokes_out"]), int(report["base_strokes"]))
        self.assertTrue(math.isfinite(float(report["psnr"])))
        self.assertGreater(float(report["psnr"]), 5.0)
        kinds = {str(s.get("kind", "")) for s in self.default_run["strokes"]}
        self.assertFalse(any("fill" in kind for kind in kinds))


class TestHardCapUnchanged(unittest.TestCase):
    def test_small_budget_thins_formal_layers_only(self):
        with tempfile.TemporaryDirectory(prefix="humanpaint_cap_") as td:
            run = _run_scene(td, "cap", ["--max-curves", "120"])
        report = run["report"]
        self.assertLessEqual(int(report["strokes_out"]), 120)
        self.assertTrue(bool(report["capped_100k"]))
        self.assertGreater(int(report["pre_cap_strokes"]), int(report["strokes_out"]))
        adjustments = report["cap_adjustments"]
        self.assertIsInstance(adjustments, dict)
        self.assertTrue(adjustments)
        base = _coat(run)
        self.assertGreater(len(base), 0)
        self.assertEqual(len(base), int(report["base_strokes"]))
        for layer, info in adjustments.items():
            self.assertIn("removed", info)
            self.assertIn("frac", info)


if __name__ == "__main__":
    unittest.main(verbosity=2)
