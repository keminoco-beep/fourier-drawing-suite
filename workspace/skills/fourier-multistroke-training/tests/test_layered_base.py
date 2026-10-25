#!/usr/bin/env python
"""Unit invariants + CLI smoke test for ``scripts/layered_base.py``.

Fast tests only (well under the 60 s budget): no real training image is used;
the CLI smoke test runs the script on a 192x144 synthetic colour-block PNG at
``--work-res 128`` (about 1.5 s on a normal laptop).

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
from PIL import Image

try:  # imported as a top-level module by ``unittest discover -s tests``
    import _support as S
except ImportError:  # imported as ``tests.test_layered_base``
    from tests import _support as S

LB = S.import_layered_base()
HAS_GRADIENT_API = S.has_gradient_api(LB)


def _roundtrip_strokes():
    return [
        {
            "points": np.array([
                [0.111111111, 0.222222222],
                [0.333333333, 0.444444444],
                [0.555555555, 0.666666666],
            ]),
            "widths": np.array([0.012345678, 0.012345678, 0.012345678]),
            "color": np.array([0.123456789, 0.500000000, 0.987654321]),
            "alpha": 1.0,
            "layer": 2,
            "kind": "raw_path",
            "closed": False,
            "gradient": {
                "start": np.array([0.100000001, 0.200000002, 0.300000003]),
                "end": np.array([0.400000004, 0.500000005, 0.600000006]),
            },
        },
        {
            "points": np.array([[0.0, 0.0], [0.5, 0.25], [1.0, 1.0]]),
            "widths": np.array([0.02, 0.02, 0.02]),
            "color": np.array([0.25, 0.5, 0.75]),
            "alpha": 1.0,
            "layer": 3,
            "kind": "detail_line",
            "closed": False,
        },
    ]


def _load_all(path):
    """Load every stroke, tolerating the pre-v1.33 load_strokes signature."""
    try:
        return LB.load_strokes(path, skip=(), skip_prefixes=())
    except TypeError:  # pre-v1.33 snapshot: no skip_prefixes argument
        return LB.load_strokes(path, skip=())


def _ramp_rgb(H=64, W=64):
    """Horizontal smooth red->blue ramp (no sharp edges: a "smooth ramp")."""
    x = np.arange(W)
    t = x / float(W - 1)
    red = np.array([0.85, 0.10, 0.10])
    blue = np.array([0.10, 0.10, 0.85])
    row = red[None, :] * (1.0 - t)[:, None] + blue[None, :] * t[:, None]
    return np.repeat(row[None, :, :], H, axis=0)


class TestSaveLoadRoundtrip(unittest.TestCase):
    """save_strokes / load_strokes must preserve payload fields and precision."""

    def test_save_writes_rounded_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "strokes.json")
            LB.save_strokes(path, _roundtrip_strokes())
            payload = json.load(open(path, encoding="utf-8"))
        self.assertEqual(len(payload), 2)
        first = payload[0]
        # points round(...,6), widths round(...,7), colour/gradient round(...,5)
        self.assertAlmostEqual(first["points"][0][0], 0.111111, places=9)
        self.assertAlmostEqual(first["widths"][0], 0.0123457, places=9)
        self.assertAlmostEqual(first["color"][0], 0.12346, places=9)
        self.assertAlmostEqual(first["gradient"]["start"][0], 0.1, places=9)
        self.assertEqual(first["alpha"], 1.0)
        self.assertIsInstance(first["alpha"], float)
        self.assertEqual(first["layer"], 2)
        self.assertIsInstance(first["layer"], int)
        self.assertEqual(first["kind"], "raw_path")
        self.assertIs(first["closed"], False)
        self.assertNotIn("gradient", payload[1])

    def test_load_roundtrip_arrays_and_precision(self):
        original = _roundtrip_strokes()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "strokes.json")
            LB.save_strokes(path, original)
            loaded = _load_all(path)
        self.assertEqual(len(loaded), len(original))
        for got, want in zip(loaded, original):
            self.assertIsInstance(got["points"], np.ndarray)
            self.assertIsInstance(got["widths"], np.ndarray)
            self.assertIsInstance(got["color"], np.ndarray)
            np.testing.assert_allclose(got["points"], want["points"], atol=1e-6)
            np.testing.assert_allclose(got["widths"], want["widths"], atol=1e-7)
            np.testing.assert_allclose(got["color"], want["color"], atol=1e-5)
            self.assertEqual(got["kind"], want["kind"])
            self.assertEqual(got["layer"], want["layer"])
            self.assertEqual(got["alpha"], 1.0)
            self.assertEqual(got["closed"], False)
            if "gradient" in want:
                np.testing.assert_allclose(got["gradient"]["start"],
                                           want["gradient"]["start"], atol=1e-5)
                np.testing.assert_allclose(got["gradient"]["end"],
                                           want["gradient"]["end"], atol=1e-5)
            else:
                self.assertNotIn("gradient", got)

    def test_load_default_drops_underpainting_kinds(self):
        kinds = [
            "base_pass1", "base_pass2", "base_pass3", "base_pass4", "base",
            "fix", "fix_extra", "soft_atmosphere", "soft_glaze", "soft_extra",
            "detail", "tex", "hatch",
        ]
        strokes = []
        for i, kind in enumerate(kinds):
            strokes.append({
                "points": np.array([[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]),
                "widths": np.array([0.01, 0.01, 0.01]),
                "color": np.array([0.1, 0.2, 0.3]),
                "alpha": 1.0, "layer": 5, "kind": kind, "closed": False,
            })
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "detail.json")
            LB.save_strokes(path, strokes)
            default_loaded = LB.load_strokes(path)
            everything = _load_all(path)
        self.assertEqual(sorted(s["kind"] for s in default_loaded),
                         ["detail", "hatch", "tex"])
        self.assertEqual(len(everything), len(kinds))


class TestPaletteMaps(unittest.TestCase):
    """palette_map / local_palette_map shape and value-range invariants."""

    def _rgb(self, h=40, w=32):
        rng = np.random.default_rng(0)
        return np.clip(rng.normal(0.5, 0.25, (h, w, 3)), 0.0, 1.0)

    def test_palette_map_shape_and_range(self):
        rgb = self._rgb()
        fg = np.ones(rgb.shape[:2], dtype=bool)
        fg[:, :4] = False
        cmap, centers = LB.palette_map(rgb, fg, 6, 2.0, np.random.default_rng(0))
        self.assertEqual(cmap.shape, rgb.shape)
        self.assertTrue(np.all(np.isfinite(cmap)))
        self.assertGreaterEqual(float(cmap.min()), 0.0)
        self.assertLessEqual(float(cmap.max()), 1.0)
        centers = np.asarray(centers, dtype=np.float64)
        self.assertEqual(centers.ndim, 2)
        self.assertEqual(centers.shape[1], 3)
        self.assertGreaterEqual(int(centers.shape[0]), 1)
        self.assertLessEqual(int(centers.shape[0]), 6)
        self.assertTrue(np.all(np.isfinite(centers)))
        self.assertGreaterEqual(float(centers.min()), 0.0)
        self.assertLessEqual(float(centers.max()), 1.0)

    def test_local_palette_map_shape_and_range(self):
        rgb = self._rgb(48, 40)
        fg = np.ones(rgb.shape[:2], dtype=bool)
        cmap = LB.local_palette_map(rgb, fg, 16, 4, 1.5, np.random.default_rng(0))
        self.assertEqual(cmap.shape, rgb.shape)
        self.assertTrue(np.all(np.isfinite(cmap)))
        self.assertGreaterEqual(float(cmap.min()), 0.0)
        self.assertLessEqual(float(cmap.max()), 1.0)
        self.assertTrue(np.any(cmap > 0.0))


class TestTilePass(unittest.TestCase):
    """tile_pass invariants: grid bound, mask/fg gating, stroke field ranges."""

    H = W = 64
    SPACING = 8

    def setUp(self):
        self.rgb = S.scene_checker(self.H, self.W, tile=16)
        self.fg = np.ones((self.H, self.W), dtype=bool)
        self.mask = np.ones((self.H, self.W), dtype=bool)
        self.theta = np.zeros((self.H, self.W), dtype=np.float64)
        self.grid = len(np.arange(self.SPACING * 0.5, self.H, self.SPACING)) * \
            len(np.arange(self.SPACING * 0.5, self.W, self.SPACING))

    def _tile(self, *, fg=None, mask=None, seed=7, **kwargs):
        return LB.tile_pass(self.rgb, self.fg if fg is None else fg,
                            self.mask if mask is None else mask, self.theta,
                            self.SPACING, 1.2, 1.4, 3, "base_pass4",
                            np.random.default_rng(seed), color_map=None,
                            color_jitter=0.0, **kwargs)

    def test_count_and_stroke_field_ranges(self):
        strokes = self._tile(gradient=False)
        self.assertGreater(len(strokes), 0)
        self.assertLessEqual(len(strokes), self.grid)
        scale = float(max(self.H, self.W))
        for stroke in strokes:
            points = np.asarray(stroke["points"], dtype=np.float64)
            self.assertEqual(points.shape, (3, 2))
            self.assertGreaterEqual(float(points.min()), 0.0)
            self.assertLessEqual(float(points.max()), 1.0)
            widths = np.asarray(stroke["widths"], dtype=np.float64)
            self.assertEqual(widths.shape, (3,))
            self.assertTrue(np.all(widths > 0.0))
            self.assertGreater(widths[0], 0.0)
            color = np.asarray(stroke["color"], dtype=np.float64)
            self.assertEqual(color.shape, (3,))
            self.assertGreaterEqual(float(color.min()), 0.0)
            self.assertLessEqual(float(color.max()), 1.0)
            self.assertEqual(stroke["alpha"], 1.0)
            self.assertEqual(stroke["layer"], 3)
            self.assertEqual(stroke["kind"], "base_pass4")
            self.assertIs(stroke["closed"], False)
            self.assertNotIn("gradient", stroke)
        # sparsely sampled grid -> exactly one stroke per mask pixel on the grid
        self.assertEqual(len(strokes), self.grid)

    def test_mask_gates_generation(self):
        mask = np.zeros((self.H, self.W), dtype=bool)
        mask[:, :self.W // 2] = True
        mask &= self.fg
        strokes = self._tile(mask=mask)
        self.assertEqual(len(strokes), (self.W // 2 // self.SPACING) *
                         (self.H // self.SPACING))
        scale = float(max(self.H, self.W))
        for stroke in strokes:
            center_x = float(np.asarray(stroke["points"])[1, 0]) * scale
            self.assertLess(center_x, self.W / 2 + 1.5)
            self.assertGreaterEqual(center_x, 0.0)

    def test_fg_gates_generation(self):
        fg = np.ones((self.H, self.W), dtype=bool)
        fg[:, :self.W // 2] = False
        strokes = self._tile(fg=fg, mask=fg)
        self.assertEqual(len(strokes), (self.W // 2 // self.SPACING) *
                         (self.H // self.SPACING))
        scale = float(max(self.H, self.W))
        for stroke in strokes:
            center_x = float(np.asarray(stroke["points"])[1, 0]) * scale
            self.assertGreaterEqual(center_x, self.W / 2 - 1.5)


@unittest.skipUnless(HAS_GRADIENT_API,
                     "gradient gate API not present in this layered_base.py")
class TestGradientGateUnit(unittest.TestCase):
    """Unit-level contract of the v1.33 gradient safety gate (tile_pass kwargs)."""

    H = W = 64
    SPACING = 8
    GATE = S.GATE_DEFAULT_MAX_DELTA

    def setUp(self):
        self.rgb = S.scene_checker(self.H, self.W, tile=16)
        self.fg = np.ones((self.H, self.W), dtype=bool)
        self.theta = np.zeros((self.H, self.W), dtype=np.float64)

    def _tile(self, *, fg=None, mask=None, seed=7, stats=None, **kwargs):
        return LB.tile_pass(self.rgb, self.fg if fg is None else fg,
                            self.fg if mask is None else mask, self.theta,
                            self.SPACING, 1.2, 1.4, 3, "base_pass4",
                            np.random.default_rng(seed), color_map=None,
                            color_jitter=0.0, gradient_stats=stats, **kwargs)

    def test_gate_on_only_accepts_safe_deltas(self):
        stats = LB._new_gradient_stats()
        strokes = self._tile(gradient=True, gradient_gate=True,
                             gradient_max_delta=self.GATE,
                             gradient_edge_delta=S.GATE_DEFAULT_EDGE_DELTA,
                             gradient_edge_map=None, stats=stats)
        graded = [s for s in strokes if "gradient" in s]
        self.assertGreater(len(graded), 0)
        for stroke in graded:
            delta = float(np.max(np.abs(np.asarray(stroke["gradient"]["start"]) -
                                        np.asarray(stroke["gradient"]["end"]))))
            self.assertLessEqual(delta, self.GATE + 1e-9,
                                 "accepted gradient stroke exceeds max delta")
        self.assertEqual(stats["candidates"], len(strokes))
        self.assertEqual(stats["accepted"], len(graded))
        self.assertEqual(stats["rejected_edge"],
                         stats["rejected_edge_color"] + stats["rejected_edge_grad"])
        rejected = (stats["rejected_delta"] + stats["rejected_edge"] +
                    stats["rejected_oob"])
        self.assertEqual(rejected, len(strokes) - len(graded))

    def test_gate_off_restores_unsafe_gradients(self):
        stats = LB._new_gradient_stats()
        strokes = self._tile(gradient=True, gradient_gate=False,
                             gradient_max_delta=self.GATE,
                             gradient_edge_delta=S.GATE_DEFAULT_EDGE_DELTA,
                             gradient_edge_map=None, stats=stats)
        deltas = [float(np.max(np.abs(np.asarray(s["gradient"]["start"]) -
                                      np.asarray(s["gradient"]["end"]))))
                  for s in strokes if "gradient" in s]
        self.assertEqual(len(deltas), len(strokes))
        self.assertTrue(any(d > self.GATE for d in deltas),
                        f"gate off should keep unsafe gradients, max={max(deltas):.4f}")
        self.assertEqual(stats["accepted"], stats["candidates"])
        self.assertEqual(stats["rejected_delta"] + stats["rejected_edge"] +
                         stats["rejected_oob"], 0)

    def test_smooth_ramp_with_large_delta_is_rejected(self):
        """Strict contract: a smooth ramp must NOT bypass ``--gradient-max-delta``.

        A gentle red->blue horizontal ramp sampled by 21.6 px horizontal strokes
        yields endpoint deltas up to ~0.30 while the largest adjacent 9-sample
        colour step is only ~0.03 (a "smooth ramp", the case a dev build briefly
        whitelisted).  The frozen v1.33 contract rejects every stroke with
        ``D > --gradient-max-delta``, so gate on must accept none of them.
        """
        rgb = _ramp_rgb(self.H, self.W)
        fg = np.ones((self.H, self.W), dtype=bool)
        stats = LB._new_gradient_stats()
        strokes = LB.tile_pass(rgb, fg, fg, self.theta, 12, 1.45, 1.8, 1,
                               "base_pass2", np.random.default_rng(7),
                               color_map=None, color_jitter=0.0, gradient=True,
                               gradient_gate=True, gradient_max_delta=self.GATE,
                               gradient_edge_delta=S.GATE_DEFAULT_EDGE_DELTA,
                               gradient_edge_map=None, gradient_stats=stats)
        graded = [s for s in strokes if "gradient" in s]
        deltas = [float(np.max(np.abs(np.asarray(s["gradient"]["start"]) -
                                      np.asarray(s["gradient"]["end"]))))
                  for s in graded]
        print(f"[gate-unit] smooth-ramp candidates={stats['candidates']} "
              f"accepted={len(graded)} max_delta="
              f"{max(deltas) if deltas else 0.0:.4f}")
        self.assertGreater(stats["candidates"], 0)
        violations = [d for d in deltas if d > self.GATE + 1e-9]
        self.assertFalse(
            violations,
            f"{len(violations)} smooth-ramp gradient stroke(s) accepted with "
            f"delta > {self.GATE}: max={max(deltas) if deltas else 0.0:.4f}. "
            "Strict contract has no smooth-ramp exception.")
        # On this controlled scene every candidate exceeds the gate -> all
        # rejected by the delta sub-gate once the strict rule is implemented.
        self.assertEqual(len(graded), 0)
        self.assertEqual(stats["rejected_delta"], stats["candidates"])
        # Control: gate off accepts exactly the same candidates (proves the
        # scene really exercises the delta gate, not a degenerate empty run).
        stats_off = LB._new_gradient_stats()
        strokes_off = LB.tile_pass(rgb, fg, fg, self.theta, 12, 1.45, 1.8, 1,
                                   "base_pass2", np.random.default_rng(7),
                                   color_map=None, color_jitter=0.0, gradient=True,
                                   gradient_gate=False, gradient_max_delta=self.GATE,
                                   gradient_edge_delta=S.GATE_DEFAULT_EDGE_DELTA,
                                   gradient_edge_map=None, gradient_stats=stats_off)
        graded_off = [s for s in strokes_off if "gradient" in s]
        self.assertEqual(len(graded_off), stats_off["candidates"])

    def test_gate_off_preserves_geometry(self):
        on = self._tile(gradient=True, gradient_gate=True,
                        gradient_max_delta=self.GATE,
                        gradient_edge_delta=S.GATE_DEFAULT_EDGE_DELTA,
                        gradient_edge_map=None)
        off = self._tile(gradient=True, gradient_gate=False)
        self.assertEqual(len(on), len(off))
        for a, b in zip(on, off):
            np.testing.assert_allclose(a["points"], b["points"], atol=0.0)
            np.testing.assert_allclose(a["widths"], b["widths"], atol=0.0)

    def test_edge_map_rejects_all_crossing_strokes(self):
        stats = LB._new_gradient_stats()
        edge_map = np.ones((self.H, self.W), dtype=bool)
        strokes = self._tile(gradient=True, gradient_gate=True,
                             gradient_max_delta=self.GATE,
                             gradient_edge_delta=S.GATE_DEFAULT_EDGE_DELTA,
                             gradient_edge_map=edge_map, stats=stats)
        self.assertEqual([s for s in strokes if "gradient" in s], [])
        self.assertEqual(stats["accepted"], 0)
        self.assertEqual(stats["rejected_edge"],
                         stats["rejected_edge_color"] + stats["rejected_edge_grad"])
        self.assertGreater(stats["rejected_edge"], 0)
        self.assertEqual(stats["rejected_delta"] + stats["rejected_edge"] +
                         stats["rejected_oob"], stats["candidates"])

    def test_out_of_foreground_samples_are_rejected(self):
        fg = np.ones((self.H, self.W), dtype=bool)
        fg[:, self.W - 8:] = False
        stats = LB._new_gradient_stats()
        strokes = self._tile(fg=fg, mask=fg, gradient=True, gradient_gate=True,
                             gradient_max_delta=self.GATE,
                             gradient_edge_delta=S.GATE_DEFAULT_EDGE_DELTA,
                             gradient_edge_map=None, stats=stats)
        self.assertGreater(stats["rejected_oob"], 0)
        self.assertEqual(stats["candidates"], len(strokes))
        self.assertEqual(stats["accepted"], len([s for s in strokes if "gradient" in s]))


@unittest.skipUnless(hasattr(LB, "_stray_color_metrics"),
                     "stray colour metric not present in this layered_base.py")
class TestStrayColorMetric(unittest.TestCase):
    """_stray_color_metrics counts fg pixels whose rendered colour is > thr off."""

    def test_counts_and_fraction(self):
        rgb = np.zeros((5, 6, 3), dtype=np.float64)
        ren = rgb.copy()
        ren[0, 0] = (1.0, 1.0, 1.0)      # diff 1.0 -> counted
        ren[1, 1] = (0.30, 0.0, 0.0)     # diff 0.30 -> counted
        ren[2, 2] = (0.25, 0.0, 0.0)     # diff exactly 0.25 -> not counted (>)
        fg = np.ones((5, 6), dtype=bool)
        fg[4, :] = False
        stray, frac = LB._stray_color_metrics(rgb, ren, fg, 0.25)
        self.assertEqual(stray, 2)
        self.assertAlmostEqual(frac, 2.0 / 24.0, places=12)


class TestCLISmoke(unittest.TestCase):
    """Default-parameter CLI smoke test on a synthetic white-canvas image."""

    WORK_RES = 128

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="layered_smoke_")
        cls.addClassCleanup(cls._tmp.cleanup)
        root = cls._tmp.name
        cls.input_png = S.write_scene(os.path.join(root, "canvas.png"),
                                      S.scene_canvas(192, 144))
        cls.outdir = os.path.join(root, "out")
        S.run_layered_ok(cls, ["--input", cls.input_png, "--outdir", cls.outdir,
                               "--work-res", str(cls.WORK_RES), "--pass4"],
                         label="CLI smoke (default params + --pass4)")
        cls.report = S.load_json(os.path.join(cls.outdir, "layered_base_report.json"))
        cls.strokes = S.load_json(os.path.join(cls.outdir, "layered_base_strokes.json"))
        cls.png = os.path.join(cls.outdir, f"layered_base_R{cls.WORK_RES}.png")

    def test_outputs_exist_and_finite_metrics(self):
        self.assertTrue(os.path.isfile(self.png), "rendered PNG missing")
        self.assertTrue(os.path.isfile(os.path.join(self.outdir, "layered_base_strokes.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.outdir, "layered_base_report.json")))
        with Image.open(self.png) as im:
            self.assertEqual(im.size, (128, 96))
        self.assertTrue(math.isfinite(float(self.report["psnr"])))
        self.assertGreater(float(self.report["psnr"]), 0.0)
        self.assertTrue(math.isfinite(float(self.report["ssim"])))
        self.assertGreaterEqual(float(self.report["ssim"]), 0.0)
        self.assertLessEqual(float(self.report["ssim"]), 1.0)
        self.assertGreater(int(self.report["strokes_out"]), 0)
        white = int(self.report["white_px_in_fg"])
        if self.report.get("silhouette_on"):
            # v1.37: the loose fg mask includes the soft halo; only the
            # eroded silhouette is painted, so the ring between them stays
            # white.  The white-in-fg area must equal the un-painted part.
            fg_px = int(S.fg_mask(S.load_rgb(self.input_png,
                                            self.WORK_RES)).sum())
            self.assertGreater(fg_px, 0)
            expected = 1.0 - float(self.report["silhouette_area_frac"])
            self.assertAlmostEqual(white / fg_px, expected, delta=0.01)
        else:
            # v1.36 wash sweeps can leave a handful of white antialiasing
            # pixels in the foreground; allow <=0.5% of the canvas.
            self.assertLessEqual(white, max(20, int(0.005 * 128 * 96)))

    def test_report_schema_and_pass_accounting(self):
        required = {
            "input", "work_res", "size", "strokes_out", "pass1_full",
            "pass2_local", "pass3_local", "pass4_local", "detail",
            "white_px_in_fg", "psnr", "ssim", "png", "strokes", "seconds",
        }
        missing = required - set(self.report)
        self.assertFalse(missing, f"report missing keys: {sorted(missing)}")
        self.assertEqual(self.report["work_res"], self.WORK_RES)
        self.assertEqual(self.report["size"], [128, 96])
        self.assertEqual(int(self.report["strokes_out"]), len(self.strokes))
        pass_total = sum(int(self.report[key]) for key in
                         ("pass1_full", "pass2_local", "pass3_local", "pass4_local"))
        dropped = int(self.report.get("silhouette_dropped_strokes", 0))
        # v1.37 silhouette clipping runs after generation and before the
        # cap, so the per-pass generation counts include the dropped halo
        # strokes (exactly: generated - dropped == saved).
        self.assertEqual(pass_total + int(self.report["detail"]) - dropped,
                         int(self.report["strokes_out"]))
        if self.report.get("silhouette_on"):
            self.assertGreaterEqual(dropped, 0)
        else:
            self.assertEqual(dropped, 0)
        self.assertGreater(int(self.report["pass1_full"]), 0)
        self.assertGreater(int(self.report["pass4_local"]), 0)
        self.assertEqual(int(self.report["detail"]), 0)

    def test_strokes_invariants_and_no_gradient(self):
        scale = float(max(self.report["size"]))  # L = max(W, H)
        # v1.35: default ground is the single-coat toned pass (kind base_toned).
        kind_layer = {"base_toned": 0, "base_pass1": 0, "base_pass2": 1,
                      "base_pass3": 2, "base_pass4": 3}
        gradient_count = 0
        for stroke in self.strokes:
            points = np.asarray(stroke["points"], dtype=np.float64)
            # v1.35 row coats and v1.36 wash sweeps may be polylines.
            self.assertGreaterEqual(len(points), 2, "stroke needs >=2 points")
            self.assertGreaterEqual(float(points.min()), 0.0)
            self.assertLessEqual(float(points.max()), 1.0)
            widths = np.asarray(stroke["widths"], dtype=np.float64)
            self.assertEqual(len(widths), len(points))
            self.assertTrue(np.all(widths > 0.0))
            color = np.asarray(stroke["color"], dtype=np.float64)
            self.assertEqual(color.shape, (3,))
            self.assertGreaterEqual(float(color.min()), 0.0)
            self.assertLessEqual(float(color.max()), 1.0)
            self.assertEqual(stroke["alpha"], 1.0)
            self.assertIs(stroke["closed"], False)
            self.assertIn(stroke["kind"], kind_layer)
            self.assertEqual(stroke["layer"], kind_layer[stroke["kind"]])
            if "gradient" in stroke:
                gradient_count += 1
        self.assertEqual(gradient_count, 0, "--upper-gradient was not requested")
        if "gradient_strokes" in self.report:  # v1.33 report keeps the count too
            self.assertEqual(int(self.report["gradient_strokes"]), 0)
            self.assertEqual(int(self.report["gradient_candidates"]), 0)
        # v1.35 default ground contract
        self.assertEqual(self.report.get("base_color_mode"), "toned")
        self.assertEqual(int(self.report.get("base_strokes", -1)),
                         int(self.report["pass1_full"]))
        self.assertGreater(int(self.report["pass1_full"]), 0)
        self.assertEqual(int(self.report.get("base_color_std", -1)), 0)
        self.assertEqual(int(self.report.get("base_gradient_strokes", -1)), 0)
        self.assertLessEqual(int(self.report["strokes_out"]), 100000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
