"""纯笔触绘画（painterly brush fitting）单元测试。

核心契约（用户定义）：
- 表示里**只有**傅里叶笔触（路径 + 宽度/颜色/alpha/笔形），不允许任何填充/面片元素；
- 笔触由粗到细分层、沿结构走向、颜色来自该层概括参考色；
- 能重建出可识别的画面（工作分辨率自检 PSNR 有下限），且笔触属性合法。
"""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from fbs_core import brushfit  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402


def _test_image(h=128, w=128) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    u, v = xx / w, yy / h
    img = np.zeros((h, w, 3))
    img[..., 0] = 0.25 + 0.5 * u
    img[..., 1] = 0.20 + 0.45 * v
    img[..., 2] = 0.30 + 0.3 * np.sin(3 * u) * np.cos(3 * v)
    # 一条暗色斜边（检验沿结构走向运笔 + 高对比细节）
    edge = u + 0.4 * v < 0.55
    img[edge] = np.array([0.10, 0.12, 0.25])
    return np.clip(img, 0.0, 1.0)


class TestPainterlyPainting(unittest.TestCase):
    def test_decompose_is_pure_strokes_only(self):
        img = _test_image()
        cfg = FBSConfig(painterly=True, brush_strokes=320, brush_work_res=128)
        dec = brushfit.decompose_painterly(img, cfg, None, long_side=img.shape[1])
        els = dec["elements"]
        self.assertGreater(len(els), 50)
        self.assertEqual(dec["regions"], [], "纯笔触表示不允许有区域记录")
        for el in els:
            self.assertEqual(el["fill_role"], "medial_branch")
            self.assertIsNone(el.get("fill_poly"), "不允许任何填充多边形（色块投机）")
            self.assertIsNone(el.get("fill_holes"))
            self.assertFalse(el["closed"])
            pts = np.asarray(el["points"], dtype=float)
            self.assertTrue(np.all(np.isfinite(pts)))
            self.assertTrue(np.all(pts >= -1e-9) and np.all(pts <= 1.0 + 1e-9))
            widths = np.asarray(el["widths"], dtype=float)
            self.assertTrue(np.all(widths > 0.0), "笔触宽度必须为正（画笔有笔锋）")
            # 压力曲线：两端细、中段粗
            self.assertLess(widths[0], widths.max())
            self.assertLess(widths[-1], widths.max())
            self.assertTrue(np.all(np.asarray(el["alphas"]) > 0.0))

    def test_layered_coarse_to_fine_and_recognizable(self):
        img = _test_image()
        cfg = FBSConfig(painterly=True, brush_strokes=900, brush_work_res=128)
        dec = brushfit.decompose_painterly(img, cfg, None, long_side=img.shape[1])
        stats = dec["stats"]
        self.assertEqual(stats["method"], "painterly_brush_strokes")
        # 自检：至少比"纯白画布"好很多（证明笔触确实覆盖/重建了画面；纯白≈10dB 左右）
        self.assertGreater(stats["canvas_psnr_db"], 13.0)
        # 分层落笔：细层笔触宽度整体小于粗层
        phases = np.asarray([el["meta"]["phase"] for el in dec["elements"]])
        self.assertGreaterEqual(int(phases.max()), 1)
        w_first = float(np.median([el["widths"].max() for el in dec["elements"] if el["meta"]["phase"] == 0]))
        w_last = float(np.median([el["widths"].max() for el in dec["elements"] if el["meta"]["phase"] == phases.max()]))
        self.assertLess(w_last, w_first, "由粗到细：后层笔触应更细")

    def test_budget_respected(self):
        img = _test_image()
        cfg = FBSConfig(painterly=True, brush_strokes=120, brush_work_res=128)
        dec = brushfit.decompose_painterly(img, cfg, None, long_side=img.shape[1])
        self.assertLessEqual(len(dec["elements"]), 120 + 8)

    def test_photo_route_bypasses_rejection(self):
        from fbs_core import preprocess
        from fbs_core.util import WarningLog

        cfg = FBSConfig(painterly=True)
        h = w = 160
        yy, xx = np.mgrid[0:h, 0:w] / float(max(h, w))
        base = np.zeros((h, w, 3))
        for c in range(3):
            base[..., c] = 0.5 + 0.4 * np.sin(6 * xx + 2 * yy + c)
        rng = np.random.default_rng(3)
        photo = np.clip(base + rng.normal(0, 0.02, base.shape) + rng.normal(0, 3.0 / 255.0, base.shape), 0, 1)
        rgba = np.concatenate([(photo * 255).astype(np.uint8), np.full((h, w, 1), 255, np.uint8)], axis=2)
        a = rgba[..., 3].astype(np.float32) / 255.0
        rgb = (rgba[..., :3].astype(np.float32) / 255.0 * a[..., None] + (1.0 - a[..., None])).astype(np.float32)
        img = {"rgba": rgba, "rgb": rgb, "alpha": a, "width": w, "height": h,
               "long_side": max(w, h), "path": "mem", "source_mode": "RGBA", "scale": 1.0, "meta": {}}
        res = preprocess.classify(img, cfg)
        self.assertNotIn("reject", res, "开启 --painterly 后照片应走纯笔触通道")
        self.assertEqual(res["klass"], "illustration")


if __name__ == "__main__":
    unittest.main(verbosity=2)
