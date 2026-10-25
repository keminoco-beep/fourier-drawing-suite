"""Fill rings → FDSL fill poly + holes (evenodd / nonzero, islands, holes cap)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _fixtures import codes, convert, elements  # noqa: E402


class FillHoleCase(unittest.TestCase):
    def test_evenodd_donut_has_one_hole(self):
        plan, warnings, _stats = convert(
            '<path d="M 20 20 H 180 V 180 H 20 Z M 70 70 H 130 V 130 H 70 Z" '
            'fill="#3366cc" fill-rule="evenodd"/>'
        )
        self.assertEqual(codes(warnings), [])
        fills = elements(plan, "fill")
        self.assertEqual(len(fills), 1)
        self.assertEqual(len(fills[0]["poly"]), 4)
        self.assertEqual(len(fills[0]["holes"]), 1)
        self.assertEqual(len(fills[0]["holes"][0]), 4)

    def test_nonzero_opposite_winding_is_a_hole(self):
        plan, _warnings, _stats = convert(
            '<path d="M 20 20 H 180 V 180 H 20 Z M 70 70 V 130 H 130 V 70 Z" '
            'fill="blue"/>'
        )
        fills = elements(plan, "fill")
        self.assertEqual(len(fills), 1)
        self.assertEqual(len(fills[0]["holes"]), 1)

    def test_nonzero_same_winding_is_an_island(self):
        plan, warnings, _stats = convert(
            '<path d="M 20 20 H 180 V 180 H 20 Z M 70 70 H 130 V 130 H 70 Z" '
            'fill="blue"/>'
        )
        self.assertEqual(codes(warnings), [])
        fills = elements(plan, "fill")
        # A same-direction nested ring is filled under nonzero; the importer
        # paints it as a second region (same colour, same visual result).
        self.assertEqual(len(fills), 2)
        self.assertEqual(fills[1]["poly"][0], [70.0, 70.0])

    def test_two_holes_attach_to_one_outer(self):
        plan, _warnings, _stats = convert(
            '<path d="M 10 10 H 190 V 190 H 10 Z '
            'M 40 40 H 70 V 70 H 40 Z M 120 120 H 160 V 160 H 120 Z" '
            'fill-rule="evenodd" fill="black"/>'
        )
        fills = elements(plan, "fill")
        self.assertEqual(len(fills), 1)
        self.assertEqual(len(fills[0]["holes"]), 2)

    def test_nested_island_inside_hole(self):
        plan, _warnings, _stats = convert(
            '<path d="M 10 10 H 190 V 190 H 10 Z '
            'M 40 40 H 160 V 160 H 40 Z '
            'M 70 70 H 130 V 130 H 70 Z" fill-rule="evenodd" fill="black"/>'
        )
        fills = elements(plan, "fill")
        self.assertEqual(len(fills), 2)
        self.assertEqual(len(fills[0]["holes"]), 1)
        self.assertEqual(len(fills[1].get("holes") or []), 0)

    def test_hole_limit_is_enforced_with_warning(self):
        holes = " ".join(
            f"M {20 + (index % 8) * 20} {20 + (index // 8) * 20} h 6 v 6 h -6 Z"
            for index in range(70)
        )
        plan, warnings, _stats = convert(
            f'<path d="M 5 5 H 195 V 195 H 5 Z {holes}" fill-rule="evenodd" fill="black"/>'
        )
        self.assertIn("SI_W_HOLE_LIMIT", codes(warnings))
        fills = elements(plan, "fill")
        total_holes = sum(len(fill.get("holes") or []) for fill in fills)
        self.assertLessEqual(total_holes, 64)

    def test_tiny_ring_is_degenerate(self):
        plan, warnings, _stats = convert(
            '<path d="M 10 10 H 10.1 V 10.1 H 10 Z" fill="black"/>'
            '<rect width="20" height="20" fill="black"/>'
        )
        self.assertIn("SI_W_DEGENERATE_SKIPPED", codes(warnings))
        self.assertEqual(len(elements(plan)), 1)


if __name__ == "__main__":
    unittest.main()
