"""自适应梯度面片（连续色调通道）单元测试。

覆盖：重建精度（平滑渐变/硬边）、预算与细分、tile 覆盖性、序列化结构、
build_from_image 集成（mesh 模式不走调色板分解、不被 max_curves 截断）。
"""

import os
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from fbs_core import mesh  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402


def _psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a - b) ** 2))
    return 10.0 * np.log10(1.0 / max(mse, 1e-12))


class TestAdaptiveGradientMesh(unittest.TestCase):
    def _gradient(self, h=192, w=192) -> np.ndarray:
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
        r = xx / w
        g = yy / h
        b = 0.5 + 0.4 * np.sin(3.0 * xx / w) * np.cos(3.0 * yy / h)
        return np.clip(np.stack([r, g, b], axis=-1), 0.0, 1.0)

    def _edge(self, h=192, w=192) -> np.ndarray:
        yy, xx = np.mgrid[0:h, 0:w]
        img = np.zeros((h, w, 3))
        img[..., 0] = np.where(xx + yy * 0.3 < w * 0.55, 0.08, 0.95)
        img[..., 1] = np.where(xx + yy * 0.3 < w * 0.55, 0.10, 0.85)
        img[..., 2] = np.where(xx + yy * 0.3 < w * 0.55, 0.20, 0.70)
        return img

    def test_smooth_gradient_reconstructs_accurately(self):
        img = self._gradient()
        cfg = FBSConfig(mesh=True, mesh_tol=0.01, mesh_min_side=8, mesh_max_cells=4000)
        dec = mesh.decompose_mesh(img, cfg, None, long_side=img.shape[1])
        # 直接用叶子重建
        leaves = []
        for el in dec["elements"]:
            poly = np.asarray(el["fill_poly"])
            x0 = int(round(poly[:, 0].min() * img.shape[1]))
            x1 = int(round(poly[:, 0].max() * img.shape[1]))
            y0 = int(round(poly[:, 1].min() * img.shape[0]))
            y1 = int(round(poly[:, 1].max() * img.shape[0]))
            rid = el["region_id"]
            field = dec["regions"][rid]["color_field"]
            cols = np.asarray(field["colors"], dtype=np.float64).reshape(2, 2, 3) / 255.0
            leaves.append((y0, x0, y1, x1, cols, el["meta"]["cell_rms"]))
        rec = mesh.reconstruct(img.shape, leaves)
        self.assertGreater(_psnr(img, rec), 40.0, "平滑渐变的重建精度应很高")
        self.assertLessEqual(dec["stats"]["cells"], 4000)

    def test_hard_edge_subdivides_and_reconstructs(self):
        img = self._edge()
        cfg = FBSConfig(mesh=True, mesh_tol=0.03, mesh_min_side=3, mesh_max_cells=6000)
        dec = mesh.decompose_mesh(img, cfg, None, long_side=img.shape[1])
        stats = dec["stats"]
        self.assertGreater(stats["cells"], 64, "硬边应触发细分")
        # 叶子面积应恰好覆盖整幅图（tile 无重叠/无空洞）
        total = 0
        for el in dec["elements"]:
            poly = np.asarray(el["fill_poly"])
            total += (poly[:, 0].max() - poly[:, 0].min()) * (poly[:, 1].max() - poly[:, 1].min())
        self.assertAlmostEqual(total, 1.0, places=6, msg="面片面积之和应为 1（完整覆盖画布）")
        # 每个 region 的 2×2 颜色场合法
        for reg in dec["regions"]:
            cf = reg["color_field"]
            self.assertEqual(cf["grid"], 2)
            self.assertEqual(len(cf["colors"]), 4)
            self.assertEqual(cf["bbox"], reg["bbox"])

    def test_budget_is_respected_and_reported(self):
        img = self._edge()
        cfg = FBSConfig(mesh=True, mesh_tol=0.005, mesh_min_side=2, mesh_max_cells=300)
        dec = mesh.decompose_mesh(img, cfg, None, long_side=img.shape[1])
        self.assertLessEqual(dec["stats"]["cells"], 300 + 4)
        self.assertTrue(dec["stats"]["budget_hit"], "低预算应报告 budget_hit")

    def test_photo_experimental_routes_to_mesh_in_document(self):
        """--allow-photo-experimental 自动走 mesh；输出 method/填充数一致且不被 max_curves 截断。"""
        from fbs_core import document
        from fbs_core.util import WarningLog

        rng = np.random.default_rng(11)
        h = w = 160
        yy, xx = np.mgrid[0:h, 0:w] / float(max(h, w))
        base = np.zeros((h, w, 3))
        for c in range(3):
            base[..., c] = 0.5 + 0.4 * np.sin(6 * xx + 2 * yy + c)
        photo = np.clip(base + rng.normal(0, 0.02, base.shape) + rng.normal(0, 3.0 / 255.0, base.shape), 0, 1)
        # 注意：Windows 沙箱下 tempfile 目录的 ACL 可能拒绝写入；直接写到 workspace 的 audit 目录。
        tmp_root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "out", "audit")
        os.makedirs(tmp_root, exist_ok=True)
        path = os.path.join(tmp_root, "_test_mesh_photo.png")
        edge_path = os.path.join(tmp_root, "_test_mesh_edge.png")
        try:
            Image.fromarray((photo * 255).round().astype(np.uint8)).save(path)
            Image.fromarray((self._edge(96, 96) * 255).round().astype(np.uint8)).save(edge_path)
            # 照片实验通道现在默认走**纯笔触绘画**；--mesh 是显式旧实验（对任意图强制）。
            cfg = FBSConfig(
                target_resolution=160, allow_photo_experimental=True,
                brush_strokes=120, brush_work_res=96, max_curves=8,
            )
            doc, info = document.build_from_image(path, cfg, WarningLog())
            cfg_mesh = FBSConfig(
                target_resolution=96, mesh=True,
                mesh_tol=0.05, mesh_min_side=4, mesh_max_cells=1500, max_curves=8,
            )
            doc_mesh, info_mesh = document.build_from_image(edge_path, cfg_mesh, WarningLog())
        finally:
            for f in (path, edge_path):
                try:
                    os.remove(f)
                except OSError:  # noqa: 沙箱 ACL 下清理失败不影响测试语义（文件在 audit 临时名）
                    pass
        # 1) 照片实验通道 → 纯笔触绘画（无填充、无区域）
        self.assertEqual(doc["decomposition"]["method"], "painterly_brush_strokes")
        self.assertEqual(sum(1 for s in doc["strokes"] if s.get("fill_role") == "region_fill"), 0)
        self.assertEqual(len(doc["regions"]), 0)
        self.assertGreater(doc["stats"]["stroke_count"], 20)
        # 2) 显式 --mesh → 自适应梯度面片（旧实验）
        self.assertEqual(doc_mesh["decomposition"]["method"], "adaptive_gradient_mesh")
        self.assertEqual(doc_mesh["decomposition"]["params"]["fill_soft_px"], 0.0)
        n_fill = sum(1 for s in doc_mesh["strokes"] if s.get("fill_role") == "region_fill")
        self.assertEqual(n_fill, doc_mesh["stats"]["stroke_count"])
        self.assertEqual(n_fill, len(doc_mesh["regions"]))
        self.assertGreater(n_fill, int(cfg_mesh.max_curves), "mesh 面片不应被 max_curves 截断")


if __name__ == "__main__":
    unittest.main(verbosity=2)
