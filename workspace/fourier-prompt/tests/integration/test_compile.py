"""``compile_plan`` mapping / failure-path tests (WP-D)."""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest

from _fixtures import PACKAGE_ROOT, canonical_plan_curves, canonical_plan_shapes

if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from fourier_prompt import generators  # noqa: E402
from fourier_prompt.compile import CompileError, compile_plan, normalized_fbs  # noqa: E402
from fourier_prompt.compat import skill_api  # noqa: E402


class CompileMappingCase(unittest.TestCase):
    def test_canonical_plan_compiles(self):
        result = compile_plan(canonical_plan_shapes(), resolution=128)
        stats = result["stats"]
        self.assertTrue(stats["legacy"] is False)
        self.assertEqual(stats["elements"], 5)
        self.assertEqual(stats["records"], 4)
        self.assertEqual(stats["fill"], 2)  # 矩形 + circle fill
        self.assertEqual(stats["stroke"], 2)  # star closed=false + stroke
        self.assertEqual(stats["text_skipped"], 1)
        self.assertEqual(result["ledger"]["records"], 4)
        self.assertTrue(any(w["code"] == "FP_W_TEXT_SKIPPED" for w in result["warnings"]))
        self.assertGreater(stats["fbs_bytes"], 0)

    def test_fourier_gen_closed_defaults_to_fill(self):
        plan = canonical_plan_shapes()
        plan = copy.deepcopy(plan)
        plan["elements"] = [plan["elements"][1]]  # circle without explicit closed
        plan["elements"][0].pop("closed")
        result = compile_plan(plan, resolution=128)
        record = result["fbs"]["strokes"][0]
        self.assertEqual(record["fill_role"], "region_fill")
        self.assertIn("poly", record)
        self.assertEqual(result["stats"]["fill"], 1)

    def test_fourier_gen_closed_false_is_stroke(self):
        result = compile_plan(canonical_plan_shapes(), resolution=128)
        stroke_records = [s for s in result["fbs"]["strokes"] if s["fill_role"] != "region_fill"]
        self.assertEqual(len(stroke_records), 2)
        star = stroke_records[0]
        self.assertTrue(star["closed"] is False)
        self.assertNotIn("poly", star)

    def test_fill_holes_reach_document(self):
        result = compile_plan(canonical_plan_shapes(), resolution=128)
        fills = [s for s in result["fbs"]["strokes"] if s["fill_role"] == "region_fill"]
        self.assertEqual(len(fills[0]["fill_holes"]), 1)
        self.assertEqual(len(fills[0]["fill_holes"][0]), 4)

    def test_transform_changes_bbox(self):
        plan = canonical_plan_shapes()
        plan = copy.deepcopy(plan)
        plan["elements"] = [plan["elements"][2]]  # star stroke with rotate 15
        rotated = compile_plan(plan, resolution=128)["fbs"]["strokes"][0]["bbox"]
        plan2 = copy.deepcopy(plan)
        plan2["elements"][0].pop("transform")
        plain = compile_plan(plan2, resolution=128)["fbs"]["strokes"][0]["bbox"]
        self.assertNotEqual([round(v, 6) for v in rotated], [round(v, 6) for v in plain])

    def test_palette_colours_are_resolved(self):
        result = compile_plan(canonical_plan_shapes(), resolution=128)
        styles = result["fbs"]["brush_styles"]
        self.assertGreaterEqual(len(styles), 2)
        colors = [style["color"] for style in styles]
        self.assertTrue(all(isinstance(color, dict) for color in colors))

    def test_open_generators_stroke_and_width_curve(self):
        result = compile_plan(canonical_plan_curves(), resolution=128)
        stats = result["stats"]
        self.assertEqual(stats["stroke"], 2)  # arc + wave
        self.assertEqual(stats["fill"], 2)  # superellipse + lissajous
        widths = [stroke["brush"]["width"] for stroke in result["fbs"]["strokes"]]
        self.assertTrue(any(len(width.get("keys") or []) > 1 for width in widths))

    def test_document_is_valid_fbs_and_renderable(self):
        result = compile_plan(canonical_plan_shapes(), resolution=128)
        data = skill_api.write_fbs(result["fbs"])
        self.assertTrue(data.startswith(b"{"))
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/plan.fbs"
            skill_api.write_fbs(result["fbs"], path)
            reloaded = skill_api.import_skill().io_format.read_fbs(path)
            rgb = skill_api.render(reloaded, resolution=128, supersample=1)
        self.assertEqual(rgb.shape, (128, 128, 3))
        self.assertGreater(int(rgb.sum()), 0)

    def test_normalized_hash_is_stable(self):
        first = compile_plan(canonical_plan_shapes(), resolution=128)
        second = compile_plan(canonical_plan_shapes(), resolution=128)
        self.assertEqual(normalized_fbs(first["fbs"]), normalized_fbs(second["fbs"]))
        self.assertIn("created", first["fbs"])
        self.assertNotIn('"created"', normalized_fbs(first["fbs"]))


class CompileFailureCase(unittest.TestCase):
    def test_unknown_gen_fails(self):
        plan = canonical_plan_shapes()
        plan = copy.deepcopy(plan)
        plan["elements"][1]["gen"] = "unicorn"
        with self.assertRaises(CompileError) as ctx:
            compile_plan(plan, resolution=128)
        self.assertIn("FP_E_GEN_UNKNOWN", ctx.exception.code)

    def test_unknown_brush_fails(self):
        plan = canonical_plan_shapes()
        plan = copy.deepcopy(plan)
        plan["elements"][3]["brush"]["preset"] = "unicorn_brush"
        with self.assertRaises(CompileError) as ctx:
            compile_plan(plan, resolution=128)
        self.assertIn("FP_E_BRUSH_PRESET_UNKNOWN", ctx.exception.code)

    def test_unknown_element_kind_fails(self):
        plan = canonical_plan_shapes()
        plan = copy.deepcopy(plan)
        plan["elements"][0]["kind"] = "hologram"
        with self.assertRaises(CompileError) as ctx:
            compile_plan(plan, resolution=128)
        self.assertIn("FP_E_ELEMENT_KIND", ctx.exception.code)

    def test_unknown_param_fails(self):
        plan = canonical_plan_shapes()
        plan = copy.deepcopy(plan)
        plan["elements"][1]["params"]["radius"] = 10
        with self.assertRaises(CompileError) as ctx:
            compile_plan(plan, resolution=128)
        self.assertIn("FP_E_GEN_PARAMS", ctx.exception.code)

    def test_empty_plan_fails(self):
        plan = canonical_plan_shapes()
        plan = copy.deepcopy(plan)
        plan["elements"] = [plan["elements"][4]]  # only text
        with self.assertRaises(CompileError) as ctx:
            compile_plan(plan, resolution=128)
        self.assertEqual(ctx.exception.code, "FP_E_COMPILE_EMPTY")

    def test_degenerate_fill_fails(self):
        plan = canonical_plan_shapes()
        plan = copy.deepcopy(plan)
        plan["elements"][0]["poly"] = [[10, 10], [20, 10], [30, 10]]
        with self.assertRaises(CompileError) as ctx:
            compile_plan(plan, resolution=128)
        self.assertEqual(ctx.exception.code, "FP_E_COMPILE_GEOMETRY")

    def test_invalid_resolution_fails(self):
        for resolution in (0, 8, 8192, True):
            with self.subTest(resolution=resolution):
                with self.assertRaises(CompileError):
                    compile_plan(canonical_plan_shapes(), resolution=resolution)

    def test_bad_plan_type_fails(self):
        with self.assertRaises(CompileError):
            compile_plan(["not", "a", "plan"], resolution=128)

    def test_invalid_schema_fails(self):
        plan = canonical_plan_shapes()
        plan = copy.deepcopy(plan)
        plan["schema"] = "fdsl/9"
        with self.assertRaises(CompileError):
            compile_plan(plan, resolution=128)

    def test_skill_selftest_passes(self):
        messages = skill_api.selftest()
        self.assertTrue(any(message.startswith("skill_dir:") for message in messages))
        self.assertTrue(any("render: ok" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
