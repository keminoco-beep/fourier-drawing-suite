"""笔刷库（现代软件笔刷分类）与笔形内核的单元测试。

契约：笔刷库只是"属性预设"——每个预设有 category / shape / 尺寸/透明度/抖动/压力参数；
笔形由渲染端内核解释（shapelib 注册），不允许出现任何贴图/填充。核心验证：
1. 预设合法且与笔形注册表一致；
2. 每个笔形内核都能落墨、确定（同输入同输出，视频不闪烁）、纹理笔形有颗粒差异；
3. 纯笔触绘画真的按层使用不同笔刷，且 element 携带 shape/preset。
"""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from fbs_core import brushfit, brushlib  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.render import FloatCanvas  # noqa: E402
from fbs_core.shapelib import SHAPE_CODES, VALID_SHAPES  # noqa: E402


def _sample_canvas(shape: str, w=140, h=48, seed_shift=0) -> np.ndarray:
    fc = FloatCanvas(w, h, supersample=2, background_rgb=(1.0, 1.0, 1.0))
    n = 8
    t = np.linspace(0.0, 1.0, n)
    pts = np.stack([8 + t * (w - 16), h / 2 + np.sin(np.pi * t) * 4.0], axis=1)
    radii = np.full(n, 7.0)
    fc.add_band(
        pts * 2, radii * 2,
        np.tile(np.array([0.1, 0.1, 0.12]), (n, 1)),
        np.full(n, 0.95),
        mode="sweep",
        shapes=[shape] * n,
    )
    return fc.to_float()


class TestBrushLibrary(unittest.TestCase):
    def test_presets_valid(self):
        brushlib.validate()
        self.assertGreaterEqual(len(brushlib.BRUSHES), 15)
        cats = {b["category"] for b in brushlib.BRUSHES.values()}
        # 覆盖现代软件的主要笔刷分区
        for need in ("basic", "pencil", "inking", "marker", "dry_painting",
                     "chalk_pastel_charcoal", "watercolor", "airbrush", "spray"):
            self.assertIn(need, cats)
        for bid, b in brushlib.BRUSHES.items():
            self.assertIn(b["shape"], VALID_SHAPES, f"{bid} shape 未注册")
            lo, hi = b["alpha"]
            self.assertLess(lo, hi)
            self.assertTrue(0.0 < b["size_mult"] < 3.0)
            self.assertTrue(0.0 <= b["width_jitter"] < 1.0)
            self.assertTrue(0.0 <= b["color_jitter"] < 0.1)
            self.assertTrue(0.0 <= b["pressure"] <= 1.0)

    def test_layer_brushes_exist(self):
        for layer in range(len(brushlib.LAYER_BRUSHES)):
            ids = brushlib.presets_for_layer(layer)
            self.assertGreater(len(ids), 0)
            for bid in ids:
                self.assertIn(bid, brushlib.BRUSHES)
        # 层索引越界时钳制而不是抛错
        self.assertEqual(brushlib.presets_for_layer(99), brushlib.presets_for_layer(len(brushlib.LAYER_BRUSHES) - 1))

    def test_every_shape_kernel_draws_and_is_deterministic(self):
        for name in VALID_SHAPES:
            a = _sample_canvas(name)
            b = _sample_canvas(name)
            np.testing.assert_allclose(a, b, atol=0.0, err_msg=f"{name} 笔形不确定（视频会闪烁）")
            ink = float(np.mean(1.0 - a[..., 0]))
            self.assertGreater(ink, 0.002, f"{name} 笔形没有落墨")
            self.assertLess(ink, 0.95, f"{name} 笔形覆盖异常（几乎全屏）")

    def test_textured_kernels_differ_from_round(self):
        from fbs_core.util import gaussian_blur

        def high_freq_energy(img: np.ndarray) -> float:
            g = gaussian_blur(img.astype(np.float32), 1.0)
            return float(np.mean(np.abs(img - g)))

        round_img = _sample_canvas("round")
        round_hf = high_freq_energy(round_img)
        for name in ("chalk", "pencil", "spatter"):
            img = _sample_canvas(name)
            self.assertGreater(high_freq_energy(img), round_hf + 0.002,
                               f"{name} 的高频颗粒应比硬圆更明显")
        air = _sample_canvas("airbrush")
        # 喷枪边缘更软：同位置落墨更少
        self.assertLess(float(np.mean(1.0 - air[..., 0])), float(np.mean(1.0 - round_img[..., 0])))


class TestPainterlyUsesBrushLibrary(unittest.TestCase):
    def test_elements_carry_shapes_and_presets(self):
        h = w = 96
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
        img = np.stack([xx / w, yy / h, 0.5 + 0.3 * np.sin(3 * xx / w)], axis=-1)
        cfg = FBSConfig(painterly=True, brush_strokes=600, brush_work_res=96)
        dec = brushfit.decompose_painterly(img, cfg, None, long_side=w)
        presets = set()
        shapes = set()
        for el in dec["elements"]:
            presets.add(el["meta"]["preset"])
            shapes.add(el["meta"]["shape"])
            n = len(el["points"])
            self.assertEqual(len(el["shapes"]), n)
            self.assertTrue(all(s in VALID_SHAPES for s in el["shapes"]))
            self.assertTrue(np.all(el["widths"] > 0))
        self.assertGreaterEqual(len(presets), 3, "多笔触应使用多种笔刷预设")
        self.assertGreaterEqual(len(shapes), 2, "应使用多种笔形")
        self.assertIn("bristle", shapes)  # 干画/油画层必用鬃毛刷
        self.assertIn("chalk", shapes)    # 粉笔/炭笔层


if __name__ == "__main__":
    unittest.main(verbosity=2)
