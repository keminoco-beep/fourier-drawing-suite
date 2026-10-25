"""测绘（survey）与真渲染器 RL 的单元测试。"""

import json
import os
import sys
import unittest

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from fbs_core import stroke_rl_render, survey  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.util import WarningLog  # noqa: E402


class TestSurvey(unittest.TestCase):
    def test_survey_measures_and_reports(self):
        h = w = 120
        rgb = np.zeros((h, w, 3), dtype=np.uint8)
        rgb[:, :] = [250, 250, 250]
        rgb[20:70, 20:60] = [60, 90, 180]      # 一个色块
        rgb[80:84, 10:110] = [20, 25, 40]      # 一条深色线
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "out", "audit", "_test_survey.png")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            Image.fromarray(rgb).save(path)
            data = survey.survey_image(path, FBSConfig(target_resolution=120), WarningLog())
            for key in ("image", "class", "lines", "coverage", "regions", "resolution"):
                self.assertIn(key, data)
            self.assertGreaterEqual(data["lines"]["line_count"], 0)
            self.assertGreaterEqual(data["regions"]["region_count"], 0)
            self.assertGreater(data["regions"]["canvas_px"], 0)
            self.assertIn("note", data["resolution"])
            # 报告文件写在输入图旁
            self.assertTrue(os.path.exists(os.path.splitext(path)[0] + ".survey.json"))
            self.assertTrue(os.path.exists(os.path.splitext(path)[0] + ".survey.md"))
            js = json.load(open(os.path.splitext(path)[0] + ".survey.json", encoding="utf-8"))
            self.assertEqual(js["schema"], "fbs-survey/1")
        finally:
            for p in (path, os.path.splitext(path)[0] + ".survey.json", os.path.splitext(path)[0] + ".survey.md"):
                try:
                    os.remove(p)
                except OSError:  # noqa: 清理失败不影响测试语义
                    pass


class TestStrokeRLRender(unittest.TestCase):
    def test_rl_uses_real_renderer_and_reports_objective(self):
        h = w = 56
        target = np.zeros((h, w, 3))
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
        target[..., 0] = 0.2 + 0.4 * xx / w
        target[..., 1] = 0.3 + 0.2 * yy / h
        target[..., 2] = 0.6
        strokes = []
        for k in range(10):
            y = 6.0 + k * 4.5
            pts = np.stack([np.linspace(4, 52, 6), np.full(6, y)], axis=1)
            strokes.append(
                {
                    "class": "flat" if k % 4 else "line_thick",
                    "preset": "dry_flat",
                    "shape": "bristle",
                    "points_px": pts,
                    "widths_px": np.full(6, 5.0),
                    "color": np.array([0.3, 0.4, 0.6]),
                    "alpha": 1.0,
                }
            )
        tmpdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "out", "audit")
        os.makedirs(tmpdir, exist_ok=True)
        log = os.path.join(tmpdir, "_test_stroke_rl_render.json")
        try:
            res = stroke_rl_render.run_stroke_rl_render(
                target, strokes, log_path=log, working_res=56, S=1,
                rng=np.random.default_rng(5), min_improve=1e-3,
            )
            st = res["stats"]
            for key in ("strokes_total", "strokes_accepted", "accept_rate", "segments_accepted",
                        "canvas_psnr_db", "objective_J", "arms", "curve", "weights"):
                self.assertIn(key, st)
            self.assertGreater(st["strokes_accepted"], 0)
            self.assertGreaterEqual(st["segments_accepted"], 0)
            self.assertLessEqual(len(res["accepted"]), len(strokes))
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
