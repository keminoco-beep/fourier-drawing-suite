"""覆盖率场 / 调色板 / 局部质量守恒掩码的单元测试。

覆盖：背景估计（非白背景）、投影式 cov 的线性混合性质与墨量守恒、
局部质量守恒阈值（细线复活、厚区边界不动、禁止全局调低）、
调色板确定性/顺序无关、AA 过渡色合并的空间证据闸门（三色平涂灰发）、
细结构颜色保留、小连通块吸收、SLIC 空间正则。
"""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

from fbs_core import coverage  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.util import WarningLog, label_components  # noqa: E402


def make_img(rgba: np.ndarray) -> dict:
    """把 RGBA8 数组包成 preprocess 输出口径的 img（透明区按白底合成）。"""
    h, w = rgba.shape[:2]
    a = rgba[..., 3].astype(np.float32) / 255.0
    rgb = (rgba[..., :3].astype(np.float32) / 255.0 * a[..., None] + (1.0 - a[..., None])).astype(np.float32)
    return {"rgba": rgba, "rgb": rgb, "alpha": a, "width": w, "height": h,
            "long_side": max(w, h), "path": "mem", "source_mode": "RGBA", "scale": 1.0, "meta": {}}


def blank(h: int = 96, w: int = 96, bg=(255, 255, 255)) -> np.ndarray:
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[..., :3] = bg
    arr[..., 3] = 255
    return arr


def analyze(rgba: np.ndarray, cfg: FBSConfig | None = None, klass: str = "") -> tuple[dict, WarningLog]:
    img = make_img(rgba)
    if klass:
        img["klass"] = klass
    warn = WarningLog()
    return coverage.analyze(img, cfg or FBSConfig(), warn), warn


class TestPaleContentSurvival(unittest.TestCase):
    """近背景色的真实内容不能被当背景剔除（肤色/浅高光/浅蓝衣褶的通用回归）。"""

    def _enclosed_pale_blob(self) -> tuple[np.ndarray, tuple[slice, slice]]:
        arr = blank(140, 140)
        # 黑色圆环 + 近白浅色内部（与背景色差约 10–20，旧 fg_tol 会把它整块判成背景）
        yy, xx = np.mgrid[0:140, 0:140]
        d = np.hypot(yy - 70, xx - 70)
        ring = (d >= 36) & (d <= 41)
        inner = d < 36
        arr[ring, :3] = (10, 10, 10)
        arr[inner, :3] = (250, 243, 240)
        return arr, (slice(45, 95), slice(45, 95))

    def test_pale_enclosed_area_enters_mask(self) -> None:
        arr, sl = self._enclosed_pale_blob()
        an, _warn = analyze(arr, FBSConfig(denoise_sigma=0.0))
        mask = np.asarray(an["mask"])
        cov = np.asarray(an["cov"])
        inner_mask = mask[sl]
        self.assertGreater(float(inner_mask.mean()), 0.8, "封闭的浅色内部被误判为背景")
        self.assertGreater(float(cov[sl].mean()), 0.3, "浅色内部没有覆盖率 → 无法生成填色区域")
        labels = np.asarray(an["labels"])
        self.assertTrue(bool((labels[sl] >= 0).mean() > 0.8), "浅色内部没有进入调色板标签")

    def test_pure_background_stays_background(self) -> None:
        arr, sl = self._enclosed_pale_blob()
        an, _warn = analyze(arr, FBSConfig(denoise_sigma=0.0))
        mask = np.asarray(an["mask"])
        self.assertFalse(bool(mask[0:8, 0:8].any()), "纯背景角落不应产生墨量")


class TestCoverageField(unittest.TestCase):
    def test_linear_blend_projection(self) -> None:
        """线性混合 rgb=(1−t)bg+t·ink 下 cov 必须等于 t（投影式定义的核心性质）。"""
        h, w = 32, 32
        bg = np.array([255.0, 255.0, 255.0])
        ink = np.array([0.0, 0.0, 0.0])
        t = np.linspace(0.0, 1.0, w)[None, :].repeat(h, axis=0)
        rgb = (1.0 - t[..., None]) * bg + t[..., None] * ink
        refs = np.vstack([bg, ink])[None, :, :]  # (1,2,3)，下标 0=bg 1=ink
        refs = refs[0]
        dist_bg = np.linalg.norm(rgb - bg, axis=2)
        lab_ink = np.ones((h, w), dtype=np.int32)
        cov = coverage.coverage_field(rgb / 255.0, bg / 255.0, refs / 255.0, lab_ink, dist_bg, 6.0)
        m = dist_bg > 6.0
        if bool(m.any()):
            self.assertLess(float(np.max(np.abs(cov[m] - t[m]))), 0.02)

    def test_thick_interior_cov_is_one(self) -> None:
        """厚区内部 cov 恒为 1（宽度积分成立的前提）。"""
        arr = blank(96, 96)
        arr[20:76, 20:76, :3] = (30, 60, 200)
        a, _ = analyze(arr)
        cov = a["cov"]
        self.assertGreater(float(cov[40:56, 40:56].mean()), 0.98)
        # 背景区 cov 必须为 0（不能有假墨量）
        self.assertLess(float(cov[0:8, 0:8].mean()), 0.01)

    def test_mass_conservation_of_thick_region(self) -> None:
        """厚区质量守恒：二值面积 / cov 总墨量 ≈ 1。"""
        arr = blank(96, 96)
        arr[20:76, 20:76, :3] = 0
        a, _ = analyze(arr)
        cov_sum = float(a["cov"].sum())
        area = int(a["mask"].sum())
        self.assertGreater(cov_sum, 0.0)
        self.assertLess(abs(area / cov_sum - 1.0), 0.10)


class TestLocalMassMask(unittest.TestCase):
    def test_thin_subpixel_line_survives(self) -> None:
        """亚像素细线（两行各 0.4 覆盖率）峰值 cov<0.5，全局 0.5 阈值必丢；局部守恒必须保住。"""
        arr = blank(64, 64)
        # 附近有厚黑块 → 调色板能拿到纯墨色，细线 cov 才是真实覆盖率 0.4
        arr[5:25, 5:25, :3] = 0
        arr[31, 6:, :3] = 153           # 0.4 覆盖率 → 0.6*255 ≈ 153
        arr[32, 6:, :3] = 153
        a, _ = analyze(arr)
        cov = a["cov"]
        self.assertLess(float(cov[31:33, 10:55].max()), 0.6)
        covered = int(a["mask"][31, 6:].sum()) + int(a["mask"][32, 6:].sum())
        self.assertGreaterEqual(covered, 90, "亚像素细线被掩码丢失")
        # 墨量守恒：二值面积应接近 cov 总墨量（局部阈值不会把细线漏掉）
        ratio = int(a["mask"].sum()) / max(float(cov.sum()), 1e-9)
        self.assertTrue(0.9 < ratio < 1.5, f"mass ratio={ratio:.2f}")

    def test_global_low_threshold_overdraws(self) -> None:
        """统一调低全局阈值会把厚区边缘撑胖；局部守恒的面积必须显著小于全局低阈值。"""
        arr = blank(96, 96)
        arr[24:72, 24:72, :3] = 0
        a, _ = analyze(arr)
        cov = a["cov"]
        local_area = int(a["mask"].sum())
        global_low = int((cov > 0.15).sum())
        # 局部守恒不应把厚区边缘扩一圈（允许 ~2% 误差）
        self.assertLess(local_area, global_low * 1.02, "局部守恒阈值退化为全局低阈值（会多画）")

    def test_thick_boundary_not_eroded(self) -> None:
        """厚区边界不能被侵蚀：掩码面积与真实墨量一致。"""
        arr = blank(96, 96)
        arr[20:76, 20:76, :3] = 0
        a, _ = analyze(arr)
        self.assertLess(abs(int(a["mask"].sum()) - 56 * 56) / (56 * 56), 0.05)

    def test_noise_specks_removed(self) -> None:
        arr = blank(64, 64)
        arr[10:20, 10:20, :3] = 0
        arr[50, 50, :3] = 0          # 孤立单像素噪点
        a, _ = analyze(arr)
        self.assertFalse(bool(a["mask"][50, 50]))


class TestPalette(unittest.TestCase):
    def test_aa_blend_merged_into_ink(self) -> None:
        """抗锯齿过渡灰必须并进墨色（不能各留一个区域色），且不平均墨色（黑仍是黑）。"""
        arr = blank(64, 64)
        arr[30:34, 6:58, :3] = 0
        arr[29, 6:58, :3] = 96       # AA 过渡带
        arr[34, 6:58, :3] = 96
        arr[28, 6:58, :3] = 210
        arr[35, 6:58, :3] = 210
        a, _ = analyze(arr)
        pal = (a["palette"] * 255).round().astype(int)
        self.assertEqual(len(pal), 1, f"AA 过渡色未被并干净: {pal}")
        self.assertLess(int(pal[0].sum()), 30, f"墨色被 AA 灰平均变淡: {pal[0]}")

    def test_three_tone_flat_art_gray_not_merged(self) -> None:
        """三色平涂：灰色的空间证据（自身成片）必须阻止它被并进黑色。"""
        arr = blank(160, 160)                             # 四周留白，背景估计才正确
        arr[20:64, 20:140, :3] = 0                        # 黑色块
        arr[64:132, 20:140, :3] = (148, 148, 157)         # 灰色块（真实色调）
        arr[132:138, 20:140, :3] = 0                      # 黑色线
        a, _ = analyze(arr)
        pal = (a["palette"] * 255).round().astype(int)
        has_gray = any(np.linalg.norm(c - np.array([148, 148, 157])) < 20 for c in pal)
        has_black = any(int(c.sum()) < 40 for c in pal)
        self.assertTrue(has_black, f"黑色丢失: {pal}")
        self.assertTrue(has_gray, f"灰色被并进黑色: {pal}")

    def test_thin_structure_color_kept(self) -> None:
        """细结构颜色必须进调色板（不能只统计腐蚀芯）：1px 纯色线。"""
        arr = blank(64, 64)
        arr[32, :, :3] = (20, 160, 60)   # 1px 绿线（腐蚀后什么都不剩）
        a, _ = analyze(arr)
        pal = (a["palette"] * 255).round().astype(int)
        self.assertTrue(
            any(np.linalg.norm(c - np.array([20, 160, 60])) < 30 for c in pal),
            f"细线颜色未进入调色板: {pal}",
        )

    def test_cluster_colors_order_independent(self) -> None:
        rng = np.random.default_rng(3)
        pal = rng.uniform(0, 255, (40, 3))
        w = rng.integers(1, 100, 40)
        c1, w1 = coverage.cluster_colors(pal, w, thresh=50.0, max_k=8)
        perm = rng.permutation(40)
        c2, w2 = coverage.cluster_colors(pal[perm], w[perm], thresh=50.0, max_k=8)
        self.assertEqual(len(c1), len(c2))
        # 中心集合相同（顺序只由权重/颜色决定）
        c1s = c1[np.lexsort((c1[:, 2], c1[:, 1], c1[:, 0]))]
        c2s = c2[np.lexsort((c2[:, 2], c2[:, 1], c2[:, 0]))]
        self.assertLess(float(np.max(np.abs(c1s - c2s))), 1e-6)

    def test_background_not_assumed_white(self) -> None:
        arr = blank(64, 64, bg=(228, 240, 250))
        arr[24:40, 24:40, :3] = (10, 10, 10)
        a, _ = analyze(arr)
        bg = (a["bg"] * 255).round().astype(int)
        self.assertLess(int(np.abs(bg - np.array([228, 240, 250])).max()), 12)

    def test_deterministic(self) -> None:
        arr = blank(64, 64)
        arr[10:30, 10:30, :3] = (200, 20, 30)
        arr[40, :, :3] = 0
        a1, _ = analyze(arr)
        a2, _ = analyze(arr)
        self.assertTrue(np.array_equal(a1["labels"], a2["labels"]))
        self.assertLess(float(np.max(np.abs(a1["cov"] - a2["cov"]))), 1e-7)
        self.assertTrue(np.array_equal(a1["palette"], a2["palette"]))


class TestSpatialEvidence(unittest.TestCase):
    def test_same_label_erode(self) -> None:
        lab = np.full((32, 32), -1, dtype=np.int32)
        lab[5:25, 5:15] = 0      # 成片区域
        lab[5:25, 20] = 1        # 1px 细带
        surv = coverage.same_label_erode(lab, r=2)
        self.assertTrue(bool(surv[10, 10]))
        self.assertFalse(bool(surv[10, 20]))

    def test_merge_aa_blends_respects_protect(self) -> None:
        bg = np.array([255.0, 255.0, 255.0])
        centers = np.array([[0.0, 0.0, 0.0], [148.0, 148.0, 157.0]])
        weights = np.array([1000, 500])
        merged, _ = coverage.merge_aa_blends(centers, weights, bg, tol=32.0)
        self.assertEqual(len(merged), 1)  # 无空间证据时灰色被并入黑
        merged_p, _ = coverage.merge_aa_blends(
            centers, weights, bg, tol=32.0, protect=np.array([False, True])
        )
        self.assertEqual(len(merged_p), 2)  # 有空间证据时保住

    def test_slic_reduces_fragmentation(self) -> None:
        """逐像素标签在抗锯齿带上碎片化；SLIC 空间正则后同标签连通块数大幅下降。"""
        rng = np.random.default_rng(11)
        h = w = 128
        arr = blank(h, w)
        yy, xx = np.mgrid[0:h, 0:w] / float(max(h, w))
        region = (np.sin(4 * xx) + np.cos(3 * yy)) > 0.0
        arr[region, :3] = (40, 90, 180)
        arr[~region, :3] = (200, 120, 40)
        arr[..., :3] = np.clip(
            arr[..., :3].astype(np.float64) + rng.normal(0, 9.0, (h, w, 3)), 0, 255
        ).astype(np.uint8)
        a, _ = analyze(arr, klass="illustration")
        labels = a["labels"]
        n_comp = 0
        for k in np.unique(labels[labels >= 0]).tolist():
            _, n = label_components(labels == k)
            n_comp += n
        # 有 SLIC 时同标签连通块数应远小于像素数（且区域数有界）
        self.assertLess(n_comp, 400)
        self.assertTrue(bool((labels >= 0).any()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
