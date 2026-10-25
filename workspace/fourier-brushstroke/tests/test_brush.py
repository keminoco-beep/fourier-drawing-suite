"""Stage3 笔刷属性曲线单元测试（stdlib unittest，无网络）。

覆盖：6 种 interp 求值语义、三种 taper 表达数值等价（A14）、step 区间语义、
颜色 stops 边界/硬跳变/不平均墨色、形状曲线（D4）、select_shape 规则、
常数压缩（体积纪律）、build_brush 集成与性能回归。
"""
from __future__ import annotations

import math
import os
import sys
import time
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from fbs_core import attribute, brush  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.util import FBSError, WarningLog, sha256_json  # noqa: E402

CONTRACT = {"target_resolution": 1024, "epsilon_px": 0.5}


def make_s(m: int = 200) -> np.ndarray:
    return np.linspace(0.0, 1.0, m)


def eval_num(curve, t):
    return attribute.eval_numeric(curve, t)


def constant_curve(curve: dict) -> bool:
    return curve.get("interp") == "constant" and len(curve.get("keys", [])) == 1 \
        and float(curve["keys"][0][0]) == 0.0


class TestInterpSemantics(unittest.TestCase):
    """六种 interp 的求值语义（用唯一口径 attribute.eval_numeric 复核）。"""

    def test_constant(self):
        t = np.array([0.0, 0.3, 1.0])
        np.testing.assert_allclose(eval_num({"interp": "constant", "keys": [[0.0, 0.25]]}, t), 0.25)

    def test_linear(self):
        t = np.array([0.0, 0.25, 0.5, 1.0])
        curve = {"interp": "linear", "keys": [[0.0, 1.0], [1.0, 3.0]], "clamp": True}
        np.testing.assert_allclose(eval_num(curve, t), 1.0 + 2.0 * t, atol=1e-12)

    def test_spline_catmull_rom(self):
        # Catmull-Rom：必须过全部关键点；共线数据在中间段精确复现直线
        keys = [[k / 4.0, k / 2.0] for k in range(5)]  # v = 2t
        curve = {"interp": "spline", "keys": keys, "clamp": True}
        got_keys = eval_num(curve, np.asarray([k[0] for k in keys]))
        np.testing.assert_allclose(got_keys, [k[1] for k in keys], atol=1e-12)
        # 中间两段（不受端点虚拟点处理影响）必须精确线性
        t_mid = np.linspace(0.25, 0.75, 9)
        np.testing.assert_allclose(eval_num(curve, t_mid), 2.0 * t_mid, atol=1e-9)
        # 端点段允许端点切向近似带来的有界偏差（不影响过点与连续性）
        t_all = np.linspace(0.0, 1.0, 33)
        self.assertLess(float(np.max(np.abs(eval_num(curve, t_all) - 2.0 * t_all))), 0.1)

    def test_step_interval_semantics(self):
        t = np.array([0.0, 0.499, 0.5, 0.501, 1.0])
        curve = {"interp": "step", "keys": [[0.0, 1.0], [0.5, 2.0]], "clamp": True}
        np.testing.assert_allclose(eval_num(curve, t), [1.0, 1.0, 2.0, 2.0, 2.0])

    def test_fourier(self):
        t = np.linspace(0.0, 1.0, 17)
        curve = {"interp": "fourier", "coef": [[0, 1.0, 0.0], [1, 0.5, 0.0]]}
        np.testing.assert_allclose(eval_num(curve, t), 1.0 + 0.5 * np.cos(2.0 * math.pi * t), atol=1e-12)

    def test_preset(self):
        t = np.linspace(0.0, 1.0, 17)
        curve = {"interp": "preset", "preset": "taper-out", "scale": 0.5, "strength": 0.6}
        # f = scale*(1-strength+strength*(1-t)) = 0.5 - 0.3 t
        np.testing.assert_allclose(eval_num(curve, t), 0.5 - 0.3 * t, atol=1e-12)


class TestTaperExpressions(unittest.TestCase):
    """A14：linear / spline / preset 三种「从粗到细」表达数值等价（两两差 < 1%）。"""

    def setUp(self):
        self.s = make_s(200)
        self.w0, self.w1 = 0.5, 0.1
        self.target = self.w0 + (self.w1 - self.w0) * self.s

    def test_three_taper_expressions_equivalent(self):
        t = np.linspace(0.0, 1.0, 32)
        built = brush.build_width_curve(self.s, self.target, 0.02)
        linear = {"interp": "linear", "keys": [[0.0, self.w0], [1.0, self.w1]], "clamp": True}
        spline = {"interp": "spline",
                  "keys": [[k / 8.0, self.w0 + (self.w1 - self.w0) * (k / 8.0)] for k in range(9)],
                  "clamp": True}
        preset = {"interp": "preset", "preset": "taper-out", "scale": self.w0,
                  "strength": 1.0 - self.w1 / self.w0}
        curves = {"built": built, "linear": linear, "spline": spline, "preset": preset}
        vals = {name: eval_num(curve, t) for name, curve in curves.items()}
        tol = 0.01 * self.w0
        names = list(vals)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                diff = float(np.max(np.abs(vals[names[i]] - vals[names[j]])))
                self.assertLess(diff, tol, f"{names[i]} vs {names[j]} differs by {diff}")

    def test_built_taper_is_faithful(self):
        built = brush.build_width_curve(self.s, self.target, 0.02)
        self.assertIn(built["interp"], ("linear", "spline", "preset"))
        err = float(np.max(np.abs(eval_num(built, self.s) - self.target)))
        self.assertLessEqual(err, 0.02 * self.w0 * 1.5)
        # 与 true taper 在 32 点的最大差 < 1% 最大宽度
        t = np.linspace(0.0, 1.0, 32)
        true_vals = self.w0 + (self.w1 - self.w0) * t
        self.assertLess(float(np.max(np.abs(eval_num(built, t) - true_vals))), 0.01 * self.w0)

    def test_ease_taper_uses_preset_or_faithful(self):
        # 二次缓动 profile：应能选 preset（ease-in）或其它保真表达，32 点误差 < 1%
        w = 0.02 + 0.3 * self.s * self.s
        built = brush.build_width_curve(self.s, w, 0.02)
        t = np.linspace(0.0, 1.0, 32)
        self.assertLess(float(np.max(np.abs(eval_num(built, t) - (0.02 + 0.3 * t * t)))), 0.01 * 0.32)


class TestStepWidth(unittest.TestCase):
    def test_step_interval_semantics(self):
        s = make_s(200)
        w = np.where(s < 0.4, 0.08, np.where(s < 0.75, 0.30, 0.12))
        curve = brush.build_width_curve(s, w, 0.02)
        self.assertEqual(curve["interp"], "step")
        self.assertEqual(len(curve["keys"]), 3)
        keys = curve["keys"]
        self.assertEqual(float(keys[0][0]), 0.0)
        self.assertAlmostEqual(float(keys[0][1]), 0.08)
        self.assertAlmostEqual(float(keys[1][1]), 0.30)
        self.assertAlmostEqual(float(keys[2][1]), 0.12)
        # 区间语义：左闭右开
        t = np.array([0.0, 0.39, keys[1][0], 0.74, keys[2][0], 1.0])
        got = eval_num(curve, t)
        np.testing.assert_allclose(got, [0.08, 0.08, 0.30, 0.30, 0.12, 0.12], atol=1e-12)
        # 对原始采样点必须保真
        self.assertLessEqual(float(np.max(np.abs(eval_num(curve, s) - w))), 0.02 * 0.30)

    def test_step_beats_linear_ramp(self):
        # 平均色不准用常量近似：明显分段常量必须输出 step，而不是 constant/线性斜坡
        s = make_s(100)
        w = np.where(s < 0.5, 0.30, 0.05)
        curve = brush.build_width_curve(s, w, 0.02)
        self.assertEqual(curve["interp"], "step")
        mid = float(eval_num(curve, np.array([0.25]))[0])
        self.assertAlmostEqual(mid, 0.30, places=6)
        mid2 = float(eval_num(curve, np.array([0.75]))[0])
        self.assertAlmostEqual(mid2, 0.05, places=6)


class TestColorCurve(unittest.TestCase):
    def test_constant_color(self):
        s = make_s(120)
        rgb = np.tile(np.array([51 / 255.0, 102 / 255.0, 153 / 255.0]), (len(s), 1))
        curve = brush.build_color_curve(s, rgb, np.ones(len(s)), 0.02)
        self.assertEqual(curve["interp"], "constant")
        self.assertEqual(len(curve["stops"]), 1)
        self.assertEqual(curve["stops"][0]["t"], 0.0)
        self.assertEqual(curve["stops"][0]["rgb"], [51, 102, 153])
        self.assertEqual(curve["stops"][0]["a"], 1.0)

    def test_hard_jump_and_no_averaging(self):
        s = make_s(200)
        rgb = np.zeros((len(s), 3))
        rgb[:100] = np.array([204, 26, 26]) / 255.0   # 红
        rgb[100:] = np.array([26, 26, 204]) / 255.0   # 蓝
        curve = brush.build_color_curve(s, rgb, np.ones(len(s)), 0.02)
        self.assertEqual(curve["interp"], "linear")
        stops = curve["stops"]
        ts = [float(st["t"]) for st in stops]
        self.assertEqual(ts, sorted(ts), "stops must be sorted")
        self.assertEqual(ts[0], 0.0)
        # 硬跳变：同 t 的一对 stop，左红右蓝
        jump_pairs = [(a, b) for a, b in zip(stops[:-1], stops[1:]) if float(a["t"]) == float(b["t"])]
        self.assertEqual(len(jump_pairs), 1)
        left, right = jump_pairs[0]
        self.assertEqual(left["rgb"], [204, 26, 26])
        self.assertEqual(right["rgb"], [26, 26, 204])
        # 求值：跳变前是红、跳变后是蓝；不得出现被平均的紫色
        t = np.array([0.25, 0.49, 0.51, 0.75])
        got_rgb, got_a = attribute.eval_color(curve, t)
        np.testing.assert_allclose(got_rgb[0], [204 / 255.0, 26 / 255.0, 26 / 255.0], atol=1.5 / 255.0)
        np.testing.assert_allclose(got_rgb[1], [204 / 255.0, 26 / 255.0, 26 / 255.0], atol=1.5 / 255.0)
        np.testing.assert_allclose(got_rgb[2:], np.tile([26 / 255.0, 26 / 255.0, 204 / 255.0], (2, 1)),
                                   atol=1.5 / 255.0)
        self.assertLess(float(np.min(got_rgb[:, 0])), 0.2)
        # 每条 stop 的 rgb 必须是 0..255 int，a 在 0..1
        for st in stops:
            self.assertEqual(len(st["rgb"]), 3)
            for ch in st["rgb"]:
                self.assertIsInstance(ch, int)
                self.assertGreaterEqual(ch, 0)
                self.assertLessEqual(ch, 255)
            self.assertGreaterEqual(float(st["a"]), 0.0)
            self.assertLessEqual(float(st["a"]), 1.0)

    def test_gradient_stops(self):
        s = make_s(300)
        c0 = np.array([230, 25, 50]) / 255.0
        c1 = np.array([25, 120, 230]) / 255.0
        rgb = np.outer(np.ones(len(s)), c0) + np.outer(s, c1 - c0)
        curve = brush.build_color_curve(s, rgb, np.ones(len(s)), 0.02)
        self.assertEqual(curve["interp"], "linear")
        self.assertLessEqual(len(curve["stops"]), 5)
        t = np.linspace(0.0, 1.0, 32)
        got, _ = attribute.eval_color(curve, t)
        ref = np.outer(np.ones(len(t)), c0) + np.outer(t, c1 - c0)
        self.assertLess(float(np.max(np.abs(got - ref))) * 255.0, 4.5)

    def test_alpha_ramp_in_color_curve(self):
        s = make_s(200)
        rgb = np.tile(np.array([0.2, 0.4, 0.6]), (len(s), 1))
        a = np.linspace(1.0, 0.2, len(s))
        curve = brush.build_color_curve(s, rgb, a, 0.02)
        _, got_a = attribute.eval_color(curve, np.array([0.0, 0.5, 1.0]))
        np.testing.assert_allclose(got_a, [1.0, 0.6, 0.2], atol=0.02)
        # rgb 不变
        got_rgb, _ = attribute.eval_color(curve, np.array([0.5]))
        np.testing.assert_allclose(got_rgb[0], [0.2, 0.4, 0.6], atol=1.5 / 255.0)

    def test_bad_shapes(self):
        with self.assertRaises(FBSError):
            brush.build_color_curve(make_s(10), np.zeros((9, 3)), np.ones(10), 0.02)
        with self.assertRaises(FBSError):
            brush.build_color_curve(make_s(10), np.zeros((10, 2)), np.ones(10), 0.02)


class TestShapeCurve(unittest.TestCase):
    def test_shape_step_curve(self):
        s = make_s(200)
        shapes = ["round"] * 100 + ["marker"] * 100
        curve = brush.build_shape_curve(s, shapes)
        self.assertEqual(curve["interp"], "step")
        self.assertAlmostEqual(float(curve["keys"][0][0]), 0.0)
        self.assertEqual(curve["keys"][0][1], "round")
        self.assertEqual(curve["keys"][1][1], "marker")
        got = attribute.eval_shape(curve, np.array([0.1, 0.499, 0.51, 0.9]))
        self.assertEqual(got, ["round", "round", "marker", "marker"])

    def test_constant_and_none(self):
        # 单值 / 全同列表 → 单 key；None → 默认 round
        for shapes, expected in (("soft", "soft"), (["soft"] * 50, "soft"), (None, "round")):
            curve = brush.build_shape_curve(make_s(50), shapes)
            self.assertEqual(curve["interp"], "step")
            self.assertEqual(len(curve["keys"]), 1)
            self.assertEqual(curve["keys"][0][1], expected)

    def test_three_shapes_along_t(self):
        s = make_s(300)
        shapes = ["round"] * 100 + ["soft"] * 100 + ["marker"] * 100
        curve = brush.build_shape_curve(s, shapes)
        got = attribute.eval_shape(curve, np.array([0.1, 0.5, 0.9]))
        self.assertEqual(got, ["round", "soft", "marker"])

    def test_invalid(self):
        with self.assertRaises(FBSError):
            brush.build_shape_curve(make_s(10), ["round"] * 9 + ["bogus"])
        with self.assertRaises(FBSError):
            brush.build_shape_curve(make_s(10), ["round"] * 5)


class TestSelectShape(unittest.TestCase):
    def _el(self, widths, alphas, fill_role="medial_branch"):
        return {"widths": np.asarray(widths, dtype=np.float64),
                "alphas": np.asarray(alphas, dtype=np.float64),
                "fill_role": fill_role}

    def test_forced_mode(self):
        el = self._el(np.full(100, 0.02), np.ones(100))
        for mode in ("round", "soft", "marker"):
            self.assertEqual(brush.select_shape(el, FBSConfig(shape_mode=mode)), mode)

    def test_auto_uniform_hard_is_marker(self):
        el = self._el(np.full(100, 0.02), np.ones(100))
        self.assertEqual(brush.select_shape(el, FBSConfig()), "marker")

    def test_auto_varying_width_is_round(self):
        el = self._el(np.linspace(0.002, 0.04, 100), np.ones(100))
        self.assertEqual(brush.select_shape(el, FBSConfig()), "round")

    def test_auto_translucent_is_soft(self):
        el = self._el(np.full(100, 0.02), np.full(100, 0.5))
        self.assertEqual(brush.select_shape(el, FBSConfig()), "soft")

    def test_region_fill_is_round(self):
        el = self._el(np.zeros(100), np.ones(100), fill_role="region_fill")
        self.assertEqual(brush.select_shape(el, FBSConfig()), "round")

    def test_zero_width_is_round(self):
        el = self._el(np.zeros(100), np.ones(100))
        self.assertEqual(brush.select_shape(el, FBSConfig()), "round")


class TestBuildBrush(unittest.TestCase):
    def _element(self, m=200, uniform_width=True):
        s = make_s(m)
        widths = np.full(m, 0.02) if uniform_width else 0.02 + 0.01 * np.sin(2.0 * math.pi * s)
        colors = np.tile(np.array([0.5, 0.4, 0.3]), (m, 1))
        alphas = np.ones(m)
        return {
            "points": np.stack([0.2 + 0.6 * s, 0.5 * np.ones(m)], axis=1),
            "s": s, "closed": False, "fill_role": "medial_branch",
            "widths": widths, "colors": colors, "alphas": alphas, "shapes": None,
            "fill_poly": None, "region_id": -1, "layer": 0, "meta": {},
        }

    def test_style_contract(self):
        style, br = brush.build_brush(self._element(), CONTRACT, FBSConfig())
        self.assertEqual(set(style.keys()), {"color", "alpha", "shape", "jitter"})
        self.assertEqual(set(br.keys()), {"style", "width"})
        self.assertEqual(br["style"], 0)
        self.assertIsInstance(br["style"], int)
        self.assertAlmostEqual(style["jitter"], 0.0)
        self.assertEqual(style["alpha"]["interp"], "constant")
        self.assertTrue(constant_curve(style["alpha"]))
        self.assertEqual(style["color"]["interp"], "constant")
        self.assertEqual(style["shape"]["interp"], "step")

    def test_constant_compression(self):
        el = self._element(uniform_width=True)
        style, br = brush.build_brush(el, CONTRACT, FBSConfig())
        self.assertTrue(constant_curve(br["width"]), br["width"])
        self.assertAlmostEqual(float(br["width"]["keys"][0][1]), 0.02, places=9)
        self.assertTrue(constant_curve(style["alpha"]))
        self.assertEqual(len(style["color"]["stops"]), 1)

    def test_width_faithful_and_world_units(self):
        el = self._element(uniform_width=False)
        style, br = brush.build_brush(el, CONTRACT, FBSConfig())
        got = eval_num(br["width"], el["s"])
        tol = 0.02 * float(np.max(el["widths"])) * 1.5
        self.assertLess(float(np.max(np.abs(got - el["widths"]))), tol)
        # 世界单位：与输入同量级（不是像素、不是半径）
        self.assertLess(abs(float(np.median(got)) - float(np.median(el["widths"]))), 1e-3)

    def test_curves_independent(self):
        el = self._element(uniform_width=False)
        el["alphas"] = np.full(len(el["s"]), 0.8)
        style, br = brush.build_brush(el, CONTRACT, FBSConfig())
        self.assertFalse(constant_curve(br["width"]))
        self.assertTrue(constant_curve(style["alpha"]))
        self.assertAlmostEqual(float(style["alpha"]["keys"][0][1]), 0.8, places=6)
        # 反过来：等宽 + alpha 渐变
        el2 = self._element(uniform_width=True)
        el2["alphas"] = np.linspace(1.0, 0.4, len(el2["s"]))
        style2, br2 = brush.build_brush(el2, CONTRACT, FBSConfig())
        self.assertTrue(constant_curve(br2["width"]))
        self.assertFalse(constant_curve(style2["alpha"]))
        a_mid = float(eval_num(style2["alpha"], np.array([0.5]))[0])
        self.assertAlmostEqual(a_mid, 0.7, places=2)

    def test_shapes_explicit_used(self):
        el = self._element()
        el["shapes"] = ["round"] * 100 + ["marker"] * 100
        style, _ = brush.build_brush(el, CONTRACT, FBSConfig())
        got = attribute.eval_shape(style["shape"], np.array([0.2, 0.8]))
        self.assertEqual(got, ["round", "marker"])

    def test_shapes_none_uses_select_shape(self):
        el = self._element(uniform_width=True)
        style, _ = brush.build_brush(el, CONTRACT, FBSConfig())
        self.assertEqual(style["shape"]["keys"][0][1], "marker")
        el2 = self._element(uniform_width=False)
        style2, _ = brush.build_brush(el2, CONTRACT, FBSConfig())
        self.assertEqual(style2["shape"]["keys"][0][1], "round")

    def test_deterministic_and_dedupable(self):
        el = self._element(uniform_width=False)
        style_a, br_a = brush.build_brush(el, CONTRACT, FBSConfig())
        style_b, br_b = brush.build_brush(el, CONTRACT, FBSConfig())
        self.assertEqual(sha256_json(style_a), sha256_json(style_b))
        self.assertEqual(sha256_json(br_a["width"]), sha256_json(br_b["width"]))
        # 不同属性 → 不同样式（共享表要去重的只是真正相同的样式）
        el2 = self._element(uniform_width=False)
        el2["alphas"] = np.linspace(1.0, 0.5, len(el2["s"]))
        style_c, _ = brush.build_brush(el2, CONTRACT, FBSConfig())
        self.assertNotEqual(sha256_json(style_a), sha256_json(style_c))

    def test_jitter_from_config(self):
        style, _ = brush.build_brush(self._element(), CONTRACT, FBSConfig(brush_jitter=0.25))
        self.assertAlmostEqual(style["jitter"], 0.25)

    def test_length_mismatch_recovered_with_warning(self):
        el = self._element()
        el["widths"] = np.full(50, 0.02)  # 长度不匹配
        warn = WarningLog()
        style, br = brush.build_brush(el, CONTRACT, FBSConfig(), warn)
        self.assertIn("W_BRUSH_LENGTH_MISMATCH", [w["code"] for w in warn.to_list()])
        self.assertEqual(len(eval_num(br["width"], el["s"])), len(el["s"]))

    def test_empty_s_raises(self):
        el = self._element()
        el["s"] = np.zeros(0)
        with self.assertRaises(FBSError):
            brush.build_brush(el, CONTRACT, FBSConfig())


class TestPerformance(unittest.TestCase):
    def test_brush_throughput(self):
        # 1000 曲线 @R1024 的 Stage3 预算 30s；这里 300 条宽松限制 30s（实测约 1s）
        rng = np.random.default_rng(0)
        cfg = FBSConfig()
        elems = []
        for _ in range(300):
            m = int(rng.integers(20, 400))
            s = np.linspace(0.0, 1.0, m)
            power = int(rng.integers(0, 3))
            widths = (0.01 + 0.03 * rng.random()) * (1.0 - s) ** power
            widths = np.maximum(widths + rng.uniform(-2e-4, 2e-4, m), 0.0)
            colors = np.clip(np.tile(rng.uniform(0.0, 1.0, 3), (m, 1))
                             + rng.uniform(-0.01, 0.01, (m, 3)), 0.0, 1.0)
            elems.append({"s": s, "widths": widths, "colors": colors,
                          "alphas": np.ones(m), "shapes": None, "fill_role": "medial_branch"})
        t0 = time.perf_counter()
        for el in elems:
            brush.build_brush(el, CONTRACT, cfg)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 30.0, f"300 curves took {elapsed:.1f}s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
