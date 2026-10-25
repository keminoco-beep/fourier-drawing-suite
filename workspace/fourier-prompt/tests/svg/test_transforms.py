"""Transform + viewBox mapping (transform lists, nesting, preserveAspectRatio)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _fixtures import convert, elements, svg  # noqa: E402

from fourier_prompt.svg_import import parse_transform, svg_to_fdsl  # noqa: E402


class TransformCase(unittest.TestCase):
    def test_translate_and_scale_in_group(self):
        plan, warnings, _stats = convert(
            '<g transform="translate(50 20) scale(2)">'
            '<rect x="10" y="10" width="20" height="10" fill="black"/></g>'
        )
        self.assertEqual(warnings, [])
        self.assertEqual(
            elements(plan, "fill")[0]["poly"],
            [[70.0, 40.0], [110.0, 40.0], [110.0, 60.0], [70.0, 60.0]],
        )

    def test_rotate_about_point(self):
        plan, _warnings, _stats = convert(
            '<rect x="90" y="80" width="20" height="40" fill="black" '
            'transform="rotate(90 100 100)"/>'
        )
        poly = elements(plan, "fill")[0]["poly"]
        # 90 degrees clockwise: (90,80) -> (120,90), (90,120) -> (80,90)
        xs = [point[0] for point in poly]
        ys = [point[1] for point in poly]
        self.assertAlmostEqual(min(xs), 80.0, places=2)
        self.assertAlmostEqual(max(xs), 120.0, places=2)
        self.assertAlmostEqual(min(ys), 90.0, places=2)
        self.assertAlmostEqual(max(ys), 110.0, places=2)

    def test_matrix_and_skew(self):
        plan, _warnings, _stats = convert(
            '<rect x="0" y="0" width="20" height="10" fill="black" '
            'transform="matrix(1 0 0 1 10 5)"/>'
            '<rect x="0" y="100" width="10" height="10" fill="black" '
            'transform="skewX(45)"/>'
        )
        first, second = elements(plan, "fill")
        self.assertEqual(first["poly"][0], [10.0, 5.0])
        # skewX(45): x' = x + y => (0,100) -> (100,100), (10,110) -> (120,110)
        self.assertAlmostEqual(second["poly"][0][0], 100.0, places=2)
        self.assertAlmostEqual(second["poly"][2][0], 120.0, places=2)

    def test_nested_group_transforms_compose(self):
        plan, _warnings, _stats = convert(
            '<g transform="translate(10 10)"><g transform="scale(3)">'
            '<rect x="0" y="0" width="10" height="10" fill="black"/></g></g>'
        )
        self.assertEqual(elements(plan, "fill")[0]["poly"][0], [10.0, 10.0])
        self.assertEqual(elements(plan, "fill")[0]["poly"][2], [40.0, 40.0])

    def test_viewbox_offset_maps_to_canvas(self):
        document = svg(
            '<rect x="10" y="10" width="100" height="100" fill="black"/>',
            viewbox="10 10 100 100",
        )
        plan, _warnings, _stats = svg_to_fdsl(document, canvas=(200, 200))
        poly = elements(plan, "fill")[0]["poly"]
        self.assertEqual(poly[0], [0.0, 0.0])
        self.assertEqual(poly[2], [200.0, 200.0])

    def test_preserve_aspect_ratio_letterbox(self):
        document = svg(
            '<rect x="0" y="0" width="100" height="50" fill="black"/>',
            viewbox="0 0 100 50",
        )
        plan, _warnings, _stats = svg_to_fdsl(
            document, canvas=(200, 200)
        )
        poly = elements(plan, "fill")[0]["poly"]
        # uniform scale = 2, centred vertically at y = 50
        self.assertEqual(bbox(poly), (0.0, 50.0, 200.0, 150.0))

    def test_transform_parser_rejects_garbage(self):
        with self.assertRaises(Exception) as caught:
            parse_transform("translate(10 10) bogus")
        self.assertEqual(getattr(caught.exception, "code", None), "SI_E_TRANSFORM")


def bbox(points):
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


if __name__ == "__main__":
    unittest.main()
