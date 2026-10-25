"""Stroke conversion: width→brush mapping, opacity, closed flags, fill+stroke."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _fixtures import codes, convert, elements  # noqa: E402

from fourier_prompt.svg_import import brush_preset_for_width  # noqa: E402


class BrushMappingCase(unittest.TestCase):
    def test_width_thresholds(self):
        expected = {
            0.5: "ink_fineliner",
            0.9: "ink_fineliner",
            1.0: "ink_gpen",
            2.2: "ink_gpen",
            2.3: "ink_brush_rough",
            5.5: "ink_brush_rough",
            5.6: "basic_round_hard",
            30.0: "basic_round_hard",
        }
        for width, preset in expected.items():
            with self.subTest(width=width):
                self.assertEqual(brush_preset_for_width(width), preset)

    def test_stroke_width_scales_with_transform(self):
        plan, _warnings, _stats = convert(
            '<path d="M 10 10 L 90 90" fill="none" stroke="black" '
            'stroke-width="2" transform="scale(2)"/>'
        )
        stroke = elements(plan, "stroke")[0]
        self.assertEqual(stroke["brush"]["width"], 4.0)
        self.assertEqual(stroke["brush"]["preset"], "ink_brush_rough")

    def test_closed_and_open_flags(self):
        plan, _warnings, _stats = convert(
            '<path d="M 10 10 H 90 V 90 Z" fill="none" stroke="black"/>'
            '<path d="M 110 110 L 190 190" fill="none" stroke="black"/>'
        )
        closed, opened = elements(plan, "stroke")
        self.assertTrue(closed["closed"])
        self.assertFalse(opened["closed"])

    def test_stroke_and_fill_opacity_accumulate(self):
        plan, _warnings, _stats = convert(
            '<g opacity="0.5">'
            '<rect x="0" y="0" width="50" height="50" fill="black" '
            'stroke="black" stroke-width="2" fill-opacity="0.8" stroke-opacity="0.4"/>'
            "</g>"
        )
        fill, stroke = elements(plan)
        self.assertAlmostEqual(fill["opacity"], 0.4, places=3)
        self.assertAlmostEqual(stroke["opacity"], 0.2, places=3)

    def test_fill_then_stroke_order(self):
        plan, _warnings, _stats = convert(
            '<path d="M 10 10 H 90 V 90 H 10 Z" fill="red" stroke="black" '
            'stroke-width="2"/>'
        )
        self.assertEqual([element["kind"] for element in plan["elements"]], ["fill", "stroke"])

    def test_forced_brush_preset_override(self):
        plan, _warnings, _stats = convert(
            '<path d="M 10 10 L 90 90" stroke="black" stroke-width="1" fill="none"/>',
            brush_preset="sumi_e",
        )
        self.assertEqual(elements(plan, "stroke")[0]["brush"]["preset"], "sumi_e")


class StrokeLimitCase(unittest.TestCase):
    def test_hairline_width_is_clamped(self):
        plan, warnings, _stats = convert(
            '<path d="M 10 10 L 90 90" stroke="black" stroke-width="0.02" fill="none"/>'
        )
        self.assertIn("SI_W_STROKE_WIDTH_CLAMPED", codes(warnings))
        self.assertEqual(elements(plan, "stroke")[0]["brush"]["width"], 0.1)


if __name__ == "__main__":
    unittest.main()
