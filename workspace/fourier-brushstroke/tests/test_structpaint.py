"""结构感知绘画（structpaint / structdoc）单元测试。

覆盖：结构分类合法性、区域 mask 几何、按结构生成笔触（平涂/明暗/细节/线）、
UCB 策略选择、笔触 → .fbs 文档并可渲染。
"""

import os
import sys
import unittest

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from fbs_core import structdoc, structpaint  # noqa: E402
from fbs_core import brushlib  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.shapelib import VALID_SHAPES  # noqa: E402
from fbs_core.util import WarningLog  # noqa: E402


class TestStructurePaint(unittest.TestCase):
    def _fake_image(self) -> dict:
        h, w = 96, 96
        rgb = np.zeros((h, w, 3))
        rgb[:, : w // 2] = [0.2, 0.3, 0.8]
        rgb[:, w // 2:] = [0.9, 0.8, 0.7]
        alpha = np.ones((h, w))
        rgba = np.concatenate([(rgb * 255).astype(np.uint8), np.full((h, w, 1), 255, np.uint8)], axis=2)
        return {
            "rgb": rgb, "alpha": alpha, "rgba": rgba, "width": w, "height": h,
            "long_side": max(w, h), "path": "mem", "source_mode": "RGBA", "scale": 1.0, "meta": {},
        }

    def test_ucb_choose_prefers_best_reward(self):
        learned = {
            "flat": {
                "a|w1|l1": {"mean_psnr": 10, "mean_ssim": 0.5, "reward": 0.2, "n": 3},
                "b|w1|l1": {"mean_psnr": 20, "mean_ssim": 0.8, "reward": 0.9, "n": 3},
            }
        }
        self.assertEqual(structpaint.ucb_choose(learned, "flat"), "b|w1|l1")
        pol = structpaint.policies_from_learned({"flat": learned["flat"]})
        self.assertEqual(pol["flat"]["preset"], "b")

    def test_policy_candidates_are_valid_brushes(self):
        for klass, cands in structpaint.POLICY_CANDIDATES.items():
            self.assertGreater(len(cands), 0, klass)
            for pol in cands:
                self.assertIn(pol["preset"], brushlib.BRUSHES)
                self.assertGreater(pol["width_scale"], 0.0)

    def test_region_mask_geometry(self):
        poly = np.array([[0.1, 0.1], [0.4, 0.1], [0.4, 0.4], [0.1, 0.4]])
        geom = structpaint._region_mask_from_poly(poly, 100, 100)
        self.assertIsNotNone(geom)
        self.assertGreater(int(geom["mask"].sum()), 500)
        x0, y0, x1, y1 = geom["bbox"]
        self.assertAlmostEqual(x0, 10, delta=2)
        self.assertAlmostEqual(y1, 40, delta=2)

    def _structure(self) -> dict:
        img = self._fake_image()
        h, w = img["height"], img["width"]
        # 一块平涂 + 一条细线
        region_mask = np.zeros((h, w), dtype=bool)
        region_mask[20:70, 15:60] = True
        ys, xs = np.nonzero(region_mask)
        geom = {"bbox": (15, 20, 60, 70), "mask": region_mask[20:70, 15:60]}
        region = {
            "kind": "region", "class": "flat", "rid": 0,
            "poly_world": np.array([[0.1, 0.15], [0.45, 0.15], [0.45, 0.55], [0.1, 0.55]]),
            "ys": ys, "xs": xs, "area_px": int(len(ys)), "orientation": 0.2,
            "color": np.array([0.3, 0.4, 0.7]), "alpha": 1.0,
            "bbox_px": (15, 20, 60, 70), "geom": geom,
        }
        line = {
            "kind": "line", "class": "line_thick",
            "points_world": np.stack([np.linspace(0.2, 0.7, 6), np.linspace(0.2, 0.6, 6)], axis=1),
            "widths_world": np.full(6, 0.004), "colors": np.tile(np.array([0.1, 0.12, 0.2]), (6, 1)),
            "alphas": np.ones(6), "width_px": 4.0, "length_px": 60.0,
            "color": np.array([0.1, 0.12, 0.2]), "dark": True, "bbox_px": (20, 20, 72, 62),
        }
        return {"structures": [region, line], "counts": {"flat": 1, "line_thick": 1},
                "W": w, "H": h, "L": max(h, w), "rgb": img["rgb"]}

    def test_paint_structure_emits_structured_strokes(self):
        structure = self._structure()
        policies = {
            "flat": {"preset": "dry_flat", "width_scale": 1.0, "length_scale": 1.0, "alpha_scale": 1.0},
            "shade": {"preset": "basic_airbrush", "width_scale": 1.0, "length_scale": 1.0, "alpha_scale": 1.0},
            "detail": {"preset": "marker_chisel", "width_scale": 1.0, "length_scale": 1.0, "alpha_scale": 1.0},
            "line_fine": {"preset": "ink_fineliner", "width_scale": 1.0, "length_scale": 1.0, "alpha_scale": 1.0},
            "line_thick": {"preset": "ink_gpen", "width_scale": 1.0, "length_scale": 1.0, "alpha_scale": 1.0},
        }
        strokes = structpaint.paint_structure(structure, policies)
        self.assertGreater(len(strokes), 3)
        self.assertTrue(any(s["class"] == "line_thick" for s in strokes))
        for st in strokes:
            self.assertIn(st["preset"], brushlib.BRUSHES)
            self.assertGreater(len(st["points_px"]), 1)
            self.assertTrue(np.all(np.asarray(st["widths_px"]) > 0))
            self.assertGreater(st["alpha"], 0.0)

    def test_strokes_build_doc_and_render(self):
        structure = self._structure()
        policies = {
            "flat": {"preset": "dry_flat", "width_scale": 1.0, "length_scale": 1.0, "alpha_scale": 1.0},
            "shade": {"preset": "basic_airbrush", "width_scale": 1.0, "length_scale": 1.0, "alpha_scale": 1.0},
            "detail": {"preset": "marker_chisel", "width_scale": 1.0, "length_scale": 1.0, "alpha_scale": 1.0},
            "line_fine": {"preset": "ink_fineliner", "width_scale": 1.0, "length_scale": 1.0, "alpha_scale": 1.0},
            "line_thick": {"preset": "ink_gpen", "width_scale": 1.0, "length_scale": 1.0, "alpha_scale": 1.0},
        }
        strokes = structpaint.paint_structure(structure, policies)
        for st in strokes:
            st["shape"] = brushlib.BRUSHES[st["preset"]]["shape"]
        img = self._fake_image()
        # 写一个临时输入图（build_doc 需要 source 路径算 sha256）
        tmpdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "out", "audit")
        os.makedirs(tmpdir, exist_ok=True)
        path = os.path.join(tmpdir, "_test_structpaint.png")
        try:
            Image.fromarray(np.asarray(img["rgba"])).save(path)
            cfg = FBSConfig(target_resolution=96)
            doc = structdoc.build_doc_from_strokes(strokes, img, path, cfg, WarningLog())
            from fbs_core import render as render_mod
            rgb = render_mod.render(doc, resolution=96, supersample=2, mode="sweep", warn=WarningLog())
            self.assertEqual(rgb.shape[0], 96)
            self.assertEqual(sum(1 for s in doc["strokes"] if s.get("fill_role") == "region_fill"), 0)
            self.assertGreater(len(doc["strokes"]), 0)
            self.assertGreater(float(np.mean(rgb.min(axis=2) < 250)), 0.05, "文档渲染应落墨")
        finally:
            try:
                os.remove(path)
            except OSError:  # noqa: 清理失败不影响测试语义
                pass


class TestLineMergeAndStrokeRL(unittest.TestCase):
    def test_merge_line_chains_joins_collinear_only(self):
        def line(x0, x1, y, cls="line_thick"):
            pts = np.stack([np.linspace(x0, x1, 4) / 1000.0, np.full(4, y / 1000.0)], axis=1)
            return {
                "kind": "line", "class": cls, "points_world": pts,
                "widths_world": np.full(4, 0.003), "colors": np.tile([0.1, 0.1, 0.2], (4, 1)),
                "alphas": np.ones(4), "width_px": 3.0, "length_px": (x1 - x0),
                "color": np.array([0.1, 0.1, 0.2]), "dark": True, "bbox_px": (0, 0, 1, 1),
            }

        near = [line(100, 140, 100), line(141, 180, 100)]  # 端点相邻、方向一致
        merged = structpaint.merge_line_chains(near, L=1000.0)
        self.assertEqual(len(merged), 1, "共线相邻的两段应合并成一条")
        far = [line(100, 140, 100), line(400, 440, 100)]  # 相距很远
        self.assertEqual(len(structpaint.merge_line_chains(far, L=1000.0)), 2)
        # 垂直方向不应合并
        def line_y(y0, y1, x):
            pts = np.stack([np.full(4, x / 1000.0), np.linspace(y0, y1, 4) / 1000.0], axis=1)
            return {
                "kind": "line", "class": "line_fine", "points_world": pts,
                "widths_world": np.full(4, 0.002), "colors": np.tile([0.1, 0.1, 0.2], (4, 1)),
                "alphas": np.ones(4), "width_px": 2.0, "length_px": (y1 - y0),
                "color": np.array([0.1, 0.1, 0.2]), "dark": True, "bbox_px": (0, 0, 1, 1),
            }

        cross = [line(100, 140, 100), line_y(141, 180, 140)]
        self.assertEqual(len(structpaint.merge_line_chains(cross, L=1000.0)), 2, "近似垂直的线不应合并")

    def test_stroke_rl_accepts_and_logs(self):
        from fbs_core import stroke_rl

        h = w = 64
        target = np.zeros((h, w, 3))
        target[:] = [0.2, 0.3, 0.8]
        strokes = []
        for k in range(12):
            y = 8 + k * 4
            pts = np.stack([np.linspace(4, 60, 6), np.full(6, float(y))], axis=1)
            strokes.append(
                {"class": "flat" if k % 3 else "detail", "preset": "dry_flat",
                 "points_px": pts, "widths_px": np.full(6, 6.0),
                 "color": np.array([0.2, 0.3, 0.8]), "alpha": 1.0}
            )
        tmpdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "out", "audit")
        os.makedirs(tmpdir, exist_ok=True)
        log = os.path.join(tmpdir, "_test_stroke_rl.json")
        try:
            res = stroke_rl.run_stroke_rl(target, strokes, log_path=log, rng=np.random.default_rng(3))
            self.assertGreater(len(res["accepted"]), 0)
            self.assertLessEqual(len(res["accepted"]), len(strokes))
            st = res["stats"]
            for key in ("strokes_total", "strokes_accepted", "accept_rate", "canvas_psnr_db", "arms", "curve"):
                self.assertIn(key, st)
            # 白画布基线 PSNR（目标为均匀色）应显著低于训练后
            base_mse = float(np.mean((np.ones_like(target) - target) ** 2))
            base_psnr = 10.0 * np.log10(1.0 / max(base_mse, 1e-12))
            self.assertGreater(st["canvas_psnr_db"], base_psnr + 3.0, "RL 后画布应明显优于白画布")
            self.assertGreater(st["strokes_accepted"], 0)
            self.assertTrue(os.path.exists(log))
            self.assertTrue(os.path.exists(os.path.splitext(log)[0] + ".md"))
        finally:
            for p in (log, os.path.splitext(log)[0] + ".md"):
                try:
                    os.remove(p)
                except OSError:  # noqa: 清理失败不影响测试语义
                    pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
