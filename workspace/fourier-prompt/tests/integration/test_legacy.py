"""Legacy ``fdsl/2`` → ``fdsl/2.1`` conversion tests (WP-D)."""

from __future__ import annotations

import copy
import json
import sys
import unittest

from _fixtures import EXAMPLES, PACKAGE_ROOT

if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from fourier_prompt.legacy import LegacyError, from_legacy, is_legacy  # noqa: E402
from fourier_prompt.schema import validate_plan  # noqa: E402


def load_example(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


class LegacyDetectionCase(unittest.TestCase):
    def test_all_six_drafts_are_legacy(self):
        files = sorted(EXAMPLES.glob("*.json"))
        self.assertEqual(len(files), 6)
        for path in files:
            with self.subTest(name=path.name):
                plan = json.loads(path.read_text(encoding="utf-8"))
                self.assertTrue(is_legacy(plan))

    def test_canonical_plan_is_not_legacy(self):
        from _fixtures import canonical_plan_shapes

        self.assertFalse(is_legacy(canonical_plan_shapes()))


class LegacyConversionCase(unittest.TestCase):
    def test_canvas_and_schema_normalised(self):
        plan, warnings = from_legacy(load_example("01_rose_medallion.json"))
        self.assertEqual(plan["schema"], "fdsl/2.1")
        self.assertEqual(plan["canvas"]["width"], 1024)
        self.assertEqual(plan["canvas"]["height"], 1024)
        self.assertEqual(plan["canvas"]["background"], "#f6efe0")
        self.assertNotIn("w", plan["canvas"])
        self.assertNotIn("h", plan["canvas"])
        self.assertEqual(validate_plan(plan), [])
        self.assertTrue(any(w["code"] == "FP_W_LEGACY_CANVAS" for w in warnings))

    def test_elements_have_canonical_kind(self):
        plan, _warnings = from_legacy(load_example("05_aurora.json"))
        for element in plan["elements"]:
            self.assertIn(element["kind"], {"fourier_gen", "stroke", "fill", "text"})
            self.assertNotIn("type", element)
            self.assertIn("layer", element)
        self.assertEqual(validate_plan(plan), [])

    def test_fill_gen_becomes_explicit_poly(self):
        plan, _warnings = from_legacy(load_example("02_star_badge.json"))
        fills = [el for el in plan["elements"] if el["kind"] == "fill"]
        self.assertGreaterEqual(len(fills), 3)
        for fill in fills:
            self.assertIn("poly", fill)
            self.assertGreaterEqual(len(fill["poly"]), 3)
            self.assertNotIn("gen", fill)
        star_fill = fills[2]
        self.assertEqual(len(star_fill["poly"]), 10)  # 5 角星 = 10 个精确顶点

    def test_legacy_gen_is_expanded_to_stroke(self):
        plan, _warnings = from_legacy(load_example("04_ocean_waves.json"))
        self.assertFalse(any(el["kind"] == "fourier_gen" for el in plan["elements"]))
        self.assertTrue(all("points" in el for el in plan["elements"] if el["kind"] == "stroke"))
        self.assertEqual(validate_plan(plan), [])

    def test_repeat_expands_with_transform(self):
        plan, _warnings = from_legacy(load_example("02_star_badge.json"))
        repeated = [el for el in plan["elements"] if el.get("transform")]
        # 60 道刻度 + 5 颗圆点 = 65 个副本（首个副本 rotate=0 也算副本）
        self.assertEqual(len(repeated), 65)
        self.assertEqual(validate_plan(plan), [])

    def test_repeat_scale_is_preserved(self):
        plan, _warnings = from_legacy(load_example("03_golden_spiral.json"))
        scaled = [el for el in plan["elements"] if el.get("transform", {}).get("scale") == 0.62]
        self.assertEqual(len(scaled), 2)

    def test_bezier_stroke_becomes_points(self):
        plan, _warnings = from_legacy(load_example("06_cat_tophat.json"))
        # 原始草稿里恰好一条 bezier 笔画
        self.assertGreaterEqual(len(plan["elements"]), 1)
        for element in plan["elements"]:
            if element["kind"] == "stroke":
                self.assertIsInstance(element["points"], list)
                self.assertGreaterEqual(len(element["points"]), 2)
        self.assertEqual(validate_plan(plan), [])

    def test_render_fields_move_to_meta(self):
        plan, warnings = from_legacy(load_example("01_rose_medallion.json"))
        self.assertEqual(plan["meta"]["legacy_render"]["fill_soft_px"], 0.5)
        self.assertEqual(plan["meta"]["legacy_render_contract"]["resolution"], 1024)
        self.assertNotIn("render", plan)
        self.assertNotIn("render_contract", plan)
        self.assertTrue(any(w["code"] == "FP_W_LEGACY_RENDER" for w in warnings))

    def test_layers_are_synthesised(self):
        plan, warnings = from_legacy(load_example("01_rose_medallion.json"))
        ids = [layer["id"] for layer in plan["layers"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(validate_plan(plan), [])
        self.assertTrue(any(w["code"] == "FP_W_LEGACY_LAYER" for w in warnings))

    def test_brush_alias_is_mapped(self):
        plan, warnings = from_legacy(load_example("05_aurora.json"))
        presets = {el["brush"].get("preset") for el in plan["elements"] if el["kind"] == "stroke"}
        self.assertIn("basic_airbrush", presets)
        self.assertNotIn("airbrush", presets)
        self.assertTrue(any(w["code"] == "FP_W_LEGACY_BRUSH_ALIAS" for w in warnings))

    def test_unknown_field_is_warned_and_dropped(self):
        draft = load_example("01_rose_medallion.json")
        draft = copy.deepcopy(draft)
        draft["future_block"] = {"x": 1}
        draft["elements"][0]["mystery"] = 42
        plan, warnings = from_legacy(draft)
        self.assertNotIn("future_block", plan)
        self.assertNotIn("mystery", plan["elements"][0])
        codes = [w["code"] for w in warnings]
        self.assertIn("FP_W_LEGACY_UNKNOWN_FIELD", codes)

    def test_unknown_element_type_raises(self):
        draft = load_example("01_rose_medallion.json")
        draft = copy.deepcopy(draft)
        draft["elements"][0]["type"] = "hologram"
        with self.assertRaises(LegacyError):
            from_legacy(draft)

    def test_bad_repeat_raises(self):
        draft = load_example("01_rose_medallion.json")
        draft = copy.deepcopy(draft)
        draft["elements"][3]["repeat"] = {"n": 0, "around": [512, 512]}
        with self.assertRaises(LegacyError):
            from_legacy(draft)

    def test_unknown_gen_raises(self):
        draft = load_example("01_rose_medallion.json")
        draft = copy.deepcopy(draft)
        draft["elements"][0]["gen"] = "unicorn"
        with self.assertRaises(LegacyError):
            from_legacy(draft)


if __name__ == "__main__":
    unittest.main()
