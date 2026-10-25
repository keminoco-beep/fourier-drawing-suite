"""Path-data coverage: every command, relative/absolute, implicit repeats, arcs."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _fixtures import bbox, codes, convert, elements  # noqa: E402

from fourier_prompt.svg_import import SvgImportError, parse_path_data  # noqa: E402


class PathCommandCase(unittest.TestCase):
    def test_all_commands_produce_one_closed_fill(self):
        plan, warnings, stats = convert(
            '<path d="M10 10 L40 10 H70 V40 C80 40 90 50 90 60 '
            'S100 80 110 80 Q120 80 130 70 T150 60 A10 10 0 0 1 160 70 Z" '
            'fill="#123456"/>'
        )
        self.assertEqual(codes(warnings), [])
        fills = elements(plan, "fill")
        self.assertEqual(len(fills), 1)
        self.assertGreater(len(fills[0]["poly"]), 20)
        self.assertGreaterEqual(stats["points"], len(fills[0]["poly"]))

    def test_relative_and_implicit_lineto(self):
        plan, _warnings, _stats = convert(
            '<path d="m 10 10 20 0 0 20 h -20 z" fill="black"/>'
        )
        poly = elements(plan, "fill")[0]["poly"]
        self.assertEqual(bbox(poly), (10.0, 10.0, 30.0, 30.0))
        # Z closes the ring: the duplicate closing vertex is dropped.
        self.assertEqual(len(poly), 4)

    def test_multiple_subpaths_become_multiple_fills(self):
        plan, _warnings, _stats = convert(
            '<path d="M10 10 H60 V60 H10 Z M120 120 H190 V190 H120 Z" fill="black"/>'
        )
        fills = elements(plan, "fill")
        self.assertEqual(len(fills), 2)
        self.assertEqual(bbox(fills[0]["poly"]), (10.0, 10.0, 60.0, 60.0))
        self.assertEqual(bbox(fills[1]["poly"]), (120.0, 120.0, 190.0, 190.0))

    def test_arc_approximation_keeps_endpoints(self):
        plan, _warnings, _stats = convert(
            '<path d="M 10 100 A 90 90 0 0 1 190 100 Z" fill="black"/>'
        )
        poly = elements(plan, "fill")[0]["poly"]
        self.assertGreater(len(poly), 12)
        # Top semi-circle: the lowest y stays near 10, the ends are exact.
        self.assertAlmostEqual(min(point[1] for point in poly), 10.0, places=1)
        self.assertAlmostEqual(poly[0][0], 10.0, places=2)

    def test_degenerate_arc_falls_back_to_line(self):
        subpaths = parse_path_data("M 10 10 A 0 0 0 0 0 50 50", tolerance=0.35)
        self.assertEqual(subpaths[0]["points"][-1], (50.0, 50.0))
        self.assertFalse(subpaths[0]["closed"])

    def test_bad_path_data_raises(self):
        for data in ("M 10", "M 10 10 X 20 20", "L 10 10"):
            with self.subTest(data=data):
                with self.assertRaises(SvgImportError) as caught:
                    parse_path_data(data)
                self.assertEqual(caught.exception.code, "SI_E_PATH_DATA")

    def test_empty_path_is_ignored_with_warning(self):
        plan, warnings, _stats = convert(
            '<path d="" fill="black"/><rect width="20" height="20" fill="black"/>'
        )
        self.assertIn("SI_W_DEGENERATE_SKIPPED", codes(warnings))
        self.assertEqual(len(elements(plan)), 1)


if __name__ == "__main__":
    unittest.main()
