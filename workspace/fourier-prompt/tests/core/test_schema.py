"""Schema validation tests for FDSL v2.1 (WP-A core)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for candidate in (str(HERE), str(ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from _fixtures import clone, valid_plan, write_json, write_text  # noqa: E402

from fourier_prompt.schema import (  # noqa: E402
    FDSL_SCHEMA,
    FDSchemaError,
    load_plan,
    validate_plan,
    validate_plan_detailed,
)


class SchemaValidCase(unittest.TestCase):
    def assert_has_error(self, errors, code):
        self.assertTrue(
            any(item.startswith(code) for item in errors),
            f"expected {code}, got {errors}",
        )

    # -- happy path ---------------------------------------------------------
    def test_valid_plan_passes(self):
        self.assertEqual(validate_plan(valid_plan()), [])

    def test_fdsl_2_is_accepted(self):
        plan = valid_plan()
        plan["schema"] = "fdsl/2"
        self.assertEqual(validate_plan(plan), [])

    def test_reported_schema_constant(self):
        self.assertEqual(FDSL_SCHEMA, "fdsl/2.1")

    def test_gen_alias_type_is_accepted(self):
        plan = valid_plan()
        plan["elements"][1]["type"] = "gen"
        del plan["elements"][1]["kind"]
        self.assertEqual(validate_plan(plan), [])

    def test_detailed_report_shape(self):
        report = validate_plan_detailed(valid_plan())
        self.assertTrue(report["ok"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["schema"], "fdsl/2.1")

    # -- top level / canvas -------------------------------------------------
    def test_unknown_schema_rejected(self):
        plan = valid_plan()
        plan["schema"] = "fdsl/9"
        self.assert_has_error(validate_plan(plan), "FP_E_SCHEMA_VERSION")

    def test_missing_schema_rejected(self):
        plan = valid_plan()
        del plan["schema"]
        self.assert_has_error(validate_plan(plan), "FP_E_SCHEMA_MISSING")

    def test_non_object_plan_raises(self):
        with self.assertRaises(FDSchemaError) as ctx:
            validate_plan([])
        self.assertEqual(ctx.exception.code, "FP_E_PLAN_TYPE")

    def test_canvas_missing_dimension(self):
        plan = valid_plan()
        del plan["canvas"]["width"]
        self.assert_has_error(validate_plan(plan), "FP_E_CANVAS_MISSING")

    def test_canvas_out_of_range(self):
        plan = valid_plan()
        plan["canvas"]["height"] = 8
        self.assert_has_error(validate_plan(plan), "FP_E_CANVAS_RANGE")

    def test_canvas_background_invalid(self):
        plan = valid_plan()
        plan["canvas"]["background"] = "#zzzzzz"
        self.assert_has_error(validate_plan(plan), "FP_E_COLOR_INVALID")

    # -- elements -----------------------------------------------------------
    def test_unknown_gen_rejected(self):
        plan = valid_plan()
        plan["elements"][1]["gen"] = "unicorn"
        self.assert_has_error(validate_plan(plan), "FP_E_GEN_UNKNOWN")

    def test_unknown_brush_preset_rejected(self):
        plan = valid_plan()
        plan["elements"][1]["brush"]["preset"] = "magic_pen"
        errors = validate_plan(plan)
        self.assert_has_error(errors, "FP_E_BRUSH_PRESET_UNKNOWN")

    def test_unknown_brush_shape_rejected(self):
        plan = valid_plan()
        plan["elements"][1]["brush"]["shape"] = "spray9"
        self.assert_has_error(validate_plan(plan), "FP_E_BRUSH_SHAPE")

    def test_unknown_element_kind_rejected(self):
        plan = valid_plan()
        plan["elements"][0]["kind"] = "bitmap_stamp"
        self.assert_has_error(validate_plan(plan), "FP_E_ELEMENT_KIND")

    def test_stroke_requires_two_points(self):
        plan = valid_plan()
        plan["elements"][2]["points"] = [[10, 10]]
        self.assert_has_error(validate_plan(plan), "FP_E_POINTS_MIN")

    def test_fill_requires_three_points(self):
        plan = valid_plan()
        plan["elements"][0]["poly"] = [[0, 0], [10, 10]]
        self.assert_has_error(validate_plan(plan), "FP_E_POINTS_MIN")

    def test_non_finite_point_rejected(self):
        plan = valid_plan()
        plan["elements"][2]["points"].append([float("nan"), 5])
        self.assert_has_error(validate_plan(plan), "FP_E_POINT_INVALID")

    def test_duplicate_element_id_rejected(self):
        plan = valid_plan()
        plan["elements"][2]["id"] = "e2"
        self.assert_has_error(validate_plan(plan), "FP_E_ELEMENT_ID")

    def test_invalid_element_id_pattern_rejected(self):
        plan = valid_plan()
        plan["elements"][2]["id"] = "hero"
        self.assert_has_error(validate_plan(plan), "FP_E_ELEMENT_ID")

    def test_too_many_tags_rejected(self):
        plan = valid_plan()
        plan["elements"][1]["tags"] = [f"tag{i}" for i in range(9)]
        self.assert_has_error(validate_plan(plan), "FP_E_TAGS")

    def test_invalid_tag_pattern_rejected(self):
        plan = valid_plan()
        plan["elements"][1]["tags"] = ["Bad Tag"]
        self.assert_has_error(validate_plan(plan), "FP_E_TAG")

    def test_undeclared_layer_rejected(self):
        plan = valid_plan()
        plan["elements"][2]["layer"] = 7
        self.assert_has_error(validate_plan(plan), "FP_E_LAYER_REF")

    def test_element_limit_from_constraints(self):
        plan = valid_plan()
        plan["constraints"]["max_elements"] = 2
        self.assert_has_error(validate_plan(plan), "FP_E_ELEMENT_LIMIT")

    def test_alpha_out_of_range_rejected(self):
        plan = valid_plan()
        plan["elements"][1]["brush"]["alpha"] = 1.5
        self.assert_has_error(validate_plan(plan), "FP_E_ALPHA_RANGE")

    def test_width_curve_must_be_monotonic(self):
        plan = valid_plan()
        plan["elements"][2]["brush"]["width"] = [[1.0, 2.0], [0.0, 3.0]]
        self.assert_has_error(validate_plan(plan), "FP_E_WIDTH_ORDER")

    def test_transform_zero_scale_rejected(self):
        plan = valid_plan()
        plan["elements"][1]["transform"]["scale"] = 0
        self.assert_has_error(validate_plan(plan), "FP_E_SCALE_RANGE")

    def test_palette_list_with_bad_colour_rejected(self):
        plan = valid_plan()
        plan["palette"] = ["#12203a", "not-a-colour"]
        self.assert_has_error(validate_plan(plan), "FP_E_COLOR_INVALID")

    def test_short_hex_is_rejected_by_schema(self):
        # 3-digit hex is repaired (expanded) but not accepted as canonical FDSL.
        plan = valid_plan()
        plan["elements"][1]["brush"]["color"] = "#abc"
        self.assert_has_error(validate_plan(plan), "FP_E_COLOR_INVALID")

    def test_named_palette_colour_accepted(self):
        plan = valid_plan()
        plan["canvas"]["background"] = "ink"
        self.assertEqual(validate_plan(plan), [])

    # -- texts --------------------------------------------------------------
    def test_text_missing_body_rejected(self):
        plan = valid_plan()
        del plan["texts"][0]["text"]
        self.assert_has_error(validate_plan(plan), "FP_E_TEXT_MISSING")

    def test_text_element_is_structurally_valid(self):
        plan = valid_plan()
        plan["elements"].append(
            {
                "kind": "text",
                "layer": 1,
                "text": "标题",
                "font": {"id": "lxgw_wenkai", "size_px": 32},
                "box": [10, 10, 120, 40],
            }
        )
        self.assertEqual(validate_plan(plan), [])

    def test_unknown_font_is_warning(self):
        plan = valid_plan()
        plan["texts"][0]["font"]["id"] = "comic_sans"
        self.assertEqual(validate_plan(plan), [])  # warning only
        report = validate_plan_detailed(plan)
        self.assertTrue(
            any(item["code"] == "FP_W_FONT_UNKNOWN" for item in report["warnings"])
        )

    # -- warnings / strict --------------------------------------------------
    def test_unknown_top_level_field_is_warning(self):
        plan = valid_plan()
        plan["future_field"] = True
        self.assertEqual(validate_plan(plan), [])
        report = validate_plan_detailed(plan)
        self.assertTrue(
            any(item["code"] == "FP_W_UNKNOWN_FIELD" for item in report["warnings"])
        )

    def test_unknown_element_field_is_warning(self):
        plan = valid_plan()
        plan["elements"][1]["z_index"] = 3
        report = validate_plan_detailed(plan)
        self.assertTrue(
            any(
                item["code"] == "FP_W_UNKNOWN_FIELD" and ".z_index" in item["path"]
                for item in report["warnings"]
            )
        )

    def test_strict_promotes_warnings(self):
        plan = valid_plan()
        plan["future_field"] = True
        errors = validate_plan(plan, strict=True)
        self.assertTrue(
            any(item.startswith("FP_W_UNKNOWN_FIELD") for item in errors), errors
        )
        report = validate_plan_detailed(plan, strict=True)
        self.assertFalse(report["ok"])
        self.assertTrue(report["errors"][0].get("promoted"))

    # -- loading ------------------------------------------------------------
    def test_load_plan_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(tmp, "plan.json", valid_plan())
            self.assertEqual(load_plan(path)["schema"], "fdsl/2.1")

    def test_load_plan_missing_file(self):
        with self.assertRaises(FDSchemaError) as ctx:
            load_plan("no-such-plan-file.json")
        self.assertEqual(ctx.exception.code, "FP_E_LOAD")

    def test_load_plan_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_text(tmp, "broken.json", "{not json")
            with self.assertRaises(FDSchemaError) as ctx:
                load_plan(path)
        self.assertEqual(ctx.exception.code, "FP_E_JSON")


if __name__ == "__main__":
    unittest.main()
