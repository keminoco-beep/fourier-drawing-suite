"""Stage1 混合分解（区域填充 + 中轴分支）的单元测试。

覆盖：element 契约字段/数据类型、层序（region_fill 全在 medial 之下）、
1px 与亚像素线保留率、已知宽度 AA 锥形笔画的宽度反演（A7）、
实心块 → 区域填充、尖角形状、平行双线（单峰判据）、退化点盘不消失、
纯色/透明边界、max_curves 安全阀优先保填充、确定性。
"""
from __future__ import annotations

import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

from fbs_core import coverage, decompose, preprocess  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.util import WarningLog  # noqa: E402


def make_img(rgba: np.ndarray) -> dict:
    h, w = rgba.shape[:2]
    a = rgba[..., 3].astype(np.float32) / 255.0
    rgb = (rgba[..., :3].astype(np.float32) / 255.0 * a[..., None] + (1.0 - a[..., None])).astype(np.float32)
    return {"rgba": rgba, "rgb": rgb, "alpha": a, "width": w, "height": h,
            "long_side": max(w, h), "path": "mem", "source_mode": "RGBA", "scale": 1.0, "meta": {}}


def blank(h: int = 128, w: int = 128, bg=(255, 255, 255)) -> np.ndarray:
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[..., :3] = bg
    arr[..., 3] = 255
    return arr


def run(rgba: np.ndarray, cfg: FBSConfig | None = None, klass: str = "") -> tuple[dict, dict, dict, WarningLog]:
    cfg = cfg or FBSConfig()
    img = make_img(rgba)
    if klass:
        img["klass"] = klass
        img["class_evidence"] = {"unique_colors": 4096}
    else:
        preprocess.classify(img, cfg)
    warn = WarningLog()
    analysis = coverage.analyze(img, cfg, warn)
    dec = decompose.decompose(img, analysis, cfg, warn)
    return img, analysis, dec, warn


def aa_stroke_mask(
    h: int,
    w: int,
    width_fn,
    center_fn=None,
    ss: int = 16,
    ink=(0, 0, 0),
    bg=(255, 255, 255),
) -> np.ndarray:
    """解析式构造带抗锯齿的水平走向笔画（列主序：每列一个覆盖率剖面）。

    width_fn(x) -> 全宽(px)；center_fn(x) -> 中心 y(px)。返回 RGBA8。
    """
    xx = np.arange(w, dtype=np.float64)
    yy = np.arange(h, dtype=np.float64)[:, None] + (np.arange(ss, dtype=np.float64)[None, :] + 0.5) / ss - 0.5
    wd = np.asarray([float(width_fn(float(x))) for x in xx], dtype=np.float64)  # noqa: 仅在 w 个列上调用一次
    cy = (np.asarray([float(center_fn(float(x))) for x in xx], dtype=np.float64)
          if center_fn is not None else np.full(w, h / 2.0))
    # (h,1,ss) 采样点 vs (1,w,1) 每列宽度 → 逐列覆盖率，全向量化
    inside = np.abs(yy[:, None, :] - cy[None, :, None]) <= (wd[None, :, None] / 2.0)
    cov = np.clip(inside.mean(axis=2), 0.0, 1.0)                      # (h,w)
    arr = blank(h, w, bg)
    for c in range(3):
        arr[:, :, c] = np.clip(cov * ink[c] + (1.0 - cov) * bg[c], 0, 255).astype(np.uint8)
    return arr


def stamp_mass(elements: list[dict], h: int, w: int, long_side: int) -> np.ndarray:
    """按「圆盘并集」语义把 medial_branch 栅格化成覆盖率场（像素坐标）。"""
    cov = np.zeros((h, w), dtype=np.float64)
    for e in elements:
        if e["fill_role"] != "medial_branch":
            continue
        pts = np.asarray(e["points"]) * long_side - 0.5    # world → 像素索引坐标（整数=像素中心）
        wd = np.asarray(e["widths"]) * long_side
        for i in range(len(pts)):
            r = float(wd[i]) / 2.0
            if r <= 0.0:
                continue
            x, y = float(pts[i, 0]), float(pts[i, 1])
            x0 = int(max(0, math.floor(x - r)))
            x1 = int(min(w - 1, math.ceil(x + r)))
            y0 = int(max(0, math.floor(y - r)))
            y1 = int(min(h - 1, math.ceil(y + r)))
            if x1 < x0 or y1 < y0:
                continue
            yy, xx = np.mgrid[y0:y1 + 1, x0:x1 + 1]
            d = np.sqrt((xx - x) ** 2 + (yy - y) ** 2)
            c = np.clip(np.minimum(0.5, d + r) - np.maximum(-0.5, d - r), 0.0, 1.0)
            cov[y0:y1 + 1, x0:x1 + 1] = np.maximum(cov[y0:y1 + 1, x0:x1 + 1], c)
    return cov


def polygon_area_world(poly: np.ndarray, long_side: int) -> float:
    p = np.asarray(poly, dtype=np.float64) * long_side
    x, y = p[:, 0], p[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


class TestElementContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        arr = blank(128, 128)
        arr[40:88, 30:70, :3] = 0          # 实心块 → fill
        arr[20, 10:118, :3] = 0            # 1px 线 → 中轴
        cls.img, cls.analysis, cls.dec, cls.warn = run(arr)

    def test_required_keys_and_types(self) -> None:
        els = self.dec["elements"]
        self.assertGreater(len(els), 0)
        for e in els:
            for key in ("points", "s", "closed", "fill_role", "widths", "colors",
                        "alphas", "shapes", "fill_poly", "region_id", "layer", "meta"):
                self.assertIn(key, e)
            pts = np.asarray(e["points"])
            self.assertEqual(pts.ndim, 2)
            self.assertEqual(pts.shape[1], 2)
            self.assertEqual(str(pts.dtype), "float64")
            m = len(pts)
            self.assertEqual(np.asarray(e["s"]).shape, (m,))
            self.assertEqual(np.asarray(e["widths"]).shape, (m,))
            self.assertEqual(np.asarray(e["colors"]).shape, (m, 3))
            self.assertEqual(np.asarray(e["alphas"]).shape, (m,))
            self.assertIn(e["fill_role"], ("medial_branch", "region_fill"))
            self.assertIsInstance(e["closed"], bool)
            self.assertIsInstance(e["layer"], int)
            self.assertIsInstance(e["region_id"], int)
            self.assertIsInstance(e["meta"], dict)
            # 世界坐标必须在画布内（像素中心约定）
            L = self.img["long_side"]
            self.assertTrue(np.all(pts >= -0.6 / L))
            self.assertTrue(np.all(pts <= (max(self.img["width"], self.img["height"]) + 0.6) / L))
            # s 单调 0..1
            s = np.asarray(e["s"])
            self.assertGreaterEqual(float(s[0]), 0.0)
            self.assertLessEqual(float(s[-1]), 1.0 + 1e-9)
            self.assertTrue(np.all(np.diff(s) >= -1e-12), "s 必须单调不减")
            # 颜色/alpha 范围
            col = np.asarray(e["colors"])
            self.assertTrue(np.all(col >= 0.0) and np.all(col <= 1.0))
            al = np.asarray(e["alphas"])
            self.assertTrue(np.all(al >= 0.0) and np.all(al <= 1.0))
            self.assertTrue(np.all(np.asarray(e["widths"]) >= 0.0))

    def test_fill_vs_medial_semantics(self) -> None:
        els = self.dec["elements"]
        fills = [e for e in els if e["fill_role"] == "region_fill"]
        medials = [e for e in els if e["fill_role"] == "medial_branch"]
        self.assertGreaterEqual(len(fills), 1, "实心块必须走 region_fill")
        self.assertGreaterEqual(len(medials), 1, "细线必须走 medial_branch")
        for e in fills:
            self.assertTrue(e["closed"])
            self.assertIsNotNone(e["fill_poly"])
            self.assertTrue(np.all(np.asarray(e["widths"]) == 0.0))
            self.assertGreaterEqual(len(np.asarray(e["fill_poly"])), 3)
        for e in medials:
            self.assertFalse(e["closed"])
            self.assertIsNone(e["fill_poly"])
            self.assertTrue(np.all(np.asarray(e["widths"]) >= 0.0))

    def test_layers_fills_below_branches(self) -> None:
        els = self.dec["elements"]
        fills = [e["layer"] for e in els if e["fill_role"] == "region_fill"]
        medials = [e["layer"] for e in els if e["fill_role"] == "medial_branch"]
        self.assertLess(max(fills), min(medials), "region_fill 必须全部在中轴分支之下（R7）")

    def test_regions_records(self) -> None:
        regions = self.dec["regions"]
        self.assertGreaterEqual(len(regions), 2)
        for r in regions:
            self.assertIsInstance(r["id"], int)
            self.assertEqual(len(r["rgb"]), 3)
            self.assertTrue(all(0 <= int(v) <= 255 for v in r["rgb"]))
            self.assertGreater(r["area_px"], 0)
            self.assertEqual(len(r["bbox"]), 4)
        els = self.dec["elements"]
        ids = [r["id"] for r in regions]
        self.assertEqual(len(ids), len(set(ids)), "region id 必须唯一")
        used = {e["region_id"] for e in els}
        self.assertTrue(used.issubset(set(ids)))

    def test_stats_and_method(self) -> None:
        self.assertEqual(self.dec["method"], "hybrid_medial_contour")
        st = self.dec["stats"]
        for key in ("region_count", "fill_count", "branch_count", "element_count"):
            self.assertIn(key, st)
        self.assertEqual(st["element_count"], len(self.dec["elements"]))
        self.assertIsInstance(self.dec.get("accelerators", {}), dict)


class TestLineRetention(unittest.TestCase):
    def _retention(self, rgba: np.ndarray) -> tuple[float, dict]:
        img, analysis, dec, _ = run(rgba)
        L = img["long_side"]
        cov = np.asarray(analysis["cov"], dtype=np.float64)
        mass = float(cov.sum())
        stamped = stamp_mass(dec["elements"], img["height"], img["width"], L)
        inter = float(np.minimum(stamped, cov).sum())
        return inter / max(mass, 1e-9), dec

    def test_one_pixel_line_retention(self) -> None:
        arr = blank(128, 128)
        arr[64, :, :3] = 0
        retention, dec = self._retention(arr)
        self.assertGreaterEqual(retention, 0.85, f"1px 线墨量保留率 {retention:.3f}")
        meds = [e for e in dec["elements"] if e["fill_role"] == "medial_branch"]
        self.assertEqual(len(meds), 1)
        width_px = float(np.median(meds[0]["widths"])) * 128
        self.assertGreater(width_px, 0.6)
        self.assertLess(width_px, 2.0)

    def test_subpixel_line_retention(self) -> None:
        """0.6px 亚像素线（峰值覆盖率 <0.5）必须被保留。"""
        arr = blank(128, 128)
        # 用 8x 超采样把 0.6px 线栅格化到两行像素
        ss = 16
        cov_row = np.zeros((128,), dtype=np.float64)
        yy = np.arange(128)[:, None] + (np.arange(ss)[None, :] + 0.5) / ss - 0.5
        cov_row = (np.abs(yy - 64.5) <= 0.3).mean(axis=1)
        for c in range(3):
            arr[:, :, c] = ((1.0 - cov_row[:, None]) * 255).astype(np.uint8)
        retention, dec = self._retention(arr)
        self.assertGreaterEqual(retention, 0.85, f"亚像素线墨量保留率 {retention:.3f}")
        self.assertGreaterEqual(len([e for e in dec["elements"] if e["fill_role"] == "medial_branch"]), 1)

    def test_broken_but_recoverable_line_NOT_lost(self) -> None:
        """逐点阈值切断的虚线：分支图桥接后应接成长笔画（不是一堆点盘）。"""
        arr = blank(128, 128)
        arr[64, :, :3] = 0
        arr[64, 40:44, :3] = 255      # 人为断口 4px（模拟 JPEG 低谷）
        _, analysis, dec, _ = run(arr)
        meds = [e for e in dec["elements"] if e["fill_role"] == "medial_branch"]
        disks = [e for e in dec["elements"] if e["meta"].get("source") == "point_disk"]
        total_len = sum(len(e["points"]) for e in meds)
        self.assertGreater(total_len, 100, "断线未被复原")
        self.assertLessEqual(len(disks), 2)


class TestRegionFill(unittest.TestCase):
    def test_solid_square_becomes_fill_at_correct_position(self) -> None:
        arr = blank(128, 128)
        arr[40:88, 30:70, :3] = (10, 20, 30)
        _, _, dec, _ = run(arr)
        fills = [e for e in dec["elements"] if e["fill_role"] == "region_fill"]
        self.assertEqual(len(fills), 1)
        poly = np.asarray(fills[0]["fill_poly"])
        L = 128
        area = polygon_area_world(poly, L)
        self.assertLess(abs(area - 40 * 48) / (40 * 48), 0.15, f"填充面积偏差过大: {area}")
        # 位置：质心在 (50, 64) 像素附近
        px = (poly * L).mean(axis=0)
        self.assertLess(abs(float(px[0]) - 50.0), 3.0)
        self.assertLess(abs(float(px[1]) - 64.0), 3.0)
        col = np.asarray(fills[0]["colors"])[0] * 255
        self.assertLess(float(np.abs(col - np.array([10, 20, 30])).max()), 12.0)
        self.assertTrue(fills[0]["closed"])

    def test_triangle_corners_preserved(self) -> None:
        """尖角形状：填充轮廓必须有尖角（顶点离质心明显远），不能被圆化。"""
        arr = blank(128, 128)
        yy, xx = np.mgrid[0:128, 0:128]
        tri = (yy >= 20) & (yy <= 100) & (np.abs(xx - 64) <= (yy - 20) * 0.5)
        arr[tri, :3] = 0
        _, _, dec, _ = run(arr)
        fills = [e for e in dec["elements"] if e["fill_role"] == "region_fill"]
        self.assertEqual(len(fills), 1)
        poly = np.asarray(fills[0]["fill_poly"]) * 128
        area = polygon_area_world(poly / 128.0, 128)
        self.assertLess(abs(area - tri.sum()) / max(int(tri.sum()), 1), 0.15)
        centroid = poly.mean(axis=0)
        d = np.linalg.norm(poly - centroid[None, :], axis=1)
        self.assertGreater(float(d.max()), 35.0, "尖角被圆化（顶点离质心太近）")

    def test_star_region(self) -> None:
        arr = blank(160, 160)
        import PIL.Image as Image
        import PIL.ImageDraw as ImageDraw

        pts = []
        for i in range(10):
            ang = -math.pi / 2 + i * math.pi / 5
            r = 60 if i % 2 == 0 else 27
            pts.append((80 + r * math.sin(ang), 80 - r * math.cos(ang)))
        im = Image.fromarray(arr)
        ImageDraw.Draw(im).polygon(pts, fill=(0, 0, 0, 255))
        arr = np.asarray(im).copy()
        _, _, dec, _ = run(arr)
        els = dec["elements"]
        self.assertTrue(any(e["fill_role"] == "region_fill" for e in els))
        fill = [e for e in els if e["fill_role"] == "region_fill"][0]
        poly = np.asarray(fill["fill_poly"]) * 160
        d = np.linalg.norm(poly - np.array([80.0, 80.0])[None, :], axis=1)
        self.assertGreater(float(d.max()), 50.0, "星形尖角丢失")


class TestWidthInversion(unittest.TestCase):
    def test_known_taper_width_recovered(self) -> None:
        """A7：已知 w(t) 的 AA 锥形笔画，覆盖率法向积分的相对误差中位数 <5%。"""
        h, w = 160, 200
        x0, x1 = 40, 160
        w0, w1 = 2.0, 10.0

        def width_fn(x: float) -> float:
            if x < x0 or x >= x1:
                return 0.0
            u = (x - x0) / float(x1 - x0)
            return w0 + (w1 - w0) * u

        arr = aa_stroke_mask(h, w, width_fn)
        img, _, dec, _ = run(arr)
        L = img["long_side"]
        meds = [e for e in dec["elements"] if e["fill_role"] == "medial_branch"]
        self.assertGreaterEqual(len(meds), 1)
        e = max(meds, key=lambda el: len(el["points"]))
        pts = np.asarray(e["points"]) * L
        widths = np.asarray(e["widths"]) * L
        order = np.argsort(pts[:, 0])
        pts, widths = pts[order], widths[order]
        errs = []
        for k in range(len(pts)):
            x = float(pts[k, 0])
            if x < x0 + 3 or x > x1 - 3:
                continue
            true_w = width_fn(x)
            errs.append(abs(float(widths[k]) - true_w) / true_w)
        self.assertGreater(len(errs), 20)
        median_err = float(np.median(errs))
        self.assertLess(median_err, 0.05, f"宽度反演中位相对误差 {median_err:.3f}")
        self.assertLess(float(np.percentile(errs, 90)), 0.10)


class TestAxisAndBranches(unittest.TestCase):
    def test_parallel_double_lines_unimodal(self) -> None:
        """平行双线：每条分支中线必须落在各自线中心，不能掉到两线中间（单峰判据）。"""
        arr = blank(128, 128)
        arr[40:44, :, :3] = 0
        arr[56:60, :, :3] = 0
        img, _, dec, _ = run(arr)
        L = img["long_side"]
        meds = [e for e in dec["elements"] if e["fill_role"] == "medial_branch"]
        self.assertEqual(len(meds), 2, f"平行双线应切成 2 条，实际 {len(meds)}")
        centers = sorted(float(np.mean(np.asarray(e["points"])[:, 1])) * 128 for e in meds)
        self.assertLess(abs(centers[0] - 42.0), 1.0)
        self.assertLess(abs(centers[1] - 58.0), 1.0)
        for e in meds:
            wpx = float(np.median(e["widths"])) * 128
            self.assertTrue(2.5 < wpx < 5.5, f"双线宽度 {wpx:.2f}")

    def test_taper_single_branch(self) -> None:
        """平滑锥形笔画不应被补支/剪枝切碎（尖角补支只对块状形状启用）。"""
        arr = aa_stroke_mask(96, 160, lambda x: 2.0 + 8.0 * x / 160.0)
        _, _, dec, _ = run(arr)
        meds = [e for e in dec["elements"] if e["fill_role"] == "medial_branch"]
        self.assertLessEqual(len(meds), 3)

    def test_degenerate_point_disk_present(self) -> None:
        """实心小圆盘（骨架退化）必须单独输出点盘，不能消失。"""
        h = w = 64
        yy, xx = np.mgrid[0:h, 0:w]
        arr = blank(h, w)
        circle = ((yy - 32) ** 2 + (xx - 32) ** 2) <= 6.25   # r=2.5
        arr[circle, :3] = 0
        cfg = FBSConfig(hybrid="off")
        _, _, dec, warn = run(arr, cfg)
        els = dec["elements"]
        self.assertGreaterEqual(len(els), 1, "退化圆盘整块消失了")
        disks = [e for e in els if e["meta"].get("source") == "point_disk"]
        self.assertGreaterEqual(len(disks), 1)
        disk = disks[0]
        self.assertEqual(len(disk["points"]), 2)
        self.assertGreater(float(np.max(disk["widths"])), 0.0)
        self.assertIn("point_disk", str(disk["meta"]["source"]))

    def test_alpha_半透明笔画(self) -> None:
        arr = np.zeros((64, 64, 4), dtype=np.uint8)
        arr[32, :, 3] = 128                    # 半透明黑线，透明背景
        img, analysis, dec, _ = run(arr)
        meds = [e for e in dec["elements"] if e["fill_role"] == "medial_branch"]
        if meds:
            a = float(np.median(meds[0]["alphas"]))
            self.assertTrue(0.3 < a <= 1.0, f"alpha 反演 {a:.2f}")
        self.assertGreaterEqual(len(meds), 1, "半透明细线丢失")


class TestEdgeCases(unittest.TestCase):
    def test_pure_color_image_produces_no_elements(self) -> None:
        arr = blank(64, 64)
        _, analysis, dec, warn = run(arr)
        self.assertEqual(len(dec["elements"]), 0)
        self.assertEqual(len(dec["regions"]), 0)
        self.assertTrue(any(w["code"] == "W_SOLID_COLOR" for w in warn.to_list()))

    def test_max_curves_keeps_fills(self) -> None:
        arr = blank(128, 128)
        arr[40:88, 30:70, :3] = 0
        arr[20, 10:118, :3] = 0
        arr[100, 10:118, :3] = 0
        cfg = FBSConfig(max_curves=2)
        _, _, dec, warn = run(arr, cfg)
        els = dec["elements"]
        self.assertLessEqual(len(els), 2)
        self.assertTrue(any(e["fill_role"] == "region_fill" for e in els),
                        "max_curves 截断把 region_fill 整批砍掉了")
        self.assertTrue(any(w["code"] == "W_MAX_CURVES" for w in warn.to_list()))

    def test_deterministic(self) -> None:
        arr = blank(96, 96)
        arr[30:60, 20:40, :3] = 0
        arr[70, 5:90, :3] = 0
        _, _, d1, _ = run(arr)
        _, _, d2, _ = run(arr)
        e1, e2 = d1["elements"], d2["elements"]
        self.assertEqual(len(e1), len(e2))
        for a, b in zip(e1, e2):
            self.assertTrue(np.allclose(a["points"], b["points"]))
            self.assertTrue(np.allclose(a["widths"], b["widths"]))

    def test_hybrid_off_still_covers(self) -> None:
        """关闭 hybrid 时块状区域走中轴，但必须有笔画覆盖（不能整块消失）。"""
        arr = blank(96, 96)
        arr[30:66, 30:66, :3] = 0
        cfg = FBSConfig(hybrid="off")
        img, analysis, dec, _ = run(arr, cfg)
        self.assertGreaterEqual(len(dec["elements"]), 1)
        cov = np.asarray(analysis["cov"])
        stamped = stamp_mass(dec["elements"], 96, 96, img["long_side"])
        inter = float(np.minimum(stamped, cov).sum())
        self.assertGreater(inter / max(float(cov.sum()), 1e-9), 0.8)




class TestHoleAwareFill(unittest.TestCase):
    """带洞区域应走 region_fill + fill_holes（不再回退中轴丢失填色）。"""

    @staticmethod
    def _point_in_poly(poly, x, y):
        pts = np.asarray(poly, dtype=np.float64)
        inside = False
        n = len(pts)
        j = n - 1
        for i in range(n):
            xi, yi = pts[i]
            xj, yj = pts[j]
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside

    def test_ring_region_fill_carries_holes(self):
        h = w = 400
        arr = blank(h, w)
        yy, xx = np.mgrid[0:h, 0:w]
        d = np.hypot(yy - h / 2.0, xx - w / 2.0)
        ring = (d >= 20) & (d <= 40)
        arr[ring, :3] = (30, 30, 30)
        _img, _an, dec, _w = run(arr)  # lineart：环状实心结构应强制走带洞填充
        fills = [e for e in dec["elements"] if e["fill_role"] == "region_fill"]
        self.assertTrue(fills, "环状区域没有生成 region_fill")
        holes = [hh for e in fills for hh in (e.get("fill_holes") or [])]
        self.assertGreaterEqual(len(holes), 1, "region_fill 没带洞几何")
        cx, cy = 0.5, 0.5
        self.assertTrue(
            any(self._point_in_poly(hh, cx, cy) for hh in holes),
            "洞几何未覆盖区域中心（洞位置不对）",
        )

    def test_short_branch_ink_mass_metric(self):
        """短分支墨量判据：长度×中位全宽；真实短笔触应 > 噪声阈值，1px 噪声应接近 0。"""
        dt = np.zeros((20, 20), dtype=np.float64)
        dt[5:10, 5:15] = 1.5  # 一条 5px 宽、10px 长的墨迹
        path_ink = [(y, x) for y in range(7, 8) for x in range(5, 15)]
        path_noise = [(0, 0), (0, 1)]  # 背景上的亚像素噪声：DT≈0 → 全宽≈1px
        m_ink = decompose._branch_ink_mass(path_ink, dt)
        m_noise = decompose._branch_ink_mass(path_noise, dt)
        self.assertGreater(m_ink, 2.5, "真实短笔触墨量被低估")
        self.assertLess(m_noise, 2.5, "噪声短分支墨量不应达到保留阈值")

    def test_small_ink_blob_not_lost(self):
        """小面积墨块不能被静默丢掉（进入填充或中轴，至少有一个 element 覆盖）。"""
        arr = blank(96, 96)
        arr[40:47, 40:47, :3] = (0, 0, 0)
        _img, _an, dec, _w = run(arr, cfg=FBSConfig(denoise_sigma=0.0))
        self.assertTrue(dec["elements"], "小墨块被静默丢弃")
        self.assertGreaterEqual(int(dec["stats"].get("region_count", 0)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
