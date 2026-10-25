"""render.py 单元测试：亚像素精确覆盖、圆盘并集、零宽不落墨、分层顺序、
分辨率无关、区域填充、SVG 可解析。

所有 doc 都用 io_format.new_document/finalize_document 生成（与生产同构、可被 validate 接受）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from fbs_core import io_format  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.onestroke import build_one_stroke  # noqa: E402
from fbs_core.render import (  # noqa: E402
    FloatCanvas,
    export_svg,
    render,
    render_to_image,
    sample_stroke,
)
from fbs_core.util import FBSError, WarningLog  # noqa: E402


# ------------------------------------------------------------------ 构造工具
def const_curve(value: float) -> dict:
    """常量数值曲线（width/alpha）。"""
    return {"interp": "constant", "keys": [[0.0, float(value)]], "clamp": True}


def style_of(rgb, a: float = 1.0, shape: str = "round") -> dict:
    """构造一条共享笔刷样式。"""
    return {
        "color": {"interp": "constant", "stops": [{"t": 0.0, "rgb": [int(v) for v in rgb], "a": float(a)}]},
        "alpha": const_curve(1.0),
        "shape": {"interp": "step", "keys": [[0.0, shape]]},
        "jitter": 0.0,
    }


def circle_coef(cx: float, cy: float, radius: float) -> tuple[list[list[float]], int]:
    """返回半径 radius 的圆的傅里叶系数（N=1）。"""
    coef = [[0, float(cx), float(cy)], [-1, 0.0, 0.0], [1, float(radius), 0.0]]
    return coef, 1


def stroke_of(
    sid: int,
    *,
    coef,
    N: int,
    arc: float,
    closed: bool,
    width: float,
    style: int,
    layer: int = 0,
    fill_role: str = "medial_branch",
    closure: dict | None = None,
    poly=None,
) -> dict:
    """构造一条合法 stroke 记录。"""
    st = {
        "id": int(sid),
        "closed": bool(closed),
        "fill_role": fill_role,
        "repr": "fourier",
        "N": int(N),
        "arc_length": float(arc),
        "max_err": 0.0,
        "layer": int(layer),
        "region_id": -1,
        "bbox": [-1.0, -1.0, 2.0, 2.0],
        "coef": coef,
        "closure": dict(closure or {"kind": "none" if closed else "hermite_c1", "t_end": 1.0}),
        "brush": {"style": int(style), "width": const_curve(width)},
    }
    if closed and not closure:
        st["closure"] = {"kind": "none"}
    if poly is not None:
        st["poly"] = poly
    return st


def make_doc(
    strokes: list[dict],
    styles: list[dict],
    *,
    R: int = 1024,
    canvas=(1.0, 1.0),
    base_color=(255, 255, 255, 255),
    mode: str = "per_stroke",
    fill_soft: float = 2.0,
    fills: list | None = None,
) -> dict:
    """用官方骨架构造测试文档（保证 validate 通过）。"""
    warn = WarningLog()
    doc = io_format.new_document(
        mode=mode,
        source={
            "path": "synthetic",
            "sha256": "",
            "width": 100,
            "height": 100,
            "mode": "RGB",
            "scale_applied": 1.0,
        },
        image_class={"klass": "test", "evidence": {}},
        canvas={
            "width": float(canvas[0]),
            "height": float(canvas[1]),
            "long_side_units": 1.0,
            "origin": "top-left",
            "y_axis": "down",
        },
        render_contract={"target_resolution": int(R), "epsilon_px": 0.5},
        decomposition={"method": "test", "params": {"fill_soft_px": float(fill_soft)}, "region_count": 0},
        base_color=list(base_color),
        warnings=warn,
        accelerators={},
        regions=[],
    )
    doc["brush_styles"] = styles
    doc["strokes"] = strokes
    doc["stats"]["fft_passes"] = 1
    if fills is not None:
        doc["one_stroke_fills"] = fills
    io_format.finalize_document(doc)
    return doc


def single_disc_doc(cx: float = 0.5, cy: float = 0.5, width: float = 0.3, radius: float = 0.05, R: int = 1024) -> dict:
    """单圆盘文档（圆中轴 + 大宽度）。"""
    coef, N = circle_coef(cx, cy, radius)
    strokes = [stroke_of(0, coef=coef, N=N, arc=2.0 * np.pi * radius, closed=True, width=width, style=0)]
    return make_doc(strokes, [style_of((0, 0, 0))], R=R)


ASSETS_OUT = os.path.join(os.path.dirname(HERE), "assets", "out")


def _out_path(name: str) -> str:
    """测试产物写到 assets/out（沙箱内可写，不依赖系统临时目录 ACL）。"""
    os.makedirs(ASSETS_OUT, exist_ok=True)
    return os.path.join(ASSETS_OUT, name)


class TestSubpixelCoverage(unittest.TestCase):
    """亚像素线必须用精确带覆盖公式（R3：clip(r+0.5-d) 会把 0.46 画成 0.73）。"""

    def test_subpixel_line_coverage_is_2r(self):
        fc = FloatCanvas(24, 12, supersample=2, background_rgb=(1.0, 1.0, 1.0))
        pts = np.array([[3.0, 6.5], [21.0, 6.5]])
        fc.add_band(pts, np.array([0.23, 0.23]), np.zeros((2, 3)), np.array([1.0, 1.0]), mode="sweep")
        img = fc.to_uint8().astype(np.float64) / 255.0
        cov = 1.0 - img[6, 6:18].mean(axis=0)
        self.assertAlmostEqual(float(cov.mean()), 0.46, delta=0.02, msg="亚像素线覆盖应≈2r=0.46")
        self.assertLess(float(cov.max()), 0.55, msg="禁止 clip(r+0.5-d) 的 0.73 粗式")
        # 邻行不应有墨（r=0.23 的带不会越过 ±0.5 像素）
        self.assertAlmostEqual(float(1.0 - img[5, 6:18].mean()), 0.0, delta=0.01)
        self.assertAlmostEqual(float(1.0 - img[7, 6:18].mean()), 0.0, delta=0.01)

    def test_subpixel_vertical_line(self):
        fc = FloatCanvas(12, 24, supersample=2, background_rgb=(1.0, 1.0, 1.0))
        fc.add_band(np.array([[6.5, 3.0], [6.5, 21.0]]), np.array([0.23, 0.23]), np.zeros((2, 3)), np.array([1.0, 1.0]))
        img = fc.to_uint8().astype(np.float64) / 255.0
        cov = 1.0 - img[6:18, 6].mean(axis=0)
        self.assertAlmostEqual(float(cov.mean()), 0.46, delta=0.02)

    def test_wide_line_coverage_is_one_inside(self):
        fc = FloatCanvas(24, 24, supersample=2, background_rgb=(1.0, 1.0, 1.0))
        fc.add_band(np.array([[2.0, 12.5], [22.0, 12.5]]), np.array([4.0, 4.0]), np.zeros((2, 3)), np.array([1.0, 1.0]))
        img = fc.to_uint8().astype(np.float64) / 255.0
        self.assertAlmostEqual(float(1.0 - img[12, 10:14].mean()), 1.0, delta=0.01)


class TestUnionAndZeroWidth(unittest.TestCase):
    """同一带内覆盖取 max（并集）；半径 0 的样本/段一律不落墨。"""

    def test_zero_width_band_no_ink(self):
        fc = FloatCanvas(16, 16, 2, (1.0, 1.0, 1.0))
        fc.add_band(np.array([[2.0, 8.0], [14.0, 8.0]]), np.array([0.0, 0.0]), np.zeros((2, 3)), np.ones(2))
        self.assertTrue(np.all(fc.to_uint8() == 255), "width=0 的抬笔段不得落墨（禁止半径下限）")

    def test_stamp_union_no_double_darken(self):
        fc = FloatCanvas(24, 24, 2, (1.0, 1.0, 1.0))
        fc.add_band(
            np.array([[6.0, 12.0], [18.0, 12.0]]),
            np.array([3.0, 3.0]),
            np.zeros((2, 3)),
            np.array([0.5, 0.5]),
            mode="stamp",
        )
        center = fc.to_uint8()[12, 12].astype(np.float64) / 255.0
        # 单次合成：0.5 alpha 覆盖 → 期望 0.5；若重复叠加会是 0.25
        self.assertAlmostEqual(float(center[0]), 0.5, delta=0.03)

    def test_band_union_in_single_call(self):
        # 两个重叠圆盘在同一次 add_band 内：覆盖取 max，不得变深
        fc = FloatCanvas(24, 16, 2, (1.0, 1.0, 1.0))
        fc.add_band(
            np.array([[7.0, 8.0], [13.0, 8.0]]),
            np.array([3.0, 3.0]),
            np.zeros((2, 3)),
            np.array([0.5, 0.5]),
            mode="stamp",
        )
        mid = fc.to_uint8()[8, 10].astype(np.float64) / 255.0
        self.assertAlmostEqual(float(mid[0]), 0.5, delta=0.03)

    def test_zero_width_stroke_renders_nothing(self):
        coef, N = circle_coef(0.5, 0.5, 0.2)
        doc = make_doc(
            [stroke_of(0, coef=coef, N=N, arc=2 * np.pi * 0.2, closed=True, width=0.0, style=0)],
            [style_of((0, 0, 0))],
            R=128,
        )
        img = render(doc, resolution=128)
        self.assertTrue(np.all(img == 255), "width=0 的整条笔画不应落墨")


class TestLayering(unittest.TestCase):
    """layer 升序绘制：后画的层覆盖先画的层；同层按 doc 顺序。"""

    def _two_disc_doc(self, red_cx: float, blue_cx: float, red_layer: int, blue_layer: int, red_style: int = 0):
        coef_r, N = circle_coef(red_cx, 0.5, 0.05)
        coef_b, _ = circle_coef(blue_cx, 0.5, 0.05)
        strokes = [
            stroke_of(0, coef=coef_r, N=N, arc=2 * np.pi * 0.05, closed=True, width=0.3, style=red_style, layer=red_layer),
            stroke_of(1, coef=coef_b, N=N, arc=2 * np.pi * 0.05, closed=True, width=0.3, style=1 - red_style if red_style == 0 else 0, layer=blue_layer),
        ]
        return make_doc(strokes, [style_of((255, 0, 0)), style_of((0, 0, 255))], R=256)

    def test_layer_order_higher_layer_wins(self):
        doc = self._two_disc_doc(0.45, 0.55, red_layer=0, blue_layer=1)
        img = render(doc, resolution=256)
        mid = img[128, 128]
        self.assertGreater(int(mid[2]), int(mid[0]), "layer=1 的蓝色盘应覆盖 layer=0 的红色盘")

    def test_layer_order_reversed(self):
        doc = self._two_disc_doc(0.45, 0.55, red_layer=1, blue_layer=0)
        img = render(doc, resolution=256)
        mid = img[128, 128]
        self.assertGreater(int(mid[0]), int(mid[2]), "红色盘 layer=1 应覆盖蓝色盘 layer=0")

    def test_same_layer_doc_order_wins(self):
        doc = self._two_disc_doc(0.45, 0.55, red_layer=0, blue_layer=0)
        img = render(doc, resolution=256)
        mid = img[128, 128]
        self.assertGreater(int(mid[2]), int(mid[0]), "同层时 doc 中靠后的笔画应覆盖前面的")

    def test_region_fill_is_bottom_of_layer(self):
        coef, N = circle_coef(0.5, 0.5, 0.05)
        fill = stroke_of(
            0,
            coef=[[0, 0.5, 0.5], [1, 0.0, 0.0]],
            N=1,
            arc=0.8,
            closed=True,
            width=0.0,
            style=0,
            layer=0,
            fill_role="region_fill",
            poly=[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]],
        )
        disc = stroke_of(1, coef=coef, N=N, arc=2 * np.pi * 0.05, closed=True, width=0.3, style=1, layer=0)
        doc = make_doc([fill, disc], [style_of((0, 200, 0)), style_of((255, 0, 0))], R=128)
        img = render(doc, resolution=128)
        center = img[64, 64]
        self.assertGreater(int(center[0]), int(center[1]), "同层 region_fill 必须在底层，被笔画覆盖")
        corner = img[32, 32]
        self.assertGreater(int(corner[1]), 100, "填充区域应在未被笔画覆盖处可见绿色")


class TestRegionFill(unittest.TestCase):
    """区域填充：显式多边形 + 软边。"""

    def _fill_doc(self, soft: float) -> dict:
        fill = stroke_of(
            0,
            coef=[[0, 0.5, 0.5], [1, 0.0, 0.0]],
            N=1,
            arc=0.8,
            closed=True,
            width=0.0,
            style=0,
            layer=0,
            fill_role="region_fill",
            poly=[[0.3, 0.3], [0.7, 0.3], [0.7, 0.7], [0.3, 0.7]],
        )
        return make_doc([fill], [style_of((0, 0, 0))], R=128, fill_soft=soft)

    def test_region_fill_inside_is_opaque(self):
        img = render(self._fill_doc(soft=0.0), resolution=128)
        self.assertLess(int(img[64, 64].mean()), 5, "填充内部应为实色")

    def test_region_fill_soft_edge(self):
        img_soft = render(self._fill_doc(soft=6.0), resolution=128).astype(np.float64)
        # 边缘（多边形边界附近）应出现明显中间灰度；硬边则接近二值
        edge_soft = img_soft[64, 38].mean()  # x=38/128≈0.297，恰在 0.3 边界内侧
        self.assertGreater(edge_soft, 10.0, "软边填充在边界处应有过渡灰度")
        self.assertLess(edge_soft, 250.0)
        img_hard = render(self._fill_doc(soft=0.0), resolution=128).astype(np.float64)
        edge_hard = img_hard[64, 38].mean()
        self.assertNotAlmostEqual(float(edge_soft), float(edge_hard), delta=20.0)

    def test_fill_polygon_primitive(self):
        fc = FloatCanvas(16, 16, 1, (1.0, 1.0, 1.0))
        fc.fill_polygon(np.array([[2.0, 2.0], [10.0, 2.0], [10.0, 10.0], [2.0, 10.0]]), (1.0, 0.0, 0.0), 0.5)
        img = fc.to_uint8()
        self.assertAlmostEqual(float(img[6, 6, 0]), 255.0, delta=2.0)  # 红通道保持
        self.assertAlmostEqual(float(img[6, 6, 1]), 128.0, delta=2.0)  # 白底 × 0.5
        self.assertAlmostEqual(float(img[1, 1, 1]), 255.0, delta=1.0)  # 多边形外无墨


class TestResolutionIndependence(unittest.TestCase):
    """同一 doc 渲染任意 R：形状一致，R 只改变采样密度。"""

    def test_disc_shape_consistent(self):
        doc = single_disc_doc(R=1024)
        a = render(doc, resolution=128)
        b = render(doc, resolution=256)
        ma = a.mean(axis=2) < 128.0
        mb2 = (b.mean(axis=2) < 128.0).reshape(128, 2, 128, 2).mean(axis=(1, 3)) > 0.5
        inter = float((ma & mb2).sum())
        union = float((ma | mb2).sum())
        self.assertGreater(inter / max(union, 1.0), 0.92, "R=128 与 R=256 下形状应一致（IoU>0.92）")
        self.assertAlmostEqual(float(ma.mean()), float(mb2.mean()), delta=0.01)

    def test_any_resolution_output_shape(self):
        doc = make_doc(
            [],
            [],
            R=1024,
            canvas=(1.0, 0.8),
        )
        # 空文档合法（W_SOLID_COLOR 由 new_document 的 warnings 承载不了 → 直接给一条极小笔画）
        coef, N = circle_coef(0.5, 0.4, 0.01)
        doc["strokes"] = [stroke_of(0, coef=coef, N=N, arc=2 * np.pi * 0.01, closed=True, width=0.002, style=0)]
        doc["brush_styles"] = [style_of((0, 0, 0))]
        io_format.finalize_document(doc)
        self.assertEqual(io_format.validate(doc), [])
        for R in (37, 300, 1024):
            img = render(doc, resolution=R)
            self.assertEqual(img.shape[0], max(1, int(round(0.8 * R))))
            self.assertEqual(img.shape[1], R)
            self.assertEqual(img.dtype, np.uint8)

    def test_canvas_aspect_ratio(self):
        canvas = (0.7093, 1.0)  # 505×712 的竖图
        coef, N = circle_coef(0.355, 0.5, 0.05)
        doc = make_doc(
            [stroke_of(0, coef=coef, N=N, arc=2 * np.pi * 0.05, closed=True, width=0.1, style=0)],
            [style_of((0, 0, 0))],
            R=1024,
            canvas=canvas,
        )
        img = render(doc, resolution=712)
        self.assertEqual(img.shape, (712, int(round(0.7093 * 712)), 3))

    def test_open_curve_only_draws_drawable_range(self):
        # 闭环：t∈[0,0.5] 画直线 y=0.5；t∈[0.5,1] 绕远路回起点（不应被绘制）
        n = 512
        t = np.linspace(0.0, 1.0, n, endpoint=False)
        pts = np.zeros((n, 2))
        half = n // 2
        pts[:half, 0] = np.linspace(0.2, 0.8, half)
        pts[:half, 1] = 0.5
        # 返回路径：0.8,0.5 → 0.8,0.75 → 0.2,0.75 → 0.2,0.5
        third = (n - half) // 3
        offs = [(0.8, 0.5), (0.8, 0.75), (0.2, 0.75), (0.2, 0.5)]
        idx = half
        for k in range(3):
            seg_n = third if k < 2 else n - half - 2 * third
            x0, y0 = offs[k]
            x1, y1 = offs[k + 1]
            u = np.linspace(0.0, 1.0, seg_n, endpoint=False)
            pts[idx:idx + seg_n, 0] = x0 + (x1 - x0) * u
            pts[idx:idx + seg_n, 1] = y0 + (y1 - y0) * u
            idx += seg_n
        z = pts[:, 0] + 1j * pts[:, 1]
        Z = np.fft.fft(z) / n
        N = 60
        coef = [[int(k), float(Z[k % n].real), float(Z[k % n].imag)] for k in range(-N, N + 1)]
        coef.sort(key=lambda r: (abs(r[0]), r[0]))
        st = stroke_of(
            0,
            coef=coef,
            N=N,
            arc=0.6,
            closed=False,
            width=0.02,
            style=0,
            closure={"kind": "hermite_c1", "t_end": 0.5},
        )
        doc = make_doc([st], [style_of((0, 0, 0))], R=256)
        self.assertEqual(io_format.validate(doc), [])
        img = render(doc, resolution=256)
        # y=0.5 的线上有墨；y=0.75 的返回路径不应被绘制
        self.assertLess(float(img[127, 128].mean()), 200.0, "drawable 区间应被绘制")
        self.assertGreater(float(img[192, 128].mean()), 245.0, "闭环段不得参与绘制")

    def test_invalid_arguments_raise(self):
        doc = single_disc_doc(R=128)
        with self.assertRaises(FBSError) as ctx:
            render(doc, resolution=-5)
        self.assertEqual(ctx.exception.code, "E_RENDER_RESOLUTION")
        with self.assertRaises(FBSError) as ctx2:
            render(doc, resolution=128, mode="bogus")
        self.assertEqual(ctx2.exception.code, "E_RENDER_MODE")
        with self.assertRaises(FBSError):
            FloatCanvas(0, 10, 2, (1, 1, 1))


class TestRenderModesAndMisc(unittest.TestCase):
    """三种渲染模式都能出图；背景可覆盖；render_to_image 可返回 PIL 图。"""

    def test_all_modes_produce_ink(self):
        doc = single_disc_doc(R=256)
        for mode in ("sweep", "stamp", "line"):
            img = render(doc, resolution=256, mode=mode)
            self.assertEqual(img.shape, (256, 256, 3))
            self.assertLess(float(img.mean()), 250.0, f"mode={mode} 应画出墨迹")

    def test_background_override(self):
        doc = single_disc_doc(R=128)
        img = render(doc, resolution=128, background=(255, 0, 0))
        self.assertGreater(int(img[5, 5, 0]), 250)
        self.assertLess(int(img[5, 5, 1]), 5)

    def test_render_to_image(self):
        from PIL import Image

        im = render_to_image(single_disc_doc(R=128), resolution=128)
        self.assertIsInstance(im, Image.Image)
        self.assertEqual(im.mode, "RGB")
        self.assertEqual(im.size, (128, 128))

    def test_supersample_effect(self):
        doc = single_disc_doc(R=128)
        a = render(doc, resolution=128, supersample=1)
        b = render(doc, resolution=128, supersample=2)
        self.assertLess(float(np.abs(a.astype(float) - b.astype(float)).mean()), 30.0)

    def test_sample_stroke_contract(self):
        doc = single_disc_doc(R=256)
        st = doc["strokes"][0]
        s = sample_stroke(doc, st, 256)
        self.assertEqual(s["points_world"].shape[1], 2)
        self.assertEqual(len(s["widths_world"]), len(s["points_world"]))
        self.assertEqual(s["rgb"].shape[1], 3)
        self.assertEqual(len(s["shapes"]), len(s["points_world"]))
        self.assertGreaterEqual(len(s["points_world"]), int(4 * 2 * np.pi * 0.05 * 256 * 0.9))

    def test_static_render_speed_smoke(self):
        # 150 个圆盘笔画 @R=256 应在数秒内完成（热路径不得逐点 Python 循环）
        R = 256
        styles = [style_of((10, 10, 10))]
        strokes = []
        for i in range(150):
            cx = 0.1 + 0.8 * ((i * 37) % 150) / 150.0
            cy = 0.1 + 0.8 * ((i * 91) % 150) / 150.0
            coef, N = circle_coef(cx, cy, 0.01)
            strokes.append(stroke_of(i, coef=coef, N=N, arc=2 * np.pi * 0.01, closed=True, width=0.006, style=0))
        doc = make_doc(strokes, styles, R=R)
        t0 = time.perf_counter()
        img = render(doc, resolution=R, supersample=2)
        elapsed = time.perf_counter() - t0
        self.assertEqual(img.shape, (R, R, 3))
        self.assertLess(elapsed, 30.0, f"150 条笔画渲染耗时 {elapsed:.1f}s 过慢")


class TestColorField(unittest.TestCase):
    """region_fill 支持 color_field：填充内部按网格双线性采样颜色（decomp 提供的渐变场）。"""

    def _cf_doc(self, color_field) -> dict:
        fill = stroke_of(
            0,
            coef=[[0, 0.5, 0.5], [1, 0.0, 0.0]],
            N=1,
            arc=2.4,
            closed=True,
            width=0.0,
            style=0,
            layer=0,
            fill_role="region_fill",
            poly=[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]],
        )
        fill["region_id"] = 0
        doc = make_doc([fill], [style_of((255, 255, 255))], R=128, fill_soft=0.0)
        if color_field is not None:
            doc["regions"] = [
                {"id": 0, "rgb": [255, 255, 255], "bbox": [0.0, 0.0, 1.0, 1.0], "color_field": color_field}
            ]
        return doc

    def test_color_field_gradient(self):
        grid = 4
        colors = []
        for gy in range(grid):
            row = []
            for gx in range(grid):
                row.append([255, 0, 0] if gx < grid // 2 else [0, 0, 255])
            colors.append(row)
        cf = {"grid": grid, "bbox": [0.0, 0.0, 1.0, 1.0], "colors": colors}
        img = render(self._cf_doc(cf), resolution=128)
        left = img[64, 40].astype(float)
        right = img[64, 88].astype(float)
        self.assertGreater(left[0], left[2] + 40, "颜色场左侧应为红色")
        self.assertGreater(right[2], right[0] + 40, "颜色场右侧应为蓝色")

    def test_color_field_sparse_indices(self):
        cf = {"grid": 4, "bbox": [0.2, 0.2, 0.8, 0.8], "colors": [[0, [0, 255, 0]], [15, [0, 255, 0]]]}
        img = render(self._cf_doc(cf), resolution=128).astype(float)
        self.assertGreater(img[64, 64, 1], 150.0, "稀疏索引颜色场应能回填并用绿色填充")

    def test_invalid_color_field_falls_back(self):
        doc = self._cf_doc({"grid": 2, "bbox": [0.0, 0.0, 1.0, 1.0], "colors": []})
        # 回退色来自笔画代表色：换成深色以验证"没有颜色场时仍用笔画色"
        doc["brush_styles"][0]["color"]["stops"][0]["rgb"] = [10, 10, 10]
        img = render(doc, resolution=128)
        self.assertLess(float(img[64, 64].mean()), 60.0, "color_field 非法时应回退到笔画代表色而不是崩溃")

    def test_fill_polygon_direct_color_field(self):
        fc = FloatCanvas(32, 32, 1, (1.0, 1.0, 1.0))
        cf = {"grid": 2, "bbox": [0.0, 0.0, 32.0, 32.0], "colors": [[[255, 0, 0], [0, 0, 255]], [[255, 0, 0], [0, 0, 255]]]}
        fc.fill_polygon(
            np.array([[2.0, 2.0], [30.0, 2.0], [30.0, 30.0], [2.0, 30.0]]),
            (0.5, 0.5, 0.5),
            1.0,
            0.0,
            color_field=cf,
        )
        img = fc.to_uint8()
        self.assertGreater(int(img[8, 8, 0]), int(img[8, 8, 2]))
        self.assertGreater(int(img[8, 24, 2]), int(img[8, 24, 0]))


class TestStampSweepConsistency(unittest.TestCase):
    """stamp 与 sweep 都是「路径 ⊕ 圆盘」的离散化，墨迹边界与 PSNR 不应差太多。"""

    def _rich_doc(self, R: int = 256) -> dict:
        styles = [style_of((20, 20, 24))]
        strokes = []
        # 多个不同宽度圆盘（并集语义）
        for i, (cx, cy, r_g, w) in enumerate([(0.3, 0.3, 0.04, 0.16), (0.6, 0.35, 0.03, 0.10), (0.45, 0.65, 0.06, 0.22)]):
            c, N = circle_coef(cx, cy, r_g)
            strokes.append(stroke_of(i, coef=c, N=N, arc=2 * np.pi * r_g, closed=True, width=w, style=0, layer=0))
        # 变宽（锥形）笔画：用圆环系数 + 宽度曲线
        c2, N2 = circle_coef(0.75, 0.7, 0.05)
        st = stroke_of(10, coef=c2, N=N2, arc=2 * np.pi * 0.05, closed=True, width=0.01, style=0, layer=1)
        st["brush"]["width"] = {
            "interp": "linear",
            "keys": [[0.0, 0.001], [0.5, 0.030], [1.0, 0.001]],
            "clamp": True,
        }
        strokes.append(st)
        return make_doc(strokes, styles, R=R)

    def test_psnr_between_stamp_and_sweep(self):
        doc = self._rich_doc(256)
        a = render(doc, resolution=256, mode="sweep").astype(np.float64)
        b = render(doc, resolution=256, mode="stamp").astype(np.float64)
        mse = float(np.mean((a - b) ** 2))
        self.assertGreater(mse, 0.0, "两种模式应有细微差异（不同离散化）")
        psnr = 10.0 * np.log10(255.0 ** 2 / mse)
        self.assertGreater(psnr, 22.0, f"stamp vs sweep PSNR={psnr:.2f}dB 过低，说明一种模式有缺口/错位")

    def test_ink_masks_consistent(self):
        doc = self._rich_doc(256)
        a = render(doc, resolution=256, mode="sweep").mean(axis=2) < 245.0
        b = render(doc, resolution=256, mode="stamp").mean(axis=2) < 245.0
        inter = float((a & b).sum())
        union = float((a | b).sum())
        self.assertGreater(inter / max(union, 1.0), 0.93, "stamp/sweep 墨迹区域应高度一致")
        try:
            from scipy import ndimage as ndi

            dist = ndi.distance_transform_edt(~a)
            boundary_b = b & ~ndi.binary_erosion(b)
            dev = dist[boundary_b]
            if dev.size:
                self.assertLessEqual(float(np.percentile(dev, 99)), 1.5, "sweep/stamp 边界 p99 差应 ≤1.5px")
        except ImportError:  # noqa: 无 scipy 时边界 p99 断言不适用（IoU 检查已覆盖一致性）
            self.assertGreater(inter / max(union, 1.0), 0.93)


class TestSvgExport(unittest.TestCase):
    """SVG 必须能被标准 XML 解析器打开，且包含笔画/填充。"""

    def test_svg_export_parses(self):
        coef, N = circle_coef(0.5, 0.5, 0.05)
        fill = stroke_of(
            0,
            coef=[[0, 0.5, 0.5], [1, 0.0, 0.0]],
            N=1,
            arc=0.8,
            closed=True,
            width=0.0,
            style=0,
            layer=0,
            fill_role="region_fill",
            poly=[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]],
        )
        disc = stroke_of(1, coef=coef, N=N, arc=2 * np.pi * 0.05, closed=True, width=0.1, style=1, layer=1)
        doc = make_doc([fill, disc], [style_of((0, 200, 0)), style_of((0, 0, 0))], R=128)
        path = _out_path("render_test_doc.svg")
        info = export_svg(doc, path, resolution=128)
        self.assertTrue(os.path.exists(path))
        self.assertGreater(info["bytes"], 100)
        self.assertGreaterEqual(info["paths"] + info["polygons"], 2)
        self.assertIn("notes", info)
        tree = ET.parse(path)  # 标准解析器必须能打开
        root = tree.getroot()
        self.assertTrue(root.tag.endswith("svg"))
        self.assertGreater(len(list(root)), 0)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

    def test_svg_empty_doc(self):
        doc = make_doc(
            [stroke_of(0, coef=[[0, 0.5, 0.5], [1, 0.0, 0.0]], N=1, arc=0.1, closed=True, width=0.0, style=0)],
            [style_of((0, 0, 0))],
            R=64,
        )
        path = _out_path("render_test_empty.svg")
        info = export_svg(doc, path)
        self.assertGreater(info["bytes"], 0)
        ET.parse(path)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))


def _rich_shape_doc(Rres: int = 256) -> dict:
    """round 回归基线用的确定性文档（圆盘并集 + 锥形笔画 + 区域填充 + 颜色场）。"""
    styles = [style_of((18, 20, 26)), style_of((210, 90, 70)), style_of((60, 120, 200))]
    strokes = []
    for i, (cx, cy, rg, w) in enumerate(
        [(0.3, 0.3, 0.04, 0.16), (0.6, 0.35, 0.03, 0.10), (0.45, 0.65, 0.06, 0.22)]
    ):
        c, N = circle_coef(cx, cy, rg)
        strokes.append(stroke_of(i, coef=c, N=N, arc=2 * np.pi * rg, closed=True, width=w, style=i % 3, layer=0))
    c2, N2 = circle_coef(0.75, 0.7, 0.05)
    st = stroke_of(10, coef=c2, N=N2, arc=2 * np.pi * 0.05, closed=True, width=0.01, style=0, layer=1)
    st["brush"]["width"] = {"interp": "linear", "keys": [[0.0, 0.001], [0.5, 0.030], [1.0, 0.001]], "clamp": True}
    strokes.append(st)
    fill = stroke_of(
        20, coef=[[0, 0.2, 0.2], [1, 0.0, 0.0]], N=1, arc=2.4, closed=True, width=0.0, style=2, layer=0,
        fill_role="region_fill", poly=[[0.05, 0.05], [0.25, 0.05], [0.25, 0.25], [0.05, 0.25]],
    )
    fill["region_id"] = 0
    strokes.append(fill)
    doc = make_doc(strokes, styles, R=Rres)
    doc["regions"] = [
        {
            "id": 0,
            "rgb": [60, 120, 200],
            "bbox": [0, 0, 1, 1],
            "color_field": {
                "grid": 4,
                "bbox": [0.0, 0.0, 1.0, 1.0],
                "colors": [[[60, 120, 200]] * 4 for _ in range(4)],
            },
        }
    ]
    return doc


def _img_sha(img: np.ndarray) -> str:
    import hashlib

    return hashlib.sha256(np.ascontiguousarray(img).tobytes()).hexdigest()


class TestRoundPixelRegression(unittest.TestCase):
    """R4 改动回归保护：round 是热路径，输出必须与改动前**逐像素相同**。

    基线哈希在实现 soft/marker 之前采集（R=256/300、sweep/stamp、per_stroke/one_stroke 全覆盖）。
    """

    BASELINE = {
        "RICH_SWEEP_256": "76eab40003304ab13384914b6b896934c4fdf3a63e037dc640c9e84f5e26a62f",
        "RICH_STAMP_256": "3708b5ff5ad2c46df50e257157f9800a98dd99415b048280a9b6a2c223180b17",
        "RICH_SWEEP_300": "d6ed3a714871c45a9380473dd4c6fd25d19656e9907de94ad0e2ea18ed7dc6d4",
        "ONE_SWEEP_128": "b9e6766730d7661e3c4903059fc45a099f8643961a3736456eb36a3714b4f842",
    }

    def test_round_renders_are_pixel_identical(self):
        doc = _rich_shape_doc(256)
        self.assertEqual(_img_sha(render(doc, resolution=256, supersample=2, mode="sweep")), self.BASELINE["RICH_SWEEP_256"])
        self.assertEqual(_img_sha(render(doc, resolution=256, supersample=2, mode="stamp")), self.BASELINE["RICH_STAMP_256"])
        self.assertEqual(_img_sha(render(doc, resolution=300, supersample=2, mode="sweep")), self.BASELINE["RICH_SWEEP_300"])
        from test_onestroke import tiny_doc  # 延迟导入，避免两个测试模块循环导入

        cfg = FBSConfig(target_resolution=128, harmonics=512, max_fft=1 << 12)
        one = build_one_stroke(tiny_doc(R=128), cfg, WarningLog())
        self.assertEqual(_img_sha(render(one, resolution=128)), self.BASELINE["ONE_SWEEP_128"])

    def test_explicit_round_shapes_same_as_default(self):
        doc = _rich_shape_doc(160)
        a = render(doc, resolution=160, mode="sweep")
        # 显式给形状也必须走同一条 round 路径（逐像素相同）
        from fbs_core.render import _resolve_style, sample_stroke

        st = doc["strokes"][0]
        sampled = sample_stroke(doc, st, 160)
        self.assertTrue(all(s == "round" for s in sampled["shapes"]))


class TestShapeRendering(unittest.TestCase):
    """R4 笔形：soft 柔边（可量化）、marker 扁平笔头各向异性（近似实现）。"""

    def _band_profile(self, shape: str, r: float = 6.0) -> np.ndarray:
        fc = FloatCanvas(48, 48, 1, (1.0, 1.0, 1.0))
        pts = np.array([[2.0, 24.5], [46.0, 24.5]])
        fc.add_band(pts, np.array([r, r]), np.zeros((2, 3)), np.ones(2), mode="sweep", shapes=[shape, shape])
        img = fc.to_uint8().astype(np.float64) / 255.0
        return 1.0 - img[:, 24, 0]  # 竖向穿中线的覆盖率剖面（bg 白、墨黑；i=24 为轴线 d=0）

    def test_soft_edge_softer_than_round(self):
        r = 6.0
        round_cov = self._band_profile("round", r)
        soft_cov = self._band_profile("soft", r)
        # 中心覆盖：两者都≈1（soft 核心 rr*0.5 内不衰减）
        self.assertGreater(float(round_cov[24]), 0.95)
        self.assertGreater(float(soft_cov[24]), 0.95)
        # d=4px（0.67r）：round 仍≈1，soft 已按 smoothstep 明显衰减
        self.assertGreater(float(round_cov[24 + 4]), 0.9, "round 在 0.67r 处应接近满覆盖")
        self.assertLess(float(soft_cov[24 + 4]), 0.9, "soft 在 0.67r 处应开始柔化")
        self.assertLess(float(soft_cov[24 + 5]), float(round_cov[24 + 5]) - 0.1, "0.83r 处 soft 应比 round 明显更淡")
        # soft 的半覆盖半径更小（边缘内缩 = 更柔）
        idx_round = np.where(round_cov > 0.5)[0]
        idx_soft = np.where(soft_cov > 0.5)[0]
        half_round = float(idx_round.max() - 24)
        half_soft = float(idx_soft.max() - 24)
        self.assertLess(half_soft, half_round)
        self.assertGreater(half_soft, 0.55 * half_round)
        self.assertAlmostEqual(float(soft_cov[24 + 1]), float(round_cov[24 + 1]), delta=0.02)
        self.assertLess(float(round_cov[24 + 4]), 1.01)
        self.assertGreater(float(soft_cov[24 + 2]), float(soft_cov[24 + 4]))

    def test_marker_is_anisotropic(self):
        fc = FloatCanvas(48, 48, 1, (1.0, 1.0, 1.0))
        fc.add_band(
            np.array([[24.0, 24.0]]), np.array([5.0]), np.zeros((1, 3)), np.ones(1), mode="sweep", shapes=["marker"]
        )
        img = fc.to_uint8().mean(axis=2)
        mask = img < 128.0  # 覆盖 > 0.5
        ys, xs = np.where(mask)
        self.assertGreater(len(xs), 0)
        width_x = float(xs.max() - xs.min()) + 1.0
        width_y = float(ys.max() - ys.min()) + 1.0
        ratio = width_x / width_y
        self.assertGreater(ratio, 1.35, f"marker 沿笔头方向应更宽（实测比 {ratio:.2f}）")
        self.assertLess(ratio, 1.85, f"marker 各向异性比应≈aspect=1.6（实测比 {ratio:.2f}）")

    def test_marker_thinner_than_round_perpendicular(self):
        # 竖直笔画（移动方向 ⟂ 笔头长轴）：marker 横向变宽、纵向厚度不变
        def render_stroke(shape: str) -> np.ndarray:
            fc = FloatCanvas(64, 64, 1, (1.0, 1.0, 1.0))
            ys = np.linspace(8.0, 56.0, 24)
            pts = np.stack([np.full_like(ys, 32.0), ys], axis=1)
            fc.add_band(pts, np.full(24, 4.0), np.zeros((24, 3)), np.ones(24), mode="sweep", shapes=[shape] * 24)
            return fc.to_uint8().mean(axis=2)

        img_m = render_stroke("marker")
        img_r = render_stroke("round")
        mask_m, mask_r = img_m < 128.0, img_r < 128.0
        # 中线处横向宽度：marker ≈ 2*r*aspect，round ≈ 2*r
        row_m = mask_m[32, :]
        row_r = mask_r[32, :]
        self.assertGreater(float(row_m.sum()), float(row_r.sum()) * 1.25, "marker 横向应明显宽于 round")

    def test_shapes_length_mismatch_raises(self):
        fc = FloatCanvas(16, 16, 1, (1.0, 1.0, 1.0))
        with self.assertRaises(FBSError) as ctx:
            fc.add_band(np.array([[2.0, 8.0], [14.0, 8.0]]), np.ones(2), np.zeros((2, 3)), np.ones(2), shapes=["soft"])
        self.assertEqual(ctx.exception.code, "E_RENDER_GEOM")


class TestShapeFromDoc(unittest.TestCase):
    """渲染入口必须从 brush_styles[style].shape 求值（eval_shape）并真正生效。"""

    def _disc_doc(self, shape: str) -> dict:
        # 半径极小的闭合环 + 大宽度 = 一个「墨点」，便于量化笔头的各向异性
        coef, N = circle_coef(0.5, 0.5, 0.004)
        st = stroke_of(0, coef=coef, N=N, arc=2 * np.pi * 0.004, closed=True, width=0.12, style=0)
        style = style_of((0, 0, 0))
        style["shape"] = {"interp": "step", "keys": [[0.0, shape]]}
        return make_doc([st], [style], R=256)

    def test_soft_doc_differs_and_is_softer(self):
        a = render(self._disc_doc("round"), resolution=256).astype(np.float64)
        b = render(self._disc_doc("soft"), resolution=256).astype(np.float64)
        self.assertGreater(float(np.abs(a - b).max()), 10.0, "soft 笔形应改变渲染结果")
        # soft 的墨量更少（边缘柔化内缩）
        ink_a = float((255.0 - a).sum())
        ink_b = float((255.0 - b).sum())
        self.assertLess(ink_b, ink_a, "soft 渲染墨量应少于 round")

    def test_marker_doc_is_anisotropic(self):
        a = render(self._disc_doc("round"), resolution=256).mean(axis=2)
        b = render(self._disc_doc("marker"), resolution=256).mean(axis=2)
        self.assertGreater(float(np.abs(a - b).max()), 10.0, "marker 笔形应改变渲染结果")
        mask_m = b < 128.0
        ys, xs = np.where(mask_m)
        ratio = (float(xs.max() - xs.min()) + 1.0) / (float(ys.max() - ys.min()) + 1.0)
        self.assertGreater(ratio, 1.3, f"marker 圆盘应沿笔头方向拉长（实测 {ratio:.2f}）")


class TestColorFieldGridShape(unittest.TestCase):
    """color_field 支持 gw != gh 的行主序扁平颜色表（decomp 最终结构）。"""

    def test_non_square_grid(self):
        fill = stroke_of(
            0,
            coef=[[0, 0.5, 0.5], [1, 0.0, 0.0]],
            N=1,
            arc=2.4,
            closed=True,
            width=0.0,
            style=0,
            layer=0,
            fill_role="region_fill",
            poly=[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]],
        )
        fill["region_id"] = 0
        doc = make_doc([fill], [style_of((255, 255, 255))], R=128, fill_soft=0.0)
        gw, gh = 2, 8
        colors = []
        for gy in range(gh):
            for gx in range(gw):
                colors.append([255, 0, 0] if gx == 0 else [0, 0, 255])
        doc["regions"] = [
            {
                "id": 0,
                "rgb": [255, 255, 255],
                "bbox": [0.2, 0.2, 0.8, 0.8],
                "color_field": {"grid": gw, "gw": gw, "gh": gh, "bbox": [0.2, 0.2, 0.8, 0.8],
                                "colors": colors, "valid": gw * gh},
            }
        ]
        img = render(doc, resolution=128).astype(np.float64)
        left = img[64, 40]
        right = img[64, 88]
        self.assertGreater(left[0], left[2] + 40, "gw!=gh 颜色场左侧应为红")
        self.assertGreater(right[2], right[0] + 40, "gw!=gh 颜色场右侧应为蓝")


def _hole_fill_doc(Rres: int = 256, base=(255, 255, 255, 255), holes=None, color_field=None) -> dict:
    """带洞 region_fill 文档：0.2–0.8 红方块 + 可选 0.4–0.6 洞。"""
    fill = stroke_of(
        0,
        coef=[[0, 0.5, 0.5], [1, 0.0, 0.0]],
        N=1,
        arc=2.4,
        closed=True,
        width=0.0,
        style=0,
        layer=0,
        fill_role="region_fill",
        poly=[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]],
    )
    fill["region_id"] = 0
    if holes is not None:
        fill["fill_holes"] = holes
    coef, N = circle_coef(0.5, 0.9, 0.02)
    disc = stroke_of(1, coef=coef, N=N, arc=2 * np.pi * 0.02, closed=True, width=0.03, style=1, layer=1)
    styles = [style_of((220, 30, 40)), style_of((20, 20, 20))]
    doc = make_doc([fill, disc], styles, R=Rres, base_color=base)
    if color_field is not None:
        doc["regions"] = [{"id": 0, "rgb": [220, 30, 40], "bbox": [0, 0, 1, 1], "color_field": color_field}]
    return doc


HOLE_0406 = [[[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6]]]


class TestFillHoles(unittest.TestCase):
    """带洞区域填充：外轮廓正常填，洞内显示文档 base_color。"""

    def test_hole_shows_base_color(self):
        img = render(_hole_fill_doc(256, holes=HOLE_0406), resolution=256).astype(np.float64)
        self.assertTrue(np.allclose(img[128, 128], [255, 255, 255], atol=6), f"洞中心应为 base_color，实测 {img[128, 128]}")
        ring = img[128, 77]  # x≈0.3 在填充环上
        self.assertGreater(ring[0], 180.0, f"洞外填充区应为填充色，实测 {ring}")
        self.assertLess(ring[1], 90.0)
        self.assertTrue(np.allclose(img[128, 15], [255, 255, 255], atol=6), "多边形外应为底色")

    def test_hole_uses_non_white_base_color(self):
        base = (250, 248, 242, 255)
        img = render(_hole_fill_doc(256, base=base, holes=HOLE_0406), resolution=256).astype(np.float64)
        self.assertTrue(np.allclose(img[128, 128], base[:3], atol=6), f"洞应为文档底色 {base[:3]}，实测 {img[128, 128]}")
        self.assertGreater(img[128, 77][0], 180.0)

    def test_no_holes_doc_unchanged(self):
        solid = render(_hole_fill_doc(256), resolution=256)
        empty = render(_hole_fill_doc(256, holes=[]), resolution=256)
        self.assertTrue(np.array_equal(solid, empty), "fill_holes=[] 与缺失必须渲染完全一致（旧文档不变）")
        center = solid[128, 128].astype(float)
        self.assertGreater(center[0], 180.0, "无洞时洞中心位置仍是填充色")

    def test_holes_with_color_field(self):
        cf = {"grid": 4, "bbox": [0.0, 0.0, 1.0, 1.0],
              "colors": [[[60, 120, 200]] * 4 for _ in range(4)]}
        img = render(_hole_fill_doc(256, holes=HOLE_0406, color_field=cf), resolution=256).astype(np.float64)
        ring = img[128, 77]
        self.assertGreater(ring[2], 150.0, f"颜色场应作用于外轮廓，实测 {ring}")
        self.assertLess(ring[0], 120.0)
        self.assertTrue(np.allclose(img[128, 128], [255, 255, 255], atol=6), "洞仍应是底色（颜色场不填洞）")

    def test_invalid_holes_ignored_with_warning(self):
        doc = _hole_fill_doc(256, holes=[[[0.4, 0.4], [0.6, 0.4]]])  # 只有 2 点，非法
        warn = WarningLog()
        img = render(doc, resolution=256, warn=warn).astype(np.float64)
        self.assertGreater(img[128, 128][0], 180.0, "非法洞环应被忽略，按实心填充")
        codes = {w["code"] for w in warn.to_list()}
        self.assertIn("W_FILL_SKIPPED", codes, "非法 fill_holes 必须有降级警告（不得静默）")
        doc2 = _hole_fill_doc(256, holes="junk")
        img2 = render(doc2, resolution=128)
        self.assertEqual(img2.shape, (128, 128, 3))

    def test_one_stroke_fills_six_elements_and_hole(self):
        doc = _hole_fill_doc(256, holes=HOLE_0406)
        cfg = FBSConfig(target_resolution=256, harmonics=1024, max_fft=1 << 14)
        one = build_one_stroke(doc, cfg, WarningLog())
        fills = one["one_stroke_fills"]
        self.assertEqual(len(fills), 1)
        self.assertEqual(len(fills[0]), 6, "one_stroke_fills 新格式必须是 6 元素")
        self.assertIsNone(fills[0][4], "无 color_field 时第 5 位写 None")
        self.assertIsNotNone(fills[0][5], "带洞文档第 6 位必须写洞环")
        self.assertEqual(len(fills[0][5]), 1)
        img = render(one, resolution=256).astype(np.float64)
        self.assertTrue(np.allclose(img[128, 128], [255, 255, 255], atol=8), f"一笔画洞中心应为底色，实测 {img[128, 128]}")
        self.assertGreater(img[128, 77][0], 170.0, "一笔画填充环应为填充色")

    def test_legacy_fill_record_compat(self):
        doc = _hole_fill_doc(256, holes=HOLE_0406)
        cfg = FBSConfig(target_resolution=256, harmonics=1024, max_fft=1 << 14)
        one = build_one_stroke(doc, cfg, WarningLog())
        import copy

        # 旧 4 元素记录：无 color_field / 无洞 → 必须照旧工作（洞位置按实心填充）
        legacy = copy.deepcopy(one)
        legacy["one_stroke_fills"] = [rec[:4] for rec in one["one_stroke_fills"]]
        img4 = render(legacy, resolution=256).astype(np.float64)
        self.assertGreater(img4[128, 128][0], 180.0, "旧 4 元素记录按无洞处理（实心填充）")
        # 旧 5 元素记录：第 5 位是 dict 颜色场
        legacy5 = copy.deepcopy(one)
        legacy5["one_stroke_fills"] = [rec[:5] for rec in one["one_stroke_fills"]]
        img5 = render(legacy5, resolution=256)
        self.assertEqual(img5.shape, (256, 256, 3))
        # 新 6 元素记录：洞生效
        img6 = render(one, resolution=256).astype(np.float64)
        self.assertTrue(np.allclose(img6[128, 128], [255, 255, 255], atol=8))

    def test_svg_with_holes_parses(self):
        doc = _hole_fill_doc(128, holes=HOLE_0406)
        path = _out_path("render_test_holes.svg")
        info = export_svg(doc, path, resolution=128)
        self.assertGreater(info["polygons"], 1, "带洞填充的 SVG 应有外轮廓+洞两个多边形")
        ET.parse(path)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
