"""Shape primitives: rect/circle/ellipse/polygon/polyline/line + rounded rect."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _fixtures import bbox, codes, convert, elements  # noqa: E402


class ShapePrimitiveCase(unittest.TestCase):
    def test_rect_is_axis_aligned_polygon(self):
        plan, warnings, _stats = convert(
            '<rect x="10" y="20" width="80" height="60" fill="#336699"/>'
        )
        self.assertEqual(codes(warnings), [])
        poly = elements(plan, "fill")[0]["poly"]
        self.assertEqual(len(poly), 4)
        self.assertEqual(bbox(poly), (10.0, 20.0, 90.0, 80.0))

    def test_circle_and_ellipse_approximations(self):
        plan, _warnings, _stats = convert(
            '<circle cx="100" cy="100" r="80" fill="black"/>'
            '<ellipse cx="100" cy="100" rx="60" ry="30" fill="black"/>'
        )
        circle, ellipse = elements(plan, "fill")
        circle_box = bbox(circle["poly"])
        ellipse_box = bbox(ellipse["poly"])
        for actual, expected in zip(circle_box, (20.0, 20.0, 180.0, 180.0)):
            self.assertAlmostEqual(actual, expected, delta=1.0)
        for actual, expected in zip(ellipse_box, (40.0, 70.0, 160.0, 130.0)):
            self.assertAlmostEqual(actual, expected, delta=1.0)
        self.assertGreater(len(circle["poly"]), 20)
        self.assertGreater(len(ellipse["poly"]), 20)

    def test_polygon_fill_and_polyline_stroke(self):
        plan, _warnings, _stats = convert(
            '<polygon points="20,20 180,20 100,160" fill="#aa0000"/>'
            '<polyline points="20,180 100,140 180,180" fill="none" stroke="#000000"/>'
        )
        fills = elements(plan, "fill")
        strokes = elements(plan, "stroke")
        self.assertEqual(len(fills), 1)
        self.assertEqual(len(strokes), 1)
        self.assertFalse(strokes[0]["closed"])
        self.assertEqual(len(strokes[0]["points"]), 3)

    def test_line_requires_stroke(self):
        plan, _warnings, _stats = convert(
            '<line x1="10" y1="10" x2="100" y2="50" stroke="black" stroke-width="3"/>'
        )
        stroke = elements(plan, "stroke")[0]
        self.assertEqual(stroke["points"], [[10.0, 10.0], [100.0, 50.0]])
        self.assertEqual(stroke["brush"]["preset"], "ink_brush_rough")

    def test_rounded_rect_is_approximated_with_warning(self):
        plan, warnings, _stats = convert(
            '<rect x="0" y="0" width="100" height="50" rx="10" ry="10" fill="black"/>'
        )
        self.assertIn("SI_W_ROUNDED_RECT_APPROX", codes(warnings))
        self.assertEqual(len(elements(plan, "fill")[0]["poly"]), 4)

    def test_zero_size_shapes_are_skipped(self):
        plan, warnings, _stats = convert(
            '<rect x="0" y="0" width="0" height="50" fill="black"/>'
            '<circle cx="5" cy="5" r="0" fill="black"/>'
            '<rect x="10" y="10" width="20" height="20" fill="black"/>'
        )
        self.assertEqual(len(elements(plan)), 1)
        self.assertEqual(codes(warnings).count("SI_W_DEGENERATE_SKIPPED"), 2)

    def test_nested_groups_keep_document_order(self):
        plan, _warnings, _stats = convert(
            '<g><rect x="0" y="0" width="10" height="10" fill="red"/></g>'
            '<rect x="20" y="0" width="10" height="10" fill="blue"/>'
        )
        self.assertEqual(len(elements(plan)), 2)
        self.assertEqual(len(plan["layers"]), 2)  # base + the top-level group


if __name__ == "__main__":
    unittest.main()
