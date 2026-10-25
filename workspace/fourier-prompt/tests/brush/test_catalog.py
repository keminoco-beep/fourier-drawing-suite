"""``fourier_prompt.brush.catalog`` 测试：结构/枚举/planned/校验/fail-loud。

运行：``cd fourier-prompt && python -m unittest discover -s tests/brush -v``
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]  # fourier-prompt/
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fourier_prompt.brush.catalog import (  # noqa: E402
    CATALOG_VERSION,
    DEFAULT_CATALOG_PATH,
    ROLES,
    BrushCatalog,
    BrushCatalogError,
    UnknownPresetError,
)


class TestCatalogLoad(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = BrushCatalog.load()

    def test_default_path_exists(self):
        self.assertTrue(DEFAULT_CATALOG_PATH.is_file(), f"缺目录数据文件: {DEFAULT_CATALOG_PATH}")

    def test_schema_and_version(self):
        self.assertEqual(self.cat.version, CATALOG_VERSION)
        self.assertEqual(self.cat.as_dict()["schema"], "brush-catalog/1")

    def test_counts(self):
        # 20 个现有 skill 预设（id 不改名）+ 8–12 个 planned
        self.assertEqual(len(self.cat), 31)
        self.assertEqual(len(self.cat.ids()), 20)
        self.assertEqual(len(self.cat.planned_ids()), 11)
        self.assertTrue(8 <= len(self.cat.planned_ids()) <= 12)
        ids = set(self.cat.ids())
        expected = {
            "basic_round_hard", "basic_round_soft", "basic_airbrush",
            "pencil_2b", "pencil_hb",
            "ink_fineliner", "ink_gpen", "ink_brush_rough", "sumi_e",
            "marker_chisel", "marker_alcohol",
            "bristle_oil", "dry_flat", "palette_knife",
            "chalk", "charcoal", "pastel",
            "wet_oil", "watercolor_wash",
            "spatter_brush",
        }
        self.assertEqual(ids, expected)

    def test_get_returns_required_fields(self):
        meta = self.cat.get("ink_gpen")
        for field in (
            "id", "name", "category", "shape", "sizes_px", "width_eff", "pressure",
            "roles", "default_opacity", "recommended_layers", "preview", "license",
            "version", "status", "name_zh", "traits",
        ):
            self.assertIn(field, meta, f"ink_gpen 缺字段 {field}")
        self.assertEqual(meta["shape"], "ink")
        self.assertEqual(meta["category"], "inking")
        self.assertIn("line_taper", meta["roles"])
        self.assertEqual(meta["license"], "proprietary")
        self.assertTrue(meta["preview"].endswith(".png"))

    def test_get_is_deep_copy(self):
        meta = self.cat.get("chalk")
        meta["roles"].append("__hacked__")
        self.assertNotIn("__hacked__", self.cat.get("chalk")["roles"])

    def test_get_unknown_raises(self):
        with self.assertRaises(UnknownPresetError) as ctx:
            self.cat.get("no_such_brush")
        self.assertIsInstance(ctx.exception, KeyError)
        self.assertIn("no_such_brush", str(ctx.exception))
        # 大小写/拼写不同也是未知（不猜）
        with self.assertRaises(KeyError):
            self.cat.get("Ink_GPen")

    def test_list_by_category(self):
        markers = self.cat.list(category="marker")
        self.assertEqual({m["id"] for m in markers}, {"marker_chisel", "marker_alcohol"})
        self.assertEqual(len(self.cat.list(category="pencil")), 2)
        with self.assertRaises(BrushCatalogError):
            self.cat.list(category="not_a_category")

    def test_list_by_role(self):
        hairline = self.cat.list(role="line_hairline")
        self.assertEqual({m["id"] for m in hairline}, {"ink_fineliner", "ink_gpen"})
        for meta in self.cat.list(role="texture"):
            self.assertIn("texture", meta["roles"])
        with self.assertRaises(BrushCatalogError):
            self.cat.list(role="line_detail")  # 设计稿早期的旧名，不在词表里
        # 每个角色至少 2 支可渲染笔（策略兜底的前提）
        index = self.cat.role_index()
        self.assertEqual(set(index), set(ROLES))
        for role, ids in index.items():
            self.assertGreaterEqual(len(ids), 2, f"角色 {role} 少于 2 支笔")

    def test_planned_excluded_by_default(self):
        all_ids = self.cat.ids()
        for pid in self.cat.planned_ids():
            self.assertNotIn(pid, all_ids)
            self.assertFalse(self.cat.is_renderable(pid))
        with_planned = self.cat.list(include_planned=True)
        self.assertEqual(len(with_planned), 31)
        self.assertIn("fill_round", {m["id"] for m in with_planned})

    def test_planned_entries_are_marked(self):
        for pid in self.cat.planned_ids():
            meta = self.cat.get(pid)
            self.assertEqual(meta["status"], "planned")
            self.assertFalse(meta["renderable"])
            self.assertEqual(meta["calib"], "estimated")
            self.assertIn(meta["implements_base"], self.cat.ids())

    def test_validate_ok(self):
        self.assertEqual(self.cat.validate(), [])

    def test_load_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            BrushCatalog.load(_ROOT / "assets" / "brush" / "__nope__.json")

    def test_roundtrip_json_stable(self):
        data = self.cat.as_dict()
        again = BrushCatalog.from_dict(data)
        self.assertEqual(json.dumps(again.as_dict(), sort_keys=True, ensure_ascii=False),
                         json.dumps(data, sort_keys=True, ensure_ascii=False))


class TestCatalogValidateDetectsCorruption(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(Path(DEFAULT_CATALOG_PATH).read_text(encoding="utf-8"))

    def _cat(self, data):
        return BrushCatalog.from_dict(data)

    def test_bad_category(self):
        self.data["presets"]["ink_gpen"]["category"] = "calligraphy"
        self.assertTrue(any("category" in e for e in self._cat(self.data).validate()))

    def test_missing_field(self):
        del self.data["presets"]["sumi_e"]["width_eff"]
        self.assertTrue(any("width_eff" in e for e in self._cat(self.data).validate()))

    def test_bad_width_eff(self):
        self.data["presets"]["basic_round_soft"]["width_eff"] = 0.0
        self.assertTrue(any("width_eff" in e for e in self._cat(self.data).validate()))

    def test_bad_pressure_curve(self):
        self.data["presets"]["chalk"]["pressure"]["width"] = [[0.5, 1.0]]  # 只有 1 点
        self.assertTrue(any("pressure.width" in e for e in self._cat(self.data).validate()))

    def test_unknown_role(self):
        self.data["presets"]["chalk"]["roles"] = ["texture", "grain"]
        errs = self._cat(self.data).validate()
        self.assertTrue(any("未知角色" in e for e in errs))

    def test_planned_claims_renderable(self):
        self.data["presets"]["fill_round"]["renderable"] = True
        self.assertTrue(any("planned" in e for e in self._cat(self.data).validate()))

    def test_active_shape_must_be_registered(self):
        self.data["presets"]["ink_gpen"]["shape"] = "calligraphy_nib"
        self.assertTrue(any("不是已注册笔形" in e for e in self._cat(self.data).validate()))

    def test_planned_may_use_unregistered_shape(self):
        # planned 允许写"未来笔形"（pencil_tilt 等），不应被 shape 校验拦住
        errs = self._cat(self.data).validate()
        self.assertEqual([e for e in errs if "不是已注册笔形" in e], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
