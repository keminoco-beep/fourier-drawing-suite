"""``fourier_prompt.brush.calib`` 测试：实测数值口径/尺寸线性/压力单调/fail-loud。

数值来自 ``poc/brushcalib/out/brush_calib_measured.json``（analysis/13a 表 2.1/2.2）。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fourier_prompt.brush.calib import (  # noqa: E402
    CALIB_VERSION,
    DEFAULT_CALIB_PATH,
    BrushCalib,
    BrushCalibError,
    UnknownPresetError,
)
from fourier_prompt.brush.catalog import BrushCatalog  # noqa: E402


class TestCalibData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal = BrushCalib.load()
        cls.cat = BrushCatalog.load()

    def test_schema_and_provenance(self):
        data = self.cal.as_dict()
        self.assertEqual(data["schema"], CALIB_VERSION)
        self.assertTrue(data["generated_from"].endswith("brush_calib_measured.json"))
        self.assertIn("_build", data["generated_by"])
        self.assertTrue(data["generated_from_sha256"])
        self.assertEqual(self.cal.presets(), self.cat.ids())

    def test_validate_ok_with_catalog(self):
        self.assertEqual(self.cal.validate(catalog=self.cat), [])

    def test_width_eff_measured_groups(self):
        # 13a §2.2：round/ink/marker/watercolor≈1.0、bristle/chalk/pencil≈0.96–0.98、soft=0.749、airbrush=0.333
        self.assertAlmostEqual(self.cal.width_eff("ink_gpen"), 1.001, places=3)
        self.assertAlmostEqual(self.cal.width_eff("basic_round_hard"), 1.000, places=2)
        self.assertAlmostEqual(self.cal.width_eff("basic_round_soft"), 0.749, places=3)
        self.assertAlmostEqual(self.cal.width_eff("basic_airbrush"), 0.333, places=3)
        self.assertAlmostEqual(self.cal.width_eff("watercolor_wash"), 0.999, places=2)
        for pid in ("chalk", "charcoal", "pencil_2b", "spatter_brush"):
            self.assertGreaterEqual(self.cal.width_eff(pid), 0.95, pid)
            self.assertLessEqual(self.cal.width_eff(pid), 1.0, pid)

    def test_response_measured_values(self):
        r = self.cal.response("ink_gpen", 12, 1.0)
        self.assertEqual(r["size_bucket"], 12.0)
        self.assertTrue(r["measured"])
        self.assertFalse(r["interpolated"])
        self.assertAlmostEqual(r["width_px"], 6.007, places=2)
        self.assertAlmostEqual(r["alpha"], 1.0, places=3)
        self.assertAlmostEqual(r["width_nominal_px"], 6.0, places=3)

        soft = self.cal.response("basic_round_soft", 12, 1.0)
        self.assertAlmostEqual(soft["width_px"], 9.437, places=2)
        self.assertAlmostEqual(soft["width_eff"], 0.749, places=3)

        air = self.cal.response("basic_airbrush", 12, 1.0)
        self.assertAlmostEqual(air["width_px"], 5.997, places=2)
        self.assertAlmostEqual(air["alpha"], 0.740, places=3)
        # airbrush 峰值 α 永不饱和（13a §2.5）
        self.assertLess(air["alpha"], 0.80)

    def test_response_alpha_matches_design_mapping(self):
        # alpha_lo + (hi-lo)*p（实测就是这条映射；color.a 已归一到 1.0）
        for pid in ("basic_round_hard", "ink_gpen", "watercolor_wash", "spatter_brush"):
            lo, hi = self.cat.get(pid)["alpha_range"]
            for p in (0.25, 0.5, 0.75):
                expected = lo + (hi - lo) * p
                got = self.cal.response(pid, 12, p)["alpha"]
                self.assertAlmostEqual(got, expected, delta=0.02, msg=f"{pid} p={p}")

    def test_width_monotonic_in_pressure(self):
        for pid in self.cat.ids():
            widths = [self.cal.response(pid, 12, p)["width_px"] for p in (0.0, 0.25, 0.5, 0.75, 1.0)]
            self.assertEqual(widths, sorted(widths), f"{pid} 的宽度随压力不单调: {widths}")

    def test_width_gain_matches_measured_ratio(self):
        # sumi_e 是压力动态范围最大的一支：w(1.0)/w(0.25)=2.52（13a §2.1）
        w25 = self.cal.response("sumi_e", 12, 0.25)["width_px"]
        w100 = self.cal.response("sumi_e", 12, 1.0)["width_px"]
        self.assertAlmostEqual(w100 / w25, 2.52, delta=0.03)
        # fineliner 最弱：1.23
        self.assertAlmostEqual(
            self.cal.response("ink_fineliner", 12, 1.0)["width_px"]
            / self.cal.response("ink_fineliner", 12, 0.25)["width_px"],
            1.23,
            delta=0.03,
        )

    def test_size_linearity(self):
        # 尺寸 4→120 线性（斜率 0.999–1.002）：24px 应≈12px 的 2 倍
        for pid in self.cat.ids():
            w12 = self.cal.response(pid, 12, 1.0)["width_px"]
            w24 = self.cal.response(pid, 24, 1.0)["width_px"]
            self.assertAlmostEqual(w24 / w12, 2.0, delta=0.02, msg=pid)

    def test_pressure_clamped_not_silent(self):
        r = self.cal.response("ink_gpen", 12, 1.7)
        self.assertTrue(r["pressure_clamped"])
        self.assertEqual(r["pressure"], 1.0)
        self.assertEqual(r["pressure_input"], 1.7)
        r2 = self.cal.response("ink_gpen", 12, -0.5)
        self.assertTrue(r2["pressure_clamped"])
        self.assertEqual(r2["pressure"], 0.0)

    def test_unknown_preset_fails_loud(self):
        with self.assertRaises(UnknownPresetError) as ctx:
            self.cal.response("nope_brush", 12, 1.0)
        self.assertIsInstance(ctx.exception, KeyError)
        with self.assertRaises(KeyError):
            self.cal.width_eff("nope_brush")
        with self.assertRaises(KeyError):
            self.cal.traits("nope_brush")

    def test_bad_size_raises(self):
        with self.assertRaises(BrushCalibError):
            self.cal.response("ink_gpen", 0, 1.0)
        with self.assertRaises(BrushCalibError):
            self.cal.response("ink_gpen", -3, 1.0)

    def test_traits_match_catalog(self):
        for pid in self.cat.ids():
            self.assertEqual(self.cal.traits(pid), self.cat.get(pid)["traits"], pid)
            self.assertTrue(self.cal.traits(pid), f"{pid} 没有实测标签")

    def test_anisotropy(self):
        self.assertAlmostEqual(self.cal.anisotropy("marker_chisel"), 1.6, delta=0.02)
        self.assertAlmostEqual(self.cal.anisotropy("dry_flat"), 1.6, delta=0.02)
        self.assertAlmostEqual(self.cal.anisotropy("ink_gpen"), 1.0, places=3)

    def test_sizes_per_preset(self):
        sizes = self.cal.sizes("ink_gpen")
        for expected in (4.0, 8.0, 12.0, 16.0, 32.0, 44.0, 48.0, 64.0, 120.0):
            self.assertIn(expected, sizes)
        self.assertEqual(self.cal.sizes("basic_round_soft"), [12.0, 44.0, 48.0])

    def test_validate_detects_corruption(self):
        data = json.loads(Path(DEFAULT_CALIB_PATH).read_text(encoding="utf-8"))
        data["presets"]["chalk"]["width_eff"] = 2.0
        data["presets"]["chalk"]["sizes"]["12"]["pressure"]["1.0"]["alpha"] = 1.5
        errs = BrushCalib.from_dict(data).validate()
        self.assertTrue(any("width_eff" in e for e in errs))
        self.assertTrue(any("alpha 越界" in e for e in errs))

    def test_validate_detects_missing_catalog_entry(self):
        data = json.loads(Path(DEFAULT_CALIB_PATH).read_text(encoding="utf-8"))
        del data["presets"]["spatter_brush"]
        errs = BrushCalib.from_dict(data).validate(catalog=self.cat)
        self.assertTrue(any("spatter_brush" in e for e in errs))

    def test_load_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            BrushCalib.load(_ROOT / "assets" / "brush" / "__nope__.json")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
