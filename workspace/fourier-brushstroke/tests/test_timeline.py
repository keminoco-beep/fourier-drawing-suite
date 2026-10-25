"""画笔属性时间轴（brush timeline）单元测试。

契约：时间轴 = 傅里叶曲线 + 随时间 t 的画笔属性；每笔有 [t0,t1]、属性曲线原样保留，
时间有序且落在总时长内；能独立成文件读取（不依赖渲染器）。
"""

import json
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from fbs_core import timeline  # noqa: E402


def _fake_doc(n=5) -> dict:
    strokes = []
    styles = []
    for i in range(n):
        strokes.append(
            {
                "id": i,
                "closed": False,
                "fill_role": "medial_branch",
                "N": 3,
                "arc_length": 0.05 + 0.01 * i,
                "coef": [[0, 0.5, 0.5], [1, 0.01, 0.02], [-1, 0.02, -0.01]],
                "brush": {"style": 0 if i % 2 == 0 else 1, "width": {"interp": "constant", "keys": [[0, 0.004]]}},
            }
        )
    styles = [
        {"shape": {"interp": "step", "keys": [[0, "soft"]]}, "color": {"interp": "constant", "keys": [[0, [200, 100, 80, 255]]]}, "alpha": {"interp": "constant", "keys": [[0, 0.9]]}},
        {"shape": {"interp": "step", "keys": [[0, "round"]]}, "color": {"interp": "constant", "keys": [[0, [20, 40, 60, 255]]]}, "alpha": {"interp": "constant", "keys": [[0, 1.0]]}},
    ]
    return {"mode": "per_stroke", "strokes": strokes, "brush_styles": styles}


class TestBrushTimeline(unittest.TestCase):
    def test_timeline_structure_and_ordering(self):
        doc = _fake_doc()
        data = timeline.build_timeline(doc, duration_s=30.0, overlap=2.0)
        self.assertEqual(data["schema"], "fbs-brush-timeline/1")
        self.assertEqual(data["stroke_count"], 5)
        self.assertEqual(len(data["strokes"]), 5)
        t_prev = -1.0
        for st in data["strokes"]:
            self.assertLessEqual(0.0, st["t0"])
            self.assertLessEqual(st["t0"], st["t1"])
            self.assertLessEqual(st["t1"], 30.0 + 1e-9)
            self.assertGreaterEqual(st["t0"], t_prev - 1e-9, "起始时间应随作画顺序单调")
            t_prev = st["t0"]
            # 傅里叶曲线与压力/颜色属性都在
            self.assertTrue(st["fourier"]["coef"])
            self.assertEqual(st["fourier"]["closed"], False)
            self.assertIn("width", st["brush"])
            self.assertIsInstance(st["brush"]["style"], int)
        self.assertEqual(len(data["styles"]), 2, "样式表去重后应只出现用到的样式")
        self.assertEqual(data["styles"][0]["shape"]["keys"][0][1], "soft")

    def test_coefficients_quantized_but_renderable(self):
        doc = _fake_doc()
        data = timeline.build_timeline(doc, duration_s=10.0)
        for st in data["strokes"]:
            for k, re, im in st["fourier"]["coef"]:
                self.assertIsInstance(k, int)
                self.assertEqual(round(float(re), 5), float(re))
                self.assertEqual(round(float(im), 5), float(im))

    def test_write_and_read(self):
        doc = _fake_doc(3)
        tmp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "out", "audit")
        os.makedirs(tmp, exist_ok=True)
        path = os.path.join(tmp, "_test_timeline.json")
        try:
            timeline.write_timeline(doc, path, duration_s=12.0)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["duration_s"], 12.0)
            self.assertEqual(len(data["strokes"]), 3)
            self.assertGreater(os.path.getsize(path), 200)
        finally:
            try:
                os.remove(path)
            except OSError:  # noqa: 清理临时文件失败不影响测试语义（文件在 audit 临时名）
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
