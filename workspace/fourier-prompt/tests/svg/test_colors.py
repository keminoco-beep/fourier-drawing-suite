"""Colour parsing, palette de-duplication and quantisation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _fixtures import codes, convert, element_rgb, elements  # noqa: E402

from fourier_prompt.svg_import import _parse_color  # noqa: E402


class ColorParsingCase(unittest.TestCase):
    def test_hex_short_long_and_alpha(self):
        plan, _warnings, _stats = convert(
            '<rect width="10" height="10" fill="#abc"/>'
            '<rect x="20" width="10" height="10" fill="#112233"/>'
            '<rect x="40" width="10" height="10" fill="#11223344"/>'
        )
        fills = elements(plan, "fill")
        self.assertEqual(element_rgb(plan, fills[0]), (0xAA, 0xBB, 0xCC))
        self.assertEqual(element_rgb(plan, fills[1]), (0x11, 0x22, 0x33))
        self.assertEqual(element_rgb(plan, fills[2]), (0x11, 0x22, 0x33))
        self.assertAlmostEqual(fills[2]["opacity"], 0x44 / 255.0, places=3)
        # Same RGB -> one palette entry, alpha stays per element.
        self.assertEqual(len(plan["palette"]), 2)

    def test_named_rgb_rgba_and_current_color(self):
        plan, warnings, _stats = convert(
            '<rect width="10" height="10" fill="red"/>'
            '<rect x="20" width="10" height="10" fill="rgb(0,128,0)"/>'
            '<rect x="40" width="10" height="10" fill="rgba(0,0,255,0.5)"/>'
            '<rect x="60" width="10" height="10" color="#123456" fill="currentColor"/>'
        )
        self.assertEqual(codes(warnings), [])
        fills = elements(plan, "fill")
        self.assertEqual(element_rgb(plan, fills[0]), (255, 0, 0))
        self.assertEqual(element_rgb(plan, fills[1]), (0, 128, 0))
        self.assertEqual(element_rgb(plan, fills[2]), (0, 0, 255))
        self.assertAlmostEqual(fills[2]["opacity"], 0.5, places=3)
        self.assertEqual(element_rgb(plan, fills[3]), (0x12, 0x34, 0x56))

    def test_color_helpers(self):
        cases = {
            "#fff": (255, 255, 255, 1.0),
            "#00000080": (0, 0, 0, 128 / 255.0),
            "rebeccapurple": (0x66, 0x33, 0x99, 1.0),
            "rgb(100%, 50%, 0%)": (255, 128, 0, 1.0),
            "rgba(1, 2, 3, 0.25)": (1, 2, 3, 0.25),
        }
        for text, expected in cases.items():
            with self.subTest(colour=text):
                self.assertEqual(_parse_color(text), expected)
        for text in ("not-a-colour", "hsl(0, 100%, 50%)", "none"):
            with self.subTest(colour=text):
                self.assertIsNone(_parse_color(text))

    def test_palette_quantises_when_limit_reached(self):
        plan, warnings, stats = convert(
            '<rect width="10" height="10" fill="#ff0000"/>'
            '<rect x="20" width="10" height="10" fill="#00ff00"/>'
            '<rect x="40" width="10" height="10" fill="#0000ff"/>',
            max_colors=2,
        )
        self.assertIn("SI_W_COLOR_QUANTIZED", codes(warnings))
        self.assertLessEqual(len(plan["palette"]), 2)
        self.assertEqual(stats["quantized_colors"], 1)
        fills = elements(plan, "fill")
        self.assertEqual(element_rgb(plan, fills[2]), (255, 0, 0))  # nearest tie → earliest

    def test_unsupported_paint_warns_and_falls_back_to_black(self):
        plan, warnings, _stats = convert(
            '<rect width="10" height="10" fill="lab(50% 0 0)"/>'
        )
        self.assertIn("SI_W_UNSUPPORTED_PAINT", codes(warnings))
        self.assertEqual(element_rgb(plan, elements(plan, "fill")[0]), (0, 0, 0))

    def test_palette_names_are_schema_safe(self):
        plan, _warnings, _stats = convert(
            '<rect width="10" height="10" fill="#010203"/>'
            '<rect x="20" width="10" height="10" fill="#040506"/>'
        )
        names = list(plan["palette"])
        self.assertEqual(names, ["c0", "c1"])
        self.assertTrue(all(name.startswith("c") for name in names))


if __name__ == "__main__":
    unittest.main()
