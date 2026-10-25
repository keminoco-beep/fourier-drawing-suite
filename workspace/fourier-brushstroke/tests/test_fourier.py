"""Stage2 傅里叶参数化单元测试（stdlib unittest，无网络）。

覆盖：开/闭曲线拟合、drawable 参数映射、契约误差、Nyquist 钳制、
N 模式、误差随 N 非增、退化输入、eval 一致性、性能回归。
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

from fbs_core import fourier  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.util import FBSError, WarningLog, polyline_arclength  # noqa: E402

CONTRACT = {"target_resolution": 1024, "epsilon_px": 0.5}
THR = CONTRACT["epsilon_px"] / CONTRACT["target_resolution"]


# ------------------------------------------------------------------ 合成曲线
def make_star(n: int = 5, r_out: float = 0.40, r_in: float = 0.16,
              cx: float = 0.5, cy: float = 0.5) -> np.ndarray:
    """正 n 角星（闭合轮廓，首尾不重复）。"""
    pts = []
    for i in range(2 * n):
        ang = 2.0 * math.pi * i / (2 * n) - math.pi / 2.0
        r = r_out if i % 2 == 0 else r_in
        pts.append([cx + r * math.cos(ang), cy + r * math.sin(ang)])
    return np.asarray(pts, dtype=np.float64)


def make_circle(m: int = 400, r: float = 0.30, cx: float = 0.5, cy: float = 0.5) -> np.ndarray:
    """闭合圆（密集折线近似）。"""
    ang = np.linspace(0.0, 2.0 * math.pi, m, endpoint=False)
    return np.stack([cx + r * np.cos(ang), cy + r * np.sin(ang)], axis=1)


def make_square(side: float = 0.5) -> np.ndarray:
    """闭合正方形（四个硬角）。"""
    h = side / 2.0
    return np.asarray([[0.5 - h, 0.5 - h], [0.5 + h, 0.5 - h],
                       [0.5 + h, 0.5 + h], [0.5 - h, 0.5 + h]], dtype=np.float64)


def make_line(m: int = 40) -> np.ndarray:
    """开曲线：直线段（闭环退化共线的极端用例）。"""
    return np.stack([np.linspace(0.10, 0.80, m), np.linspace(0.20, 0.70, m)], axis=1)


def make_sine(m: int = 300) -> np.ndarray:
    """开曲线：带高频小波的平滑 S 形笔画。"""
    x = np.linspace(0.05, 0.90, m)
    u = (x - 0.05) / 0.85
    y = 0.5 + 0.18 * np.sin(2 * math.pi * 1.5 * u) + 0.02 * np.sin(2 * math.pi * 5.0 * u)
    return np.stack([x, y], axis=1)


def make_c_shape(m: int = 300) -> np.ndarray:
    """开曲线：C 形（弦长相对弧长较小）。"""
    ang = np.linspace(-0.35 * math.pi, 0.35 * math.pi, m)
    r = 0.30
    return np.stack([0.5 + r * np.cos(ang), 0.5 + r * np.sin(ang)], axis=1)


def fit(pts, closed, **cfg_kw):
    """便捷拟合：返回 (fit dict, WarningLog)。"""
    warn = WarningLog()
    cfg = FBSConfig(**cfg_kw)
    return fourier.fit_curve(pts, closed, CONTRACT, cfg, warn, name="test"), warn


def dense_drawable(rec_fit: dict, n: int):
    """drawable 参数上的密集重建采样。"""
    t = np.linspace(0.0, 1.0, n)
    return fourier.eval_curve(rec_fit["coef"], rec_fit["closure"], t)


def max_step_ratio(points: np.ndarray, arc_length: float) -> float:
    """相邻样本最大间距 / 均匀间距 —— 检测"多余斜线/尖点"造成的跳变。"""
    steps = np.linalg.norm(np.diff(points, axis=0), axis=1)
    ref = arc_length / max(len(points) - 1, 1)
    return float(steps.max() / max(ref, 1e-12))


# ------------------------------------------------------------------ 用例
class TestClosedCurves(unittest.TestCase):
    def test_circle_needs_few_harmonics(self):
        pts = make_circle()
        f, _ = fit(pts, True)
        self.assertTrue(f["closed"])
        self.assertEqual(f["closure"], {"kind": "none"})
        self.assertLessEqual(f["N"], 5, "circle should be exact with k=+-1")
        self.assertLessEqual(f["max_err"], THR)
        self.assertAlmostEqual(f["arc_length"], polyline_arclength(pts, closed=True), places=12)
        self.assertEqual(f["coef"].shape, (2 * f["N"] + 1, 3))

    def test_star_tips(self):
        pts = make_star(5)
        f, warn = fit(pts, True)
        # 5 尖角星：需要较多谐波，但必须在默认钳制内满足亚像素契约
        self.assertGreater(f["N"], 5)
        self.assertLessEqual(f["N"], FBSConfig().N_max)
        self.assertLessEqual(f["max_err"], THR)
        self.assertFalse([w for w in warn.to_list() if w["code"] == "W_N_CLIPPED"])
        # coef 规则：|k| 升序、max|k|==N、k 唯一
        ks = f["coef"][:, 0].astype(int)
        self.assertEqual(int(np.max(np.abs(ks))), f["N"])
        self.assertTrue(np.all(np.diff(np.abs(ks)) >= 0))
        self.assertEqual(len(set(ks.tolist())), len(ks))

    def test_square_hard_corners(self):
        pts = make_square()
        f, _ = fit(pts, True)
        self.assertLessEqual(f["max_err"], THR)
        self.assertGreater(f["N"], 10)
        # 闭合曲线 eval 周期
        q0 = fourier.eval_curve(f["coef"], f["closure"], np.array([0.0]))
        q1 = fourier.eval_curve(f["coef"], f["closure"], np.array([1.0]))
        self.assertLess(float(np.linalg.norm(q0[0] - q1[0])), 1e-9)

    def test_region_contour_roundtrip(self):
        # 平滑闭合轮廓（圆 + 微小形变）：重建误差必须满足契约
        ang = np.linspace(0.0, 2.0 * math.pi, 500, endpoint=False)
        r = 0.28 + 0.02 * np.sin(3.0 * ang) + 0.01 * np.cos(5.0 * ang)
        pts = np.stack([0.5 + r * np.cos(ang), 0.5 + r * np.sin(ang)], axis=1)
        f, _ = fit(pts, True)
        self.assertLessEqual(f["max_err"], THR)
        self.assertLessEqual(fourier.reconstruct_max_error(f["coef"], f["closure"], pts), THR * 1.2)


class TestOpenCurves(unittest.TestCase):
    def test_line_drawable_mapping(self):
        pts = make_line()
        f, _ = fit(pts, False)
        self.assertFalse(f["closed"])
        self.assertEqual(f["closure"]["kind"], "hermite_c1")
        t_end = float(f["closure"]["t_end"])
        self.assertGreater(t_end, 0.0)
        self.assertLess(t_end, 1.0)
        self.assertAlmostEqual(f["arc_length"], polyline_arclength(pts, closed=False), places=12)
        # 端点位置
        q0 = fourier.eval_curve(f["coef"], f["closure"], np.array([0.0]))[0]
        q1 = fourier.eval_curve(f["coef"], f["closure"], np.array([1.0]))[0]
        self.assertLess(float(np.linalg.norm(q0 - pts[0])), THR)
        self.assertLess(float(np.linalg.norm(q1 - pts[-1])), THR)
        self.assertLessEqual(f["max_err"], THR)

    def test_no_extra_slash_or_spike(self):
        # 开曲线：重建点必须在原曲线附近，且相邻采样间距不得出现"斜线横穿"级跳变
        for pts in (make_line(), make_sine(), make_c_shape()):
            f, _ = fit(pts, False)
            rec = dense_drawable(f, 3000)
            self.assertLessEqual(max_step_ratio(rec, f["arc_length"]), 3.0)
            ref = fourier._sample_polyline(pts, np.linspace(0.0, 1.0, 3000), closed=False)
            dev = np.linalg.norm(rec - ref, axis=1)
            self.assertLessEqual(float(dev.max()), THR * 1.2)
            # 重建点不得越出原曲线包围盒太多（多余闭环段不参与绘制）
            pad = max(0.02, 4.0 * THR)
            self.assertGreaterEqual(rec[:, 0].min(), pts[:, 0].min() - pad)
            self.assertGreaterEqual(rec[:, 1].min(), pts[:, 1].min() - pad)
            self.assertLessEqual(rec[:, 0].max(), pts[:, 0].max() + pad)
            self.assertLessEqual(rec[:, 1].max(), pts[:, 1].max() + pad)

    def test_c_shape(self):
        pts = make_c_shape()
        f, _ = fit(pts, False)
        self.assertLessEqual(f["max_err"], THR)
        self.assertLess(f["N"], 200)

    def test_two_point_line_supported(self):
        pts = np.asarray([[0.1, 0.2], [0.4, 0.5]], dtype=np.float64)
        f, _ = fit(pts, False)
        self.assertLessEqual(f["max_err"], THR)
        self.assertEqual(f["coef"].shape, (2 * f["N"] + 1, 3))

    def test_looser_contract_needs_no_more_harmonics(self):
        pts = make_sine()
        cfg = FBSConfig()
        w1, w2 = WarningLog(), WarningLog()
        tight = fourier.fit_curve(pts, False, {"target_resolution": 1024, "epsilon_px": 0.1}, cfg, w1)
        loose = fourier.fit_curve(pts, False, {"target_resolution": 1024, "epsilon_px": 2.0}, cfg, w2)
        self.assertLessEqual(loose["N"], tight["N"])


class TestResampledAndEval(unittest.TestCase):
    def test_resampled_uniform(self):
        pts = make_sine()
        f, _ = fit(pts, False)
        res = f["resampled"]
        m = FBSConfig().resample_points
        self.assertEqual(res.shape, (m, 2))
        seg = np.linalg.norm(np.diff(res, axis=0), axis=1)
        # 弦长 ≤ 弧长间距（跨折线顶点时弦略短），均匀性用 chord/arc 的相对离散衡量
        self.assertLess(float(seg.std() / seg.mean()), 1e-3, "resampled must be near-uniform arc-length")
        self.assertLess(float(seg.max()), float(seg.mean()) * 1.01)

    def test_eval_consistency(self):
        pts = make_sine()
        f, _ = fit(pts, False)
        t = np.linspace(0.0, 1.0, 97)
        t_end = f["closure"]["t_end"]
        direct = fourier.eval_coef(f["coef"], t * t_end)
        via_curve = fourier.eval_curve(f["coef"], f["closure"], t)
        self.assertLess(float(np.max(np.abs(direct - via_curve))), 1e-12)
        # 闭合曲线：t_closed = t_draw
        fc, _ = fit(make_circle(), True)
        self.assertLess(
            float(np.max(np.abs(fourier.eval_coef(fc["coef"], t) -
                                fourier.eval_curve(fc["coef"], fc["closure"], t)))),
            1e-12,
        )

    def test_reconstruct_max_error_matches_fit(self):
        pts = make_sine()
        f, _ = fit(pts, False)
        re = fourier.reconstruct_max_error(f["coef"], f["closure"], pts)
        # 与拟合内部指标的采样网格不同，允许小量差异，但量级必须一致
        self.assertLessEqual(re, THR * 1.3)
        self.assertGreaterEqual(re, f["max_err"] * 0.5)

    def test_eval_coef_bad_shape(self):
        with self.assertRaises(FBSError):
            fourier.eval_coef(np.zeros((3, 2)), np.array([0.0]))


class TestAdaptiveN(unittest.TestCase):
    def test_error_non_increasing_with_n(self):
        pts = make_sine()
        cfg_fixed = {"N_mode": "fixed"}
        prev = None
        for n in (1, 2, 4, 8, 16, 32, 64, 128, 256):
            f, _ = fit(pts, False, N_max=n, **cfg_fixed)
            self.assertEqual(f["N"], n)
            err = fourier.reconstruct_max_error(f["coef"], f["closure"], pts)
            if prev is not None:
                self.assertLessEqual(err, prev + 1e-9, f"error must not increase at N={n}")
            prev = err

    def test_minimal_n_plus_one_exceeds(self):
        # "最小 N"验证：N-1 的误差应超过契约（或 N==0）
        pts = make_sine()
        f, _ = fit(pts, False)
        n = f["N"]
        self.assertLessEqual(f["max_err"], THR)
        if n > 0:
            g, _ = fit(pts, False, N_mode="fixed", N_max=n - 1)
            err_prev = fourier.reconstruct_max_error(g["coef"], g["closure"], pts)
            self.assertGreater(err_prev, THR * 0.9)

    def test_energy_mode_usable(self):
        f, _ = fit(make_sine(), False, N_mode="energy")
        self.assertGreaterEqual(f["N"], 0)
        self.assertEqual(f["coef"].shape, (2 * f["N"] + 1, 3))
        self.assertTrue(np.isfinite(f["max_err"]))

    def test_fixed_mode_and_clip_warning(self):
        f, _ = fit(make_sine(), False, N_mode="fixed", N_max=7)
        self.assertEqual(f["N"], 7)
        # N_max 超过 Nyquist 钳制：钳制 + W_N_CLIPPED
        f2, warn2 = fit(make_line(), False, N_mode="fixed", N_max=100000, resample_points=256)
        self.assertEqual(f2["N"], 256 // 2 - 2)
        self.assertIn("W_N_CLIPPED", [w["code"] for w in warn2.to_list()])

    def test_nyquist_clamp_open(self):
        pts = make_star(5)  # 硬角；用极小 resample_points 触发钳制
        f, warn = fit(pts, True, resample_points=256)
        self.assertEqual(f["N"], 256 // 2 - 2)
        self.assertIn("W_N_CLIPPED", [w["code"] for w in warn.to_list()])
        q = fourier.eval_curve(f["coef"], f["closure"], np.linspace(0.0, 1.0, 200))
        self.assertTrue(np.all(np.isfinite(q)))
        # 重建不能跑到画面外（回绕 bug 的特征是数值崩坏）
        self.assertTrue(np.all(q > -1.0) and np.all(q < 2.0))

    def test_nyquist_clamp_does_not_wrap_coef(self):
        pts = make_star(5)
        f, _ = fit(pts, True, resample_points=256)
        ks = f["coef"][:, 0].astype(int)
        self.assertEqual(len(ks), len(set(ks.tolist())), "coef k must be unique")
        self.assertEqual(int(np.max(np.abs(ks))), f["N"])
        # 最大谐波必须 ≤ M/2-2（回绕 bug 会重复取同一批系数）
        self.assertLessEqual(int(np.max(np.abs(ks))), int(f["resampled"].shape[0]) // 2 - 2)


class TestDegenerate(unittest.TestCase):
    def test_empty(self):
        with self.assertRaises(FBSError):
            fit(np.zeros((0, 2)), False)

    def test_single_point(self):
        with self.assertRaises(FBSError):
            fit(np.asarray([[0.5, 0.5]]), False)

    def test_closed_too_few(self):
        with self.assertRaises(FBSError):
            fit(np.asarray([[0.1, 0.1], [0.2, 0.2]]), True)

    def test_all_equal(self):
        pts = np.tile(np.asarray([[0.3, 0.3]]), (5, 1))
        with self.assertRaises(FBSError):
            fit(pts, False)
        with self.assertRaises(FBSError):
            fit(pts, True)

    def test_nan_rejected(self):
        pts = make_line()
        pts[3, 1] = float("nan")
        with self.assertRaises(FBSError):
            fit(pts, False)


class TestPerformance(unittest.TestCase):
    def test_fit_throughput(self):
        # 300 条平滑开曲线（远多于常规单图量级的一部分）拟合必须明显快于预算
        rng = np.random.default_rng(0)
        curves = []
        for _ in range(300):
            m = int(rng.integers(8, 120))
            length = float(rng.uniform(0.03, 0.4))
            ang = float(rng.uniform(0.0, 2.0 * math.pi))
            s = np.linspace(0.0, length, m)
            x = 0.5 + s * np.cos(ang) + 0.01 * np.sin(2.0 * math.pi * s / length)
            y = 0.5 + s * np.sin(ang) + 0.01 * np.cos(2.0 * math.pi * s / length)
            curves.append(np.stack([x, y], axis=1))
        cfg = FBSConfig()
        t0 = time.perf_counter()
        for pts in curves:
            f = fourier.fit_curve(pts, False, CONTRACT, cfg, None)
            self.assertLessEqual(f["max_err"], THR)
        elapsed = time.perf_counter() - t0
        # 1000 曲线 @R1024 的 N 搜索预算为 60s（CONTRACT §12，含 Stage3）；
        # 这里 300 条宽松限制 30s，只拦性能回退（实测约 2~4s）
        self.assertLess(elapsed, 30.0, f"300 curves took {elapsed:.1f}s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
