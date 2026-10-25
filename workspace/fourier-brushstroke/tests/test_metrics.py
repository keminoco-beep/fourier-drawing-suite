"""metrics.py 单元测试（stdlib unittest，无 pytest）。

覆盖四类失败模式（历史上都真实发生过）：
1. 同分辨率闸门失效 → 上采样再比（E_METRIC_RESOLUTION）；
2. SSIM 实现与定义不一致 → 与独立暴力实现/解析值对齐；
3. 结构指标对色调错误盲 → 「灰画成黑」必须由 tone 维度抓到；
4. 厚度归因按面积而非 EDT 峰值 → 细发丝缺失必须落在"细"箱；
5. 指纹/契约闸门失效 → v1 / 不同代码代必须拒绝比较。

运行：cd fourier-brushstroke && python -m unittest discover -s tests -p "test_metrics.py" -v
"""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from fbs_core import io_format, metrics  # noqa: E402


# ------------------------------------------------------------------ 测试图构造
def blank(h: int = 64, w: int = 64, color=(255, 255, 255)) -> np.ndarray:
    return np.full((h, w, 3), np.asarray(color, dtype=np.uint8), dtype=np.uint8)


def fill_rect(img: np.ndarray, y0: int, y1: int, x0: int, x1: int, color) -> np.ndarray:
    out = img.copy()
    out[y0:y1, x0:x1] = np.asarray(color, dtype=np.uint8)
    return out


def hline(img: np.ndarray, y: int, x0: int, x1: int, width: int, color) -> np.ndarray:
    return fill_rect(img, y, y + width, x0, x1, color)


def make_doc(
    *,
    params: dict | None = None,
    resolution: int = 64,
    eps: float = 0.5,
    version: str = "2.0",
    canvas: tuple[float, float] = (1.0, 1.0),
    codegen: str | None = None,
) -> dict:
    """构造最小的合法 v2 文档骨架（用于契约/指纹测试）。"""
    doc = {
        "format": "fourier-brushstroke",
        "version": version,
        "mode": "per_stroke",
        "canvas": {
            "width": float(canvas[0]),
            "height": float(canvas[1]),
            "long_side_units": 1.0,
            "origin": "top-left",
            "y_axis": "down",
        },
        "render_contract": {"target_resolution": int(resolution), "epsilon_px": float(eps)},
        "decomposition": {"method": "test", "params": dict(params or {"k": 1})},
        "source": {"sha256": "0" * 64},
        "strokes": [],
        "regions": [],
        "stats": {},
        "warnings": [],
    }
    io_format.finalize_document(doc)
    if codegen is not None:
        doc["fingerprint"]["codegen"] = codegen
    return doc


# ================================================================== 基础指标
class TestBasicMetrics(unittest.TestCase):
    def test_psnr_known_value(self):
        # 两幅常量图相差 1 个 8-bit 灰度级：MSE = (1/255)^2
        a = np.zeros((16, 16, 3), np.uint8)
        b = np.ones((16, 16, 3), np.uint8)
        expected = 10.0 * np.log10(255.0 ** 2)
        self.assertAlmostEqual(metrics.psnr(a, b), expected, places=10)
        self.assertAlmostEqual(metrics.psnr(a, b), 48.1308036086791, places=9)

    def test_psnr_identical_is_inf(self):
        a = blank()
        self.assertTrue(np.isinf(metrics.psnr(a, a)))

    def test_rmse_mae_units_8bit(self):
        a = np.zeros((8, 8, 3), np.uint8)
        b = np.full((8, 8, 3), 3, np.uint8)
        self.assertAlmostEqual(metrics.mae(a, b), 3.0, places=12)
        self.assertAlmostEqual(metrics.rmse(a, b), 3.0, places=12)

    def test_float01_input_equivalent_to_uint8(self):
        rng = np.random.default_rng(0)
        a = rng.integers(0, 256, (16, 16, 3), dtype=np.uint8)
        b = rng.integers(0, 256, (16, 16, 3), dtype=np.uint8)
        p_u8 = metrics.psnr(a, b)
        p_f1 = metrics.psnr(a / 255.0, b / 255.0)
        self.assertAlmostEqual(p_u8, p_f1, places=9)
        self.assertAlmostEqual(metrics.rmse(a / 255.0, b / 255.0), metrics.rmse(a, b), places=9)

    def test_resolution_mismatch_raises(self):
        a = blank(32, 32)
        b = blank(64, 64)
        for fn in (metrics.psnr, metrics.rmse, metrics.mae, metrics.ssim):
            with self.assertRaises(metrics.FBSError) as ctx:
                fn(a, b)
            self.assertEqual(ctx.exception.code, "E_METRIC_RESOLUTION")


# ================================================================== SSIM
def _ssim_reference(a: np.ndarray, b: np.ndarray, win: int = 11, sigma: float = 1.5) -> float:
    """独立的暴力实现（显式 2D 卷积、reflect 边界、裁剪 interior），用于对齐实现。"""
    k = metrics.gaussian_window(win, sigma)
    r = win // 2

    def conv(x: np.ndarray) -> np.ndarray:
        h, w = x.shape
        p = np.pad(x, ((r, r), (r, r)), mode="reflect")
        out = np.zeros_like(x)
        for i in range(win):
            for j in range(win):
                out += k[i] * k[j] * p[i : i + h, j : j + w]
        return out

    mu_a, mu_b = conv(a), conv(b)
    s_aa = conv(a * a) - mu_a * mu_a
    s_bb = conv(b * b) - mu_b * mu_b
    s_ab = conv(a * b) - mu_a * mu_b
    c1, c2 = 0.01 ** 2, 0.03 ** 2
    m = ((2 * mu_a * mu_b + c1) * (2 * s_ab + c2)) / (
        (mu_a * mu_a + mu_b * mu_b + c1) * (s_aa + s_bb + c2)
    )
    m = m[r : m.shape[0] - r, r : m.shape[1] - r]
    return float(np.mean(m))


class TestSSIM(unittest.TestCase):
    def test_identical_is_one(self):
        rng = np.random.default_rng(42)
        a = rng.random((40, 40)) * 0.7 + 0.1
        self.assertAlmostEqual(metrics.ssim(a, a), 1.0, places=12)
        rgb = np.repeat(a[:, :, None], 3, axis=2)
        self.assertAlmostEqual(metrics.ssim(rgb, rgb), 1.0, places=12)

    def test_symmetry(self):
        rng = np.random.default_rng(7)
        a = rng.random((32, 32))
        b = rng.random((32, 32))
        self.assertAlmostEqual(metrics.ssim(a, b), metrics.ssim(b, a), places=12)

    def test_matches_independent_bruteforce(self):
        rng = np.random.default_rng(2026)
        a = rng.random((37, 41)) * 0.9 + 0.05
        b = np.clip(a + rng.normal(0, 0.12, a.shape), 0, 1)
        got = metrics.ssim(a, b)
        ref = _ssim_reference(a, b)
        self.assertAlmostEqual(got, ref, places=9)

    def test_constant_image_closed_form(self):
        # 常量图方差=0、协方差=0 → SSIM = (2·μa·μb + C1) / (μa² + μb² + C1)
        c1 = 0.01 ** 2
        for va, vb in ((0.2, 0.6), (0.0, 1.0), (0.35, 0.35)):
            a = np.full((16, 16), va)
            b = np.full((16, 16), vb)
            expected = (2 * va * vb + c1) / (va * va + vb * vb + c1)
            self.assertAlmostEqual(metrics.ssim(a, b), expected, places=10)

    def test_less_noise_more_similar(self):
        rng = np.random.default_rng(3)
        base = rng.random((48, 48))
        small = np.clip(base + rng.normal(0, 0.02, base.shape), 0, 1)
        large = np.clip(base + rng.normal(0, 0.20, base.shape), 0, 1)
        self.assertGreater(metrics.ssim(base, small), metrics.ssim(base, large))

    def test_multichannel_is_channel_mean(self):
        rng = np.random.default_rng(11)
        a = rng.random((24, 24, 3))
        b = rng.random((24, 24, 3))
        got = metrics.ssim(a, b)
        manual = float(np.mean([metrics.ssim(a[:, :, c], b[:, :, c]) for c in range(3)]))
        self.assertAlmostEqual(got, manual, places=12)


# ================================================================== 结构 / 厚度
class TestStructureAndThickness(unittest.TestCase):
    def test_perfect_match(self):
        img = hline(blank(), 20, 5, 60, 5, (0, 0, 0))
        got = metrics.structure(img, img)
        self.assertEqual(got["missing_pct"], 0.0)
        self.assertEqual(got["extra_pct"], 0.0)
        self.assertAlmostEqual(got["recall"], 1.0, places=12)
        self.assertAlmostEqual(got["precision"], 1.0, places=12)
        self.assertAlmostEqual(got["iou"], 1.0, places=12)
        self.assertEqual(got["missing_px"], 0)

    def test_ink_threshold_default_is_half(self):
        # 浅灰 (200) 与白底的距离 ≈ 0.374：0.5 阈值下不算墨（低于 0.5 会把淡笔画算成已覆盖）
        img = hline(blank(), 10, 0, 64, 4, (200, 200, 200))
        self.assertFalse(metrics.ink_mask(img).any())
        self.assertTrue(metrics.ink_mask(img, tol=0.35).any())

    def test_missing_thin_ink_lands_in_thin_bin(self):
        """按面积分箱会把 1px 细线归到"大块"；EDT 峰值分箱必须落在"细"箱。"""
        ref = hline(blank(), 10, 5, 60, 1, (0, 0, 0))  # 细线：EDT 峰值 1.0
        ref = hline(ref, 30, 5, 60, 9, (0, 0, 0))  # 粗条：EDT 峰值 5.0
        rec = hline(blank(), 30, 5, 60, 9, (0, 0, 0))  # 只画了粗条
        st = metrics.structure(ref, rec)
        self.assertGreater(st["missing_pct"], 0.2)
        bins = st["missing_thickness"]
        self.assertEqual(len(bins), 5)
        self.assertGreaterEqual(bins[0]["count"], 1, "细线缺失必须落在 <1.0px（极细）箱")
        self.assertEqual(bins[0]["count"], 1)
        self.assertEqual(bins[3]["count"], 0, "粗条没丢，不应出现在 2.5-5px 箱")
        # 面积上细线 55px vs 粗条 495px：若按面积分箱会被归进大块
        self.assertEqual(bins[0]["pixels"], 55)  # 面积只有 55px：按面积分箱根本看不见

    def test_bg_override(self):
        # 参考图为深底浅线：默认四角中位色能估到深底
        img = np.full((32, 32, 3), 20, np.uint8)
        img = hline(img, 15, 2, 30, 2, (240, 240, 240))
        st = metrics.structure(img, img, bg=(20, 20, 20))
        self.assertGreater(st["ref_px"], 0)
        self.assertEqual(st["missing_pct"], 0.0)

    def test_empty_reference(self):
        st = metrics.structure(blank(), blank())
        self.assertEqual(st["ref_px"], 0)
        self.assertEqual(st["recall"], 1.0)
        self.assertEqual(st["iou"], 1.0)


# ================================================================== 色调
class TestTone(unittest.TestCase):
    def test_gray_drawn_as_black_is_caught_by_tone_not_structure(self):
        """结构指标只看"有没有墨"：灰色区域画成黑色，IoU 仍然完美；色调必须报警。"""
        gray = (148, 148, 156)
        ref = blank()
        ref = fill_rect(ref, 8, 40, 8, 40, gray)  # 灰块
        ref = hline(ref, 50, 5, 60, 3, (0, 0, 0))  # 黑线（让调色板同时含灰与黑）
        rec = fill_rect(blank(), 8, 40, 8, 40, (0, 0, 0))  # 灰块画成黑
        rec = hline(rec, 50, 5, 60, 3, (0, 0, 0))
        st = metrics.structure(ref, rec)
        token = metrics.compare(
            rec, ref, bg=(1.0, 1.0, 1.0), thickness_bins=None
        )
        self.assertAlmostEqual(st["iou"], 1.0, places=12)  # 结构完全盲
        self.assertEqual(st["missing_pct"], 0.0)
        tone = token["tone"]
        self.assertGreater(tone["mean_delta_e"], 30.0, "灰画成黑的平均色差必须显著")
        self.assertGreater(tone["wrong_ink_pct"], 0.0, "色调混淆矩阵必须出现非对角元")
        self.assertEqual(len(tone["confusion"]), len(tone["refs"]))
        self.assertEqual(len(tone["names"]), len(tone["refs"]))

    def test_identical_tone_is_zero_error(self):
        ref = fill_rect(blank(), 8, 30, 8, 30, (200, 60, 60))
        got = metrics.compare(ref, ref, bg=(1.0, 1.0, 1.0))
        self.assertAlmostEqual(got["tone"]["mean_delta_e"], 0.0, places=9)
        self.assertAlmostEqual(got["tone"]["wrong_ink_pct"], 0.0, places=9)

    def test_delta_e_known_value(self):
        # 纯黑 vs 纯白：Lab 中 L 相差 100 → ΔE76 = 100
        de = metrics.delta_e76(np.zeros((1, 1, 3)), np.ones((1, 1, 3)))
        self.assertAlmostEqual(float(de[0, 0]), 100.0, delta=0.01)  # sRGB→Lab 矩阵精度 ~1e-5


# ================================================================== 线宽 / 体积
class TestLineWidth(unittest.TestCase):
    def test_width_ratio_from_edt_peaks(self):
        ref = hline(blank(), 10, 5, 60, 3, (0, 0, 0))  # 3px 宽 → EDT 峰值 2 → 全宽估计 4
        rec = hline(blank(), 40, 5, 60, 7, (0, 0, 0))  # 7px 宽 → EDT 峰值 4 → 全宽估计 8
        got = metrics.linewidth(ref, rec)
        self.assertAlmostEqual(got["ref_median_px"], 4.0, places=6)
        self.assertAlmostEqual(got["rec_median_px"], 8.0, places=6)
        self.assertAlmostEqual(got["ratio"], 2.0, places=6)
        self.assertEqual(got["ref_components"], 1)
        self.assertGreater(got["volume"]["area_ratio"], 2.0)
        self.assertGreater(got["volume"]["mass_ratio"], 2.0)

    def test_identical_ratio_one(self):
        img = hline(blank(), 20, 5, 60, 5, (0, 0, 0))
        got = metrics.linewidth(img, img)
        self.assertEqual(got["ratio"], 1.0)
        self.assertEqual(got["ratio_abs_error_pct"], 0.0)


# ================================================================== 契约 / 指纹
class TestContractAndFingerprint(unittest.TestCase):
    def test_minimal_doc_passes_contract(self):
        ref = hline(blank(), 20, 5, 60, 5, (0, 0, 0))
        doc = make_doc(resolution=64)
        got = metrics.compare(ref, ref, doc=doc, bg=(1.0, 1.0, 1.0))
        contract = got["contract"]
        self.assertEqual(contract["errors"], [])
        self.assertEqual(contract["codegen"], "fbs-v2")
        self.assertEqual(contract["target_resolution"], 64)
        self.assertTrue(contract["resolution_matches_target"])
        self.assertEqual(contract["fingerprint"]["codegen"], "fbs-v2")
        self.assertTrue(contract["fingerprint"]["params_sha256"])
        self.assertEqual(metrics.fingerprint(doc)["codegen"], "fbs-v2")

    def test_v1_doc_rejected_in_contract(self):
        ref = blank()
        doc = make_doc(version="1.0")
        got = metrics.compare(ref, ref, doc=doc)
        self.assertTrue(got["contract"]["errors"])
        self.assertFalse(got["contract"]["comparable"])
        self.assertTrue(any("version" in e for e in got["contract"]["errors"]))

    def test_different_codegen_rejected(self):
        ref = blank()
        doc = make_doc(codegen="fbs-v1")
        got = metrics.compare(ref, ref, doc=doc)
        self.assertTrue(any("代码代" in e for e in got["contract"]["errors"]))

    def test_aspect_mismatch_rejected(self):
        ref = blank(64, 64)
        doc = make_doc(canvas=(1.0, 0.5))  # 文档画布是 2:1，参考图是 1:1
        got = metrics.compare(ref, ref, doc=doc)
        self.assertTrue(any("宽高比" in e for e in got["contract"]["errors"]))

    def test_no_doc_has_no_errors(self):
        ref = blank()
        got = metrics.compare(ref, ref)
        self.assertEqual(got["contract"]["errors"], [])
        self.assertIsNone(got["contract"]["target_resolution"])

    def test_same_generation(self):
        a = make_doc()
        b = make_doc()
        self.assertTrue(metrics.same_generation(a, b))
        c = make_doc(params={"k": 2})
        self.assertFalse(metrics.same_generation(a, c))
        with self.assertRaises(metrics.FBSError) as ctx:
            metrics.assert_same_generation(a, c)
        self.assertEqual(ctx.exception.code, "E_FBS_INVALID")


# ================================================================== compare 汇总
class TestCompare(unittest.TestCase):
    def test_required_dimensions_present(self):
        ref = fill_rect(hline(blank(), 20, 5, 60, 3, (0, 0, 0)), 40, 50, 10, 30, (200, 60, 60))
        rec = fill_rect(hline(blank(), 20, 5, 60, 2, (0, 0, 0)), 40, 50, 10, 30, (180, 70, 70))
        got = metrics.compare(rec, ref, bg=(1.0, 1.0, 1.0), thickness_bins=None)
        for key in ("resolution", "psnr", "ssim", "rmse", "mae", "structure",
                    "thickness", "tone", "linewidth", "contract", "metrics_version"):
            self.assertIn(key, got)
        for key in ("recall", "precision", "iou", "missing_pct", "extra_pct"):
            self.assertIn(key, got["structure"])
        for key in ("hist_ref", "hist_rec"):
            self.assertIn(key, got["thickness"])
        for key in ("mean_delta_e", "confusion"):
            self.assertIn(key, got["tone"])
        for key in ("ref_median_px", "rec_median_px", "ratio"):
            self.assertIn(key, got["linewidth"])
        self.assertIsInstance(got["resolution"], list)
        self.assertEqual(got["resolution"], [64, 64])

    def test_identical_images(self):
        img = hline(blank(), 20, 5, 60, 5, (10, 20, 30))
        got = metrics.compare(img, img, bg=(1.0, 1.0, 1.0))
        self.assertEqual(got["psnr"], metrics.PSNR_CAP_DB)
        self.assertTrue(got["psnr_capped"])
        self.assertEqual(got["ssim"], 1.0)
        self.assertEqual(got["rmse"], 0.0)
        self.assertEqual(got["mae"], 0.0)
        self.assertEqual(got["structure"]["missing_pct"], 0.0)
        self.assertEqual(got["linewidth"]["ratio"], 1.0)

    def test_resolution_gate_raises(self):
        with self.assertRaises(metrics.FBSError) as ctx:
            metrics.compare(blank(32, 32), blank(64, 64))
        self.assertEqual(ctx.exception.code, "E_METRIC_RESOLUTION")

    def test_float_and_uint8_agree(self):
        rng = np.random.default_rng(5)
        a = rng.integers(0, 256, (20, 20, 3), dtype=np.uint8)
        b = rng.integers(0, 256, (20, 20, 3), dtype=np.uint8)
        g1 = metrics.compare(a, b)
        g2 = metrics.compare(a / 255.0, b / 255.0)
        self.assertAlmostEqual(g1["psnr"], g2["psnr"], places=9)
        self.assertAlmostEqual(g1["ssim"], g2["ssim"], places=9)
        self.assertAlmostEqual(g1["rmse"], g2["rmse"], places=9)

    def test_volume_report_always_carries_R_and_absolutes(self):
        doc = make_doc(resolution=512)
        vol = metrics.volume_report(doc, 12345, 6789, resolution=512)
        self.assertEqual(vol["resolution_R"], 512)
        self.assertEqual(vol["fbs_bytes"], 12345)
        self.assertEqual(vol["png_bytes"], 6789)
        self.assertAlmostEqual(vol["ratio_fbs_over_png"], round(12345 / 6789, 4), places=6)
        self.assertIn("漂移", vol["note"])

    def test_format_report_has_four_dimensions(self):
        img = hline(blank(), 20, 5, 60, 5, (0, 0, 0))
        text = metrics.format_report(metrics.compare(img, img))
        for token in ("结构", "色调", "线宽", "分辨率"):
            self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

