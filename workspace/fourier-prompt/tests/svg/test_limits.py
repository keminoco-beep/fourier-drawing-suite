"""Limits and overflow policy: max_paths, point budget, decimation, config."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _fixtures import bbox, codes, convert, elements  # noqa: E402

from fourier_prompt.svg_import import SvgImportError  # noqa: E402


def _circle_polygon(count=50, radius=80, centre=(100.0, 100.0)):
    points = []
    for index in range(count):
        angle = 2.0 * math.pi * index / count
        points.append(
            f"{centre[0] + radius * math.cos(angle):.3f},"
            f"{centre[1] + radius * math.sin(angle):.3f}"
        )
    return " ".join(points)


class PathLimitCase(unittest.TestCase):
    BODY = (
        '<rect x="0" y="0" width="180" height="180" fill="black"/>'
        '<rect x="190" y="190" width="2" height="2" fill="black"/>'
    )

    def test_max_paths_error_is_fail_loud(self):
        with self.assertRaises(SvgImportError) as caught:
            convert(self.BODY, max_paths=1, on_overflow="error")
        self.assertEqual(caught.exception.code, "SI_E_PATH_LIMIT")

    def test_max_paths_trim_keeps_largest_and_warns(self):
        plan, warnings, stats = convert(self.BODY, max_paths=1, on_overflow="trim")
        self.assertIn("SI_W_PATH_LIMIT", codes(warnings))
        self.assertEqual(len(elements(plan)), 1)
        self.assertEqual(bbox(elements(plan)[0]["poly"]), (0.0, 0.0, 180.0, 180.0))
        self.assertEqual(stats["paths_dropped"], 1)
        self.assertEqual(stats["elements"], 1)

    def test_total_point_budget_error(self):
        body = (
            '<circle cx="60" cy="60" r="50" fill="black"/>'
            '<circle cx="140" cy="140" r="50" fill="black"/>'
        )
        with self.assertRaises(SvgImportError) as caught:
            convert(body, max_total_points=16, on_overflow="error")
        self.assertEqual(caught.exception.code, "SI_E_POINT_LIMIT")

    def test_total_point_budget_trim(self):
        body = (
            '<circle cx="60" cy="60" r="50" fill="black"/>'
            '<circle cx="140" cy="140" r="50" fill="black"/>'
        )
        plan, warnings, stats = convert(
            body, max_total_points=50, on_overflow="trim"
        )
        self.assertIn("SI_W_TOTAL_POINTS_LIMIT", codes(warnings))
        self.assertEqual(len(elements(plan)), 1)
        self.assertGreater(stats["paths_dropped"], 0)

    def test_points_per_path_decimation(self):
        body = f'<polygon points="{_circle_polygon()}" fill="black"/>'
        plan, warnings, _stats = convert(body, max_points_per_path=8)
        self.assertIn("SI_W_POINTS_DECIMATED", codes(warnings))
        self.assertLessEqual(len(elements(plan, "fill")[0]["poly"]), 8)
        self.assertGreaterEqual(len(elements(plan, "fill")[0]["poly"]), 3)

    def test_invalid_config_raises(self):
        for kwargs in (
            {"max_paths": 0},
            {"max_paths": 5000},
            {"max_colors": 65},
            {"max_points_per_path": 2},
            {"max_total_points": 8},
            {"on_overflow": "ignore"},
        ):
            with self.subTest(config=kwargs):
                with self.assertRaises(SvgImportError) as caught:
                    convert('<rect width="10" height="10" fill="black"/>', **kwargs)
                self.assertEqual(caught.exception.code, "SI_E_CONFIG")


class PlanShapeCase(unittest.TestCase):
    def test_generated_plan_validates_and_respects_schema_limits(self):
        from fourier_prompt.schema import validate_plan

        plan, _warnings, stats = convert(
            '<rect width="20" height="20" fill="black"/>'
            '<path d="M 10 10 L 50 90" stroke="black" fill="none"/>',
            max_paths=2,
        )
        self.assertEqual(validate_plan(plan), [])
        self.assertEqual(plan["schema"], "fdsl/2.1")
        self.assertGreaterEqual(plan["constraints"]["max_elements"], len(plan["elements"]))
        ids = [element["id"] for element in plan["elements"]]
        self.assertEqual(ids, [f"e{index + 1}" for index in range(len(ids))])
        self.assertEqual(stats["warnings"], 0)

    def test_include_fill_and_stroke_switches(self):
        body = '<rect width="50" height="50" fill="red" stroke="black" stroke-width="2"/>'
        fills_only, _warnings, _stats = convert(body, include_stroke=False)
        strokes_only, _warnings, _stats = convert(body, include_fill=False)
        self.assertEqual([element["kind"] for element in fills_only["elements"]], ["fill"])
        self.assertEqual([element["kind"] for element in strokes_only["elements"]], ["stroke"])


if __name__ == "__main__":
    unittest.main()
