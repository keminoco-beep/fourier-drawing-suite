"""``fourier_prompt.brush.swatch`` 测试：生成/尺寸/确定性/planned 拒绝/skill 只读边界。

渲染用 R128–256（KICKOFF §4：R<=512），只选 2–3 支笔控制耗时。
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fourier_prompt.brush.catalog import BrushCatalog, BrushCatalogError, UnknownPresetError  # noqa: E402
from fourier_prompt.brush.swatch import MAX_RESOLUTION, render_row, render_swatch  # noqa: E402

SUBSET = ["ink_gpen", "basic_airbrush"]


class TestSwatchRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = BrushCatalog.load()

    def test_subset_board(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "swatch.png"
            stats = render_swatch(out, presets=SUBSET, resolution=128, catalog=self.cat)
            self.assertTrue(out.is_file())
            self.assertEqual(stats["count"], 2)
            self.assertEqual(stats["presets"], SUBSET)
            self.assertEqual(stats["resolution"], 128)
            self.assertEqual(stats["columns"], 2)
            # 2 支笔 → 1 行 → 高 128/4=32 + 标签 16
            self.assertEqual(stats["sheet"]["width"], 256)
            self.assertEqual(stats["sheet"]["height"], 48)
            self.assertEqual(stats["sheet"]["bytes"], out.stat().st_size)
            self.assertEqual(stats["sheet"]["sha256"], hashlib.sha256(out.read_bytes()).hexdigest())
            self.assertTrue(stats["alpha_normalized"])

    def test_deterministic_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "a.png"
            b = Path(td) / "b.png"
            s1 = render_swatch(a, presets=SUBSET, resolution=128, previews=False, catalog=self.cat)
            s2 = render_swatch(b, presets=SUBSET, resolution=128, previews=False, catalog=self.cat)
            self.assertEqual(s1["sheet"]["sha256"], s2["sheet"]["sha256"])
            self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_previews_written(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "swatch.png"
            stats = render_swatch(out, presets=SUBSET, resolution=128, catalog=self.cat)
            self.assertEqual(stats["previews"]["count"], 2)
            for pid in SUBSET:
                pv = Path(stats["previews"]["dir"]) / f"{pid}.png"
                self.assertTrue(pv.is_file(), pid)
                self.assertEqual(stats["previews"]["sha256"][pid],
                                 hashlib.sha256(pv.read_bytes()).hexdigest())

    def test_row_shape_and_determinism(self):
        from fourier_prompt.brush.calib import BrushCalib

        cal = BrushCalib.load()
        a = render_row("ink_gpen", catalog=self.cat, calib=cal, resolution=128)
        b = render_row("ink_gpen", catalog=self.cat, calib=cal, resolution=128)
        self.assertEqual(tuple(a.shape), (32, 128, 3))
        self.assertEqual(hashlib.sha256(a.tobytes()).hexdigest(),
                         hashlib.sha256(b.tobytes()).hexdigest())
        self.assertGreater(int(a.min()), 0)  # 白底（非全黑）
        self.assertLess(int(a.min()), 60)    # 有墨迹

    def test_unknown_preset_fails_loud(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(UnknownPresetError):
                render_swatch(Path(td) / "x.png", presets=["no_such_brush"], resolution=96,
                              previews=False, catalog=self.cat)

    def test_planned_preset_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(BrushCatalogError):
                render_swatch(Path(td) / "x.png", presets=["fill_round"], resolution=96,
                              previews=False, catalog=self.cat)

    def test_resolution_bounds(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                render_swatch(Path(td) / "x.png", presets=SUBSET, resolution=MAX_RESOLUTION + 1,
                              previews=False, catalog=self.cat)
            with self.assertRaises(ValueError):
                render_swatch(Path(td) / "x.png", presets=SUBSET, resolution=0,
                              previews=False, catalog=self.cat)

    def test_does_not_import_skill_document_module(self):
        # WP-B 正在改 document.py：swatch 必须走 io_format/brush/fourier/render 自建文档
        with tempfile.TemporaryDirectory() as td:
            render_swatch(Path(td) / "x.png", presets=["ink_gpen"], resolution=96,
                          previews=False, catalog=self.cat)
        self.assertNotIn("fbs_core.document", sys.modules)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
