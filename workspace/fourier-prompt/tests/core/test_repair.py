"""Deterministic repair tests for FDSL v2.1 (WP-A core)."""

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

from _fixtures import clone, valid_plan  # noqa: E402

from fourier_prompt.repair import repair_plan  # noqa: E402
from fourier_prompt.schema import FDSchemaError, validate_plan  # noqa: E402


def warning_codes(warnings):
    return [item["code"] for item in warnings]


class RepairCase(unittest.TestCase):
    def assert_warning(self, warnings, code):
        self.assertIn(code, warning_codes(warnings), warnings)

    def repaired(self, plan):
        return repair_plan(plan)[0]

    # -- geometry -----------------------------------------------------------
    def test_clips_points_into_canvas(self):
        plan = valid_plan()
        plan["elements"][2]["points"] = [[-20, 300], [400, -5]]
        fixed, warnings = repair_plan(plan)
        self.assertEqual(fixed["elements"][2]["points"], [[0.0, 256.0], [256.0, 0.0]])
        self.assert_warning(warnings, "FP_W_POINT_CLIPPED")
        self.assertEqual(validate_plan(fixed), [])

    def test_removes_duplicate_consecutive_points(self):
        plan = valid_plan()
        plan["elements"][2]["points"] = [[10, 10], [10, 10], [20, 20]]
        fixed, warnings = repair_plan(plan)
        self.assertEqual(fixed["elements"][2]["points"], [[10.0, 10.0], [20.0, 20.0]])
        self.assert_warning(warnings, "FP_W_POINT_DEDUP")

    def test_fill_polygon_stays_implicitly_closed(self):
        # Fill polygons are implicitly closed; repair must not rewrite them.
        plan = valid_plan()
        original = clone(plan["elements"][0]["poly"])
        fixed, warnings = repair_plan(plan)
        self.assertEqual(fixed["elements"][0]["poly"], original)
        self.assertNotIn("FP_W_POLY_CLOSED", warning_codes(warnings))

    def test_closed_stroke_is_closed(self):
        plan = valid_plan()
        plan["elements"][2]["closed"] = True
        fixed, warnings = repair_plan(plan)
        points = fixed["elements"][2]["points"]
        self.assertEqual(points[0], points[-1])
        self.assert_warning(warnings, "FP_W_POLY_CLOSED")

    def test_non_finite_point_is_dropped(self):
        plan = valid_plan()
        plan["elements"][2]["points"] = [[10, 10], [float("nan"), 5], [20, 20]]
        fixed, warnings = repair_plan(plan)
        self.assertEqual(len(fixed["elements"][2]["points"]), 2)
        self.assert_warning(warnings, "FP_W_NONFINITE_DROPPED")

    def test_non_finite_gen_param_replaced(self):
        plan = valid_plan()
        plan["elements"][1]["params"]["phase"] = float("inf")
        fixed, warnings = repair_plan(plan)
        self.assertEqual(fixed["elements"][1]["params"]["phase"], 0.0)
        self.assert_warning(warnings, "FP_W_NONFINITE_REPLACED")

    # -- colours / ranges ---------------------------------------------------
    def test_short_hex_colour_is_expanded(self):
        plan = valid_plan()
        plan["elements"][1]["brush"]["color"] = "#ABC"
        fixed, warnings = repair_plan(plan)
        self.assertEqual(fixed["elements"][1]["brush"]["color"], "#AABBCC")
        self.assert_warning(warnings, "FP_W_COLOR_NORMALIZED")

    def test_valid_uppercase_hex_is_kept(self):
        plan = valid_plan()
        plan["elements"][1]["brush"]["color"] = "#E8C37A"
        fixed, warnings = repair_plan(plan)
        self.assertEqual(fixed["elements"][1]["brush"]["color"], "#E8C37A")
        self.assertEqual(warnings, [])

    def test_bare_hex_colour_gets_hash(self):
        plan = valid_plan()
        plan["elements"][1]["brush"]["color"] = "E8C37A"
        fixed, warnings = repair_plan(plan)
        self.assertEqual(fixed["elements"][1]["brush"]["color"], "#e8c37a")
        self.assert_warning(warnings, "FP_W_COLOR_NORMALIZED")

    def test_unknown_colour_name_raises(self):
        plan = valid_plan()
        plan["elements"][1]["brush"]["color"] = "ultramarine"
        with self.assertRaises(FDSchemaError) as ctx:
            repair_plan(plan)
        self.assertEqual(ctx.exception.code, "FP_E_COLOR_INVALID")

    def test_alpha_and_width_are_clamped(self):
        plan = valid_plan()
        plan["elements"][1]["brush"]["alpha"] = 5
        plan["elements"][1]["brush"]["width"] = 0
        fixed, warnings = repair_plan(plan)
        self.assertEqual(fixed["elements"][1]["brush"]["alpha"], 1.0)
        self.assertEqual(fixed["elements"][1]["brush"]["width"], 0.1)
        self.assert_warning(warnings, "FP_W_ALPHA_CLAMPED")
        self.assert_warning(warnings, "FP_W_WIDTH_CLAMPED")

    def test_width_curve_is_normalised(self):
        plan = valid_plan()
        plan["elements"][2]["brush"]["width"] = [[0.5, 2.0], [0.0, 4.0]]
        fixed, warnings = repair_plan(plan)
        curve = fixed["elements"][2]["brush"]["width"]
        self.assertEqual(curve, [[0.0, 4.0], [0.5, 2.0], [1.0, 2.0]])
        self.assert_warning(warnings, "FP_W_WIDTH_NORMALIZED")

    def test_background_channels_are_rounded(self):
        plan = valid_plan()
        plan["canvas"]["background"] = [255.4, -3, 300]
        fixed, warnings = repair_plan(plan)
        self.assertEqual(fixed["canvas"]["background"], [255, 0, 255])
        self.assert_warning(warnings, "FP_W_BACKGROUND_NORMALIZED")

    # -- identity / layers --------------------------------------------------
    def test_duplicate_ids_are_reassigned(self):
        plan = valid_plan()
        plan["elements"][2]["id"] = "e2"
        fixed, warnings = repair_plan(plan)
        ids = [element["id"] for element in fixed["elements"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids[0], "e1")
        self.assertEqual(ids[1], "e2")
        self.assert_warning(warnings, "FP_W_ID_REASSIGNED")

    def test_missing_layer_is_defaulted(self):
        plan = valid_plan()
        del plan["elements"][2]["layer"]
        fixed, warnings = repair_plan(plan)
        self.assertEqual(fixed["elements"][2]["layer"], 0)
        self.assert_warning(warnings, "FP_W_LAYER_REPAIRED")

    def test_tags_are_truncated_to_eight(self):
        plan = valid_plan()
        plan["elements"][1]["tags"] = [f"tag{i}" for i in range(10)]
        fixed, warnings = repair_plan(plan)
        self.assertEqual(len(fixed["elements"][1]["tags"]), 8)
        self.assert_warning(warnings, "FP_W_TAGS_TRUNCATED")

    # -- unknown enums must not be guessed ----------------------------------
    def test_unknown_preset_raises(self):
        plan = valid_plan()
        plan["elements"][1]["brush"]["preset"] = "magic_pen"
        with self.assertRaises(FDSchemaError) as ctx:
            repair_plan(plan)
        self.assertEqual(ctx.exception.code, "FP_E_BRUSH_PRESET_UNKNOWN")

    def test_unknown_gen_raises(self):
        plan = valid_plan()
        plan["elements"][1]["gen"] = "unicorn"
        with self.assertRaises(FDSchemaError) as ctx:
            repair_plan(plan)
        self.assertEqual(ctx.exception.code, "FP_E_GEN_UNKNOWN")

    def test_unknown_brush_shape_raises(self):
        plan = valid_plan()
        plan["elements"][1]["brush"]["shape"] = "spray9"
        with self.assertRaises(FDSchemaError) as ctx:
            repair_plan(plan)
        self.assertEqual(ctx.exception.code, "FP_E_BRUSH_SHAPE")

    def test_broken_canvas_raises(self):
        plan = valid_plan()
        del plan["canvas"]["width"]
        with self.assertRaises(FDSchemaError) as ctx:
            repair_plan(plan)
        self.assertEqual(ctx.exception.code, "FP_E_CANVAS_TYPE")

    def test_bad_schema_raises(self):
        plan = valid_plan()
        plan["schema"] = "fdsl/9"
        with self.assertRaises(FDSchemaError) as ctx:
            repair_plan(plan)
        self.assertEqual(ctx.exception.code, "FP_E_SCHEMA_VERSION")

    # -- strict / determinism ----------------------------------------------
    def test_strict_valid_plan_is_unchanged(self):
        plan = valid_plan()
        fixed, warnings = repair_plan(plan, strict=True)
        self.assertEqual(fixed, plan)
        self.assertEqual(warnings, [])

    def test_strict_refuses_repairable_plan(self):
        plan = valid_plan()
        plan["elements"][2]["points"] = [[-5, 10], [20, 20]]
        with self.assertRaises(FDSchemaError) as ctx:
            repair_plan(plan, strict=True)
        self.assertEqual(ctx.exception.code, "FP_E_STRICT")

    def test_strict_refuses_warnings(self):
        plan = valid_plan()
        plan["future_field"] = True
        with self.assertRaises(FDSchemaError) as ctx:
            repair_plan(plan, strict=True)
        self.assertEqual(ctx.exception.code, "FP_E_STRICT")

    def test_repair_is_idempotent(self):
        plan = valid_plan()
        plan["elements"][2]["points"] = [[-5, 10], [10, 10], [10, 10], [300, 20]]
        plan["elements"][1]["brush"]["color"] = "#ABC"
        plan["elements"][1]["brush"]["alpha"] = 3
        fixed, warnings = repair_plan(plan)
        self.assertTrue(warnings)
        fixed_again, warnings_again = repair_plan(fixed)
        self.assertEqual(fixed_again, fixed)
        self.assertEqual(warnings_again, [])

    def test_input_plan_is_not_mutated(self):
        plan = valid_plan()
        plan["elements"][2]["points"] = [[-5, 10], [10, 10]]
        snapshot = clone(plan)
        repair_plan(plan)
        self.assertEqual(plan, snapshot)

    def test_clean_plan_has_no_warnings(self):
        fixed, warnings = repair_plan(valid_plan())
        self.assertEqual(warnings, [])
        self.assertEqual(validate_plan(fixed), [])


if __name__ == "__main__":
    unittest.main()
