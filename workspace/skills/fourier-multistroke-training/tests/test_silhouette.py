#!/usr/bin/env python
"""v1.37 silhouette-aware stroke generation: generation-time clipping tests.

v1.37 silhouette-aware stroke generation: generation-time geometry clipping of
every non-ground stroke inside an eroded subject silhouette (endpoints pulled
back + per-point width clamped + too-thin strokes dropped), plus silhouette
feather compositing.  No post-processing of the rendered image.

Frozen interface (to be confirmed by Lead/fixer-core):
  --silhouette {auto,on,off}    default auto
  --silhouette-shrink-px        1.0
  --silhouette-margin-px        0.25
  --silhouette-min-width-px     0.6
  --silhouette-feather-px       0.8
  auto: wash ground + paint_order!=legacy + clip_to_fg!=off + white_bg>2%
  report: silhouette_mode/on/area_frac/fallback/shrink_px/margin_px/
          min_width_px/feather_px/clipped_strokes/points_trimmed/
          dropped_strokes/overshoot_before_max_px/overshoot_after_max_px
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import unittest

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi

try:  # unittest discover -s tests
    import _support as S
    import hatch_gen as hgr
except ImportError:  # tests.test_silhouette
    from tests import _support as S
    import hatch_gen as hgr

WORK_RES = 128
WORK_RES_BIG = 256
FG_WHITE_THR = 0.945

FROZEN_SILHOUETTE_DEFAULTS = {
    "--silhouette": "auto",
    "--silhouette-shrink-px": 1.0,
    "--silhouette-margin-px": 0.25,
    "--silhouette-min-width-px": 0.6,
    "--silhouette-feather-px": 0.8,
}

REPORT_SILHOUETTE_KEYS = [
    "silhouette_mode", "silhouette_on", "silhouette_area_frac",
    "silhouette_fallback", "silhouette_shrink_px", "silhouette_margin_px",
    "silhouette_min_width_px", "silhouette_feather_px",
    "silhouette_clipped_strokes", "silhouette_points_trimmed",
    "silhouette_dropped_strokes", "silhouette_overshoot_before_max_px",
    "silhouette_overshoot_after_max_px",
]


# ------------------------------------------------------------------ scenes
def scene_white_circle(H=144, W=192, r_frac=0.30):
    """White background + coloured disc with a 4px soft halo rim."""
    yy, xx = np.mgrid[0:H, 0:W]
    cy, cx = H / 2.0, W / 2.0
    r = r_frac * min(H, W)
    d = np.hypot(yy - cy, xx - cx)
    img = np.ones((H, W, 3), dtype=np.float64)
    inside = d <= r
    img[inside] = np.array([0.18, 0.35, 0.62])
    rim = (d > r) & (d <= r + 4)
    t = (d[rim] - r) / 4.0
    img[rim] = np.array([0.18, 0.35, 0.62]) * (1 - t)[:, None] + t[:, None]
    return np.clip(img, 0, 1)


def scene_white_triangle(H=144, W=192):
    im = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(im)
    pts = [(int(W / 2), int(H * 0.18)), (int(W * 0.20), int(H * 0.82)),
           (int(W * 0.80), int(H * 0.82))]
    d.polygon(pts, fill=(236, 232, 230))
    core = [(int(W / 2), int(H * 0.18) + 3), (int(W * 0.20) + 4, int(H * 0.82) - 3),
            (int(W * 0.80) - 4, int(H * 0.82) - 3)]
    d.polygon(core, fill=(158, 61, 51))
    return np.asarray(im, dtype=np.float64) / 255.0


def scene_thin_strip(H=144, W=192, thick=4):
    yy, xx = np.mgrid[0:H, 0:W]
    img = np.ones((H, W, 3), dtype=np.float64)
    x0, y0 = W * 0.15, H * 0.85
    x1, y1 = W * 0.85, H * 0.15
    dx, dy = x1 - x0, y1 - y0
    t = ((xx - x0) * dx + (yy - y0) * dy) / (dx * dx + dy * dy)
    u = np.abs((xx - x0) * dy - (yy - y0) * dx) / np.hypot(dx, dy)
    inside = (t >= 0) & (t <= 1) & (u <= thick / 2.0)
    img[inside] = np.array([0.20, 0.55, 0.30])
    return np.clip(img, 0, 1)


def scene_colored_bg(H=144, W=192):
    """Full colour field: white_background_frac == 0 -> silhouette auto is OFF."""
    yy, xx = np.mgrid[0:H, 0:W]
    img = np.empty((H, W, 3), dtype=np.float64)
    img[...] = np.array([0.22, 0.34, 0.55])
    d = np.hypot(yy - H * 0.5, xx - W * 0.5)
    img[d <= 0.30 * min(H, W)] = np.array([0.80, 0.55, 0.22])
    return np.clip(img, 0, 1)


# --------------------------------------------------------------- metrics
def fg_mask(rgb):
    return ~np.all(np.asarray(rgb) > FG_WHITE_THR, axis=2)


def silhouette_masks(rgb, shrink_px=1.0):
    fg = fg_mask(rgb)
    dist_fg = ndi.distance_transform_edt(fg)
    sil = dist_fg > float(shrink_px)
    return fg, sil, dist_fg


def stroke_bbox_mask(stroke, H, W):
    L = float(max(H, W))
    pts = np.asarray(stroke["points"], dtype=np.float64) * L
    wds = np.asarray(stroke["widths"], dtype=np.float64) * L
    hw = float(wds.max()) * 0.5 + 2.0
    x0 = int(max(0, np.floor(float(pts[:, 0].min()) - hw)))
    x1 = int(min(W, np.ceil(float(pts[:, 0].max()) + hw) + 1))
    y0 = int(max(0, np.floor(float(pts[:, 1].min()) - hw)))
    y1 = int(min(H, np.ceil(float(pts[:, 1].max()) + hw) + 1))
    if x1 <= x0 or y1 <= y0:
        return np.zeros((0, 0), bool), (0, 0)
    h, w = y1 - y0, x1 - x0
    ls = float(max(h, w, 1))
    probe = {
        "points": (pts - np.array([x0, y0], dtype=np.float64)) / ls,
        "widths": np.clip(wds, 0.5, None) / ls,
        "color": [1.0, 0.0, 1.0], "alpha": 1.0, "layer": 0, "closed": False,
    }
    rend = hgr._render_strokes([probe], h, w, ss=1)
    return (rend[:, :, 1] < 0.5), (x0, y0)


def stroke_overshoot_max(stroke, dist_sil, H, W):
    L = float(max(H, W))
    pts = np.asarray(stroke["points"], dtype=np.float64) * L
    wds = np.asarray(stroke["widths"], dtype=np.float64) * L
    if len(pts) == 0:
        return 0.0
    xi = np.clip(pts[:, 0].astype(int), 0, W - 1)
    yi = np.clip(pts[:, 1].astype(int), 0, H - 1)
    return float(np.max(wds * 0.5 - dist_sil[yi, xi]))


def silhouette_stats(strokes, rgb, shrink_px=1.0, outside_tol_px=1.0):
    fg, sil, _ = silhouette_masks(rgb, shrink_px)
    H, W = sil.shape
    dist_sil = ndi.distance_transform_edt(sil)
    tol = ndi.distance_transform_edt(~sil) <= float(outside_tol_px)
    formal = [s for s in strokes if str(s.get("kind", "")) not in
              ("base_toned", "base_pass1")]
    overs = np.asarray([stroke_overshoot_max(s, dist_sil, H, W)
                        for s in formal], dtype=np.float64)
    outside_px = 0
    outside_strokes = 0
    for s in formal:
        mask, (x0, y0) = stroke_bbox_mask(s, H, W)
        if mask.size == 0:
            continue
        sub = tol[y0:y0 + mask.shape[0], x0:x0 + mask.shape[1]]
        n_out = int((mask & ~sub).sum())
        if n_out:
            outside_strokes += 1
            outside_px += n_out
    return {
        "formal": len(formal),
        "overshoot_gt_2": int((overs > 2.0).sum()),
        "overshoot_max": float(overs.max()) if overs.size else 0.0,
        "outside_px_tol1": outside_px,
        "outside_strokes": outside_strokes,
    }


# ---------------------------------------------------------------- helpers
def _run_scene(tmpdir, label, scene, extra, work_res=WORK_RES):
    root = os.path.join(tmpdir, label)
    os.makedirs(root, exist_ok=True)
    inp = os.path.join(root, "scene.png")
    S.write_scene(inp, scene)
    outdir = os.path.join(root, "out")
    S.run_layered_ok(None, ["--input", inp, "--outdir", outdir,
                            "--work-res", str(work_res)] + list(extra),
                     label=label)
    report = S.load_json(os.path.join(outdir, "layered_base_report.json"))
    strokes = S.load_json(os.path.join(outdir, "layered_base_strokes.json"))
    rgb = S.load_rgb(inp, work_res)
    png = os.path.join(outdir, f"layered_base_R{report['work_res']}.png")
    return {"report": report, "strokes": strokes, "rgb": rgb,
            "png": png, "outdir": outdir, "work_res": work_res}


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _formal(run):
    return [s for s in run["strokes"]
            if str(s.get("kind", "")) not in ("base_toned", "base_pass1")]


def _base(run):
    return [s for s in run["strokes"] if str(s.get("kind", "")) == "base_toned"]


class TestSilhouetteInterface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.help_text = S.script_help(S.DEFAULT_LAYERED)
        cls.defaults = S.argparse_defaults(S.DEFAULT_LAYERED)
        cls.choices = S.argparse_choices(S.DEFAULT_LAYERED)

    def test_help_lists_new_options(self):
        for option in FROZEN_SILHOUETTE_DEFAULTS:
            self.assertIn(option, self.help_text,
                          f"{option} missing from layered_base.py --help")

    def test_frozen_silhouette_defaults(self):
        problems = []
        for option, expected in FROZEN_SILHOUETTE_DEFAULTS.items():
            if option not in self.defaults:
                problems.append(f"{option}: missing")
            elif self.defaults[option] != expected:
                problems.append(f"{option}: {self.defaults[option]!r} != {expected!r}")
        self.assertFalse(problems, "\n".join(problems))
        self.assertEqual(self.choices.get("--silhouette"), ["auto", "on", "off"])


class TestSilhouetteOn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="silhouette_on_")
        cls.addClassCleanup(cls._tmp.cleanup)
        scene = scene_white_circle(144, 192)
        cls.on_run = _run_scene(cls._tmp.name, "circle_on", scene,
                                ["--silhouette", "on"])
        cls.off_run = _run_scene(cls._tmp.name, "circle_off", scene,
                                 ["--silhouette", "off"])
        cls.report = cls.on_run["report"]

    def test_report_contract(self):
        report = self.report
        for key in REPORT_SILHOUETTE_KEYS:
            self.assertIn(key, report, f"report missing {key}")
        self.assertEqual(report["silhouette_mode"], "on")
        self.assertTrue(report["silhouette_on"])
        self.assertFalse(report["silhouette_fallback"])
        self.assertAlmostEqual(float(report["silhouette_shrink_px"]), 1.0)
        self.assertAlmostEqual(float(report["silhouette_margin_px"]), 0.25)
        self.assertAlmostEqual(float(report["silhouette_min_width_px"]), 0.6)
        self.assertAlmostEqual(float(report["silhouette_feather_px"]), 0.8)
        self.assertGreater(int(report["silhouette_clipped_strokes"]), 0)
        self.assertGreaterEqual(int(report["silhouette_points_trimmed"]), 0)
        self.assertGreater(int(report["silhouette_dropped_strokes"]), 0)

    def test_report_stats_prove_generation_time_clipping(self):
        report = self.report
        before = float(report["silhouette_overshoot_before_max_px"])
        after = float(report["silhouette_overshoot_after_max_px"])
        print(f"[silhouette] report overshoot {before:.2f}px -> {after:.2f}px "
              f"clipped={report['silhouette_clipped_strokes']} "
              f"dropped={report['silhouette_dropped_strokes']}")
        self.assertGreaterEqual(before, 2.0,
                                "scene must trigger real overshoot to clip")
        self.assertLessEqual(after, 1.0 + 1e-6,
                             "clipped strokes must not overshoot the silhouette")
        self.assertGreaterEqual(report["silhouette_area_frac"], 0.0)
        self.assertLessEqual(report["silhouette_area_frac"], 1.0)

    def test_geometry_has_no_overshoot(self):
        stats = silhouette_stats(self.on_run["strokes"], self.on_run["rgb"],
                                 shrink_px=1.0, outside_tol_px=1.0)
        print(f"[silhouette] on: formal={stats['formal']} "
              f"overshoot>2px={stats['overshoot_gt_2']} "
              f"max={stats['overshoot_max']:.2f}px "
              f"outside_px={stats['outside_px_tol1']}")
        self.assertEqual(stats["overshoot_gt_2"], 0)
        self.assertLessEqual(stats["overshoot_max"], 1.0 + 1e-6)
        self.assertLessEqual(stats["outside_px_tol1"], 4,
                             "stroke footprints must stay inside the silhouette")

    def test_base_ground_untouched(self):
        base_on = _base(self.on_run)
        base_off = _base(self.off_run)
        self.assertEqual(len(base_on), len(base_off))
        stats = S.stroke_color_stats(base_on)
        self.assertEqual(stats["unique"], 1)
        self.assertLessEqual(max(stats["std"]), 1e-6)
        for stroke in base_on:
            self.assertNotIn("gradient", stroke)


class TestSilhouetteOff(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="silhouette_off_")
        cls.addClassCleanup(cls._tmp.cleanup)
        scene = scene_white_circle(144, 192)
        cls.off_run = _run_scene(cls._tmp.name, "circle_off", scene,
                                 ["--silhouette", "off"])
        cls.on_run = _run_scene(cls._tmp.name, "circle_on", scene,
                                ["--silhouette", "on"])

    def test_off_reports_no_clipping(self):
        report = self.off_run["report"]
        self.assertEqual(report["silhouette_mode"], "off")
        self.assertFalse(report["silhouette_on"])
        self.assertIsNone(report["silhouette_area_frac"])
        self.assertEqual(int(report["silhouette_clipped_strokes"]), 0)
        self.assertEqual(int(report["silhouette_points_trimmed"]), 0)
        self.assertEqual(int(report["silhouette_dropped_strokes"]), 0)
        self.assertAlmostEqual(float(report["silhouette_overshoot_before_max_px"]), 0.0)
        self.assertAlmostEqual(float(report["silhouette_overshoot_after_max_px"]), 0.0)

    def test_off_actually_overshoots(self):
        stats = silhouette_stats(self.off_run["strokes"], self.off_run["rgb"],
                                 shrink_px=1.0, outside_tol_px=1.0)
        print(f"[silhouette] off: formal={stats['formal']} "
              f"overshoot>2px={stats['overshoot_gt_2']} "
              f"max={stats['overshoot_max']:.2f}px "
              f"outside_px={stats['outside_px_tol1']}")
        self.assertGreater(stats["overshoot_gt_2"], 0,
                           "scene must show the defect when silhouette is off")
        self.assertGreater(stats["outside_px_tol1"], 100,
                           "off path should paint outside the silhouette")


class TestAutoSemantics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="silhouette_auto_")
        cls.addClassCleanup(cls._tmp.cleanup)
        circle = scene_white_circle(144, 192)
        colored = scene_colored_bg(144, 192)
        cls.white_auto = _run_scene(cls._tmp.name, "white_auto", circle, [])
        cls.legacy_run = _run_scene(
            cls._tmp.name, "legacy_auto", circle,
            ["--base-color-mode", "quantized"])
        cls.colored_auto = _run_scene(cls._tmp.name, "colored_auto", colored, [])
        cls.colored_off = _run_scene(cls._tmp.name, "colored_off", colored,
                                     ["--silhouette", "off"])

    def test_auto_on_for_v136_style_white_background(self):
        report = self.white_auto["report"]
        self.assertEqual(report["silhouette_mode"], "auto")
        self.assertTrue(report["silhouette_on"],
                        "auto must enable silhouette for wash/region white-bg art")
        self.assertGreater(report["white_background_frac"], 0.02)
        self.assertGreater(int(report["silhouette_clipped_strokes"]), 0)

    def test_auto_off_for_legacy_mode(self):
        report = self.legacy_run["report"]
        self.assertEqual(report["silhouette_mode"], "auto")
        self.assertFalse(report["silhouette_on"],
                         "legacy commands must stay byte-identical (silhouette off)")
        self.assertEqual(report["base_color_mode"], "quantized")

    def test_auto_off_without_white_background(self):
        report = self.colored_auto["report"]
        self.assertFalse(report["silhouette_on"])
        self.assertLessEqual(report["white_background_frac"], 0.02)

    def test_explicit_off_equals_auto_disabled_render(self):
        # On a coloured background auto is disabled, so an explicit --silhouette
        # off run must render byte-identically (no hidden clipping/feather).
        self.assertEqual(_sha(self.colored_auto["png"]), _sha(self.colored_off["png"]),
                         "silhouette off must equal the old (disabled) render path")
        self.assertEqual(self.colored_off["report"]["silhouette_mode"], "off")
        self.assertFalse(self.colored_off["report"]["silhouette_on"])


class TestThinFeatureProtection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="silhouette_thin_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.strip_run = _run_scene(cls._tmp.name, "strip4_on",
                                   scene_thin_strip(144, 192, 4),
                                   ["--silhouette", "on"])
        cls.fallback_run = _run_scene(cls._tmp.name, "fallback",
                                      scene_white_circle(144, 192),
                                      ["--silhouette", "on",
                                       "--silhouette-shrink-px", "30"])

    def test_thin_strip_does_not_crash(self):
        report = self.strip_run["report"]
        self.assertTrue(report["silhouette_on"])
        self.assertGreater(int(report["strokes_out"]), 0)
        self.assertTrue(math.isfinite(float(report["psnr"])))
        self.assertGreaterEqual(int(report["silhouette_dropped_strokes"]), 0)
        # base ground survives; dropped formal strokes are expected on a 4px strip
        self.assertGreaterEqual(len(_base(self.strip_run)), 1)

    def test_silhouette_area_fallback_is_reported(self):
        report = self.fallback_run["report"]
        print(f"[silhouette] fallback: on={report['silhouette_on']} "
              f"area_frac={report['silhouette_area_frac']} "
              f"fallback={report['silhouette_fallback']}")
        self.assertTrue(report["silhouette_on"])
        self.assertTrue(report["silhouette_fallback"],
                        "an over-shrunk silhouette must fall back to the raw fg")
        self.assertAlmostEqual(float(report["silhouette_area_frac"]), 1.0)
        self.assertGreater(int(report["strokes_out"]), 0)
        self.assertTrue(math.isfinite(float(report["psnr"])))


class TestSilhouetteQuality(unittest.TestCase):
    """Large subject: clipping the halo strokes must not hurt the render."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="silhouette_quality_")
        cls.addClassCleanup(cls._tmp.cleanup)
        scene = scene_white_circle(256, 256)
        cls.on_run = _run_scene(cls._tmp.name, "big_on", scene,
                                ["--silhouette", "on"], work_res=WORK_RES_BIG)
        cls.off_run = _run_scene(cls._tmp.name, "big_off", scene,
                                 ["--silhouette", "off"], work_res=WORK_RES_BIG)

    def test_quality_and_budget(self):
        on = self.on_run["report"]
        off = self.off_run["report"]
        print(f"[silhouette] quality @{WORK_RES_BIG}: "
              f"off psnr={off['psnr']:.3f} ssim={off['ssim']:.4f} | "
              f"on psnr={on['psnr']:.3f} ssim={on['ssim']:.4f}")
        self.assertGreaterEqual(float(on["psnr"]), float(off["psnr"]) - 0.5,
                                "removing halo strokes must not hurt PSNR")
        self.assertTrue(math.isfinite(float(on["ssim"])))
        self.assertLessEqual(int(on["strokes_out"]), 100000)
        self.assertEqual(int(on["fill_count"]), 0)
        stats = silhouette_stats(self.on_run["strokes"], self.on_run["rgb"])
        self.assertEqual(stats["overshoot_gt_2"], 0)
        self.assertLessEqual(stats["outside_px_tol1"], 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
