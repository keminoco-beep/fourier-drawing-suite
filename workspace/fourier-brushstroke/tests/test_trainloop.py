"""训练循环辅助函数与连续动作 RL 的单元测试。"""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from fbs_core import stroke_rl_render, trainloop  # noqa: E402


class TestTrainLoopHelpers(unittest.TestCase):
    def test_psnr_ssim_identical(self):
        rng = np.random.default_rng(1)
        a = rng.random((32, 32, 3))
        p, s = trainloop._psnr_ssim(a, a)
        self.assertGreater(p, 100.0)
        self.assertGreater(s, 0.99)

    def test_jitter_structure_preserves_shape_and_clips(self):
        rgb = np.random.default_rng(2).random((16, 16, 3))
        structure = {
            "W": 16, "H": 16, "L": 16, "rgb": rgb, "counts": {"flat": 1, "line_thick": 1},
            "structures": [
                {"kind": "region", "class": "flat", "orientation": 0.3,
                 "color": np.array([0.5, 0.5, 0.5]), "ys": np.array([1]), "xs": np.array([1]),
                 "area_px": 1, "bbox_px": (0, 0, 2, 2), "poly_world": np.zeros((3, 2))},
                {"kind": "line", "class": "line_thick",
                 "points_world": np.array([[0.1, 0.1], [0.2, 0.2]]),
                 "widths_world": np.array([0.01, 0.01]), "color": np.array([0.1, 0.1, 0.1]),
                 "length_px": 10.0, "bbox_px": (0, 0, 2, 2)},
            ],
        }
        out = trainloop._jitter_structure(structure, np.random.default_rng(3))
        self.assertEqual(len(out["structures"]), 2)
        self.assertNotEqual(out["structures"][0]["orientation"], 0.3)
        for st in out["structures"]:
            c = np.asarray(st["color"])
            self.assertTrue(np.all(c >= 0.0) and np.all(c <= 1.0))

    def test_pick_seg_prefers_high_J(self):
        state = {"seg_stats": {
            "palette": {"n": 20, "J_sum": 10.0},
            "contour": {"n": 60, "J_sum": 900.0},
        }}
        self.assertEqual(trainloop._pick_seg(state), "contour")
        state2 = {"seg_stats": {"palette": {"n": 1, "J_sum": 0.0}}}
        self.assertIn(trainloop._pick_seg(state2), ("palette", "contour"))


class TestStrokeRLSearch(unittest.TestCase):
    def test_search_returns_actions_and_benefits(self):
        h = w = 40
        target = np.zeros((h, w, 3))
        target[:] = [0.3, 0.4, 0.6]
        strokes = []
        for k in range(6):
            y = 5.0 + k * 5.0
            pts = np.stack([np.linspace(3, 37, 5), np.full(5, y)], axis=1)
            strokes.append({
                "class": "flat", "preset": "dry_flat", "shape": "round",
                "points_px": pts, "widths_px": np.full(5, 4.0),
                "color": np.array([0.3, 0.4, 0.6]), "alpha": 1.0,
            })
        res = stroke_rl_render.run_stroke_rl_search(
            target, strokes, log_path=None, working_res=40, S=1,
            rng=np.random.default_rng(7), min_improve=1e-3, n_candidates=1,
        )
        st = res["stats"]
        for key in ("strokes_accepted", "objective_J", "segments_accepted", "benefit", "classes"):
            self.assertIn(key, st)
        self.assertGreater(st["strokes_accepted"], 0)
        for s_ in res["accepted"]:
            self.assertIn("action", s_)
            self.assertIn("benefit", s_)


if __name__ == "__main__":
    unittest.main(verbosity=2)
