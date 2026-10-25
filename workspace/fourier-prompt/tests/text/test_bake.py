"""Canonical fill baking tests: holes, colour/alpha, transform, overflow (WP-K)."""

from __future__ import annotations

import unittest

from fourier_prompt import fonts, text


class BakeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.font = fonts.load_font()
        cls.canvas = {"width": 384, "height": 256}

    def element(self, value, **overrides):
        element = {
            "kind": "text",
            "id": "e1",
            "layer": 0,
            "as": "outline",
            "text": value,
            "font": {"size_px": 48, "color": "ink", "align": "left", "alpha": 0.9},
            "box": [20, 20, 340, 200],
            "opacity": 0.5,
        }
        element.update(overrides)
        return element

    def test_o_bakes_outer_and_hole(self):
        fills = text.text_fills(self.element("o"), self.font, canvas=self.canvas)
        self.assertEqual(len(fills), 1)
        fill = fills[0]
        self.assertEqual(fill["kind"], "fill")
        self.assertGreaterEqual(len(fill["poly"]), 20)
        self.assertEqual(len(fill["holes"]), 1)
        self.assertGreaterEqual(len(fill["holes"][0]), 10)
        self.assertEqual(fill["layer"], 0)
        self.assertAlmostEqual(fill["opacity"], 0.45, places=6)  # alpha 0.9 * opacity 0.5
        self.assertEqual(fill["color"], "ink")

    def test_he_has_no_false_holes(self):
        fills = text.text_fills(self.element("合"), self.font, canvas=self.canvas)
        self.assertGreaterEqual(len(fills), 4)
        self.assertEqual(sum(len(fill["holes"]) for fill in fills), 0)

    def test_transform_rotates_whole_block(self):
        plain = text.text_fills(self.element("你好 Fourier"), self.font, canvas=self.canvas)
        rotated = text.text_fills(
            self.element("你好 Fourier", transform={"center": [192, 128], "rotate": 30}),
            self.font,
            canvas=self.canvas,
        )
        self.assertEqual(len(plain), len(rotated))

        def bounds(fills):
            xs = [x for fill in fills for x, _ in fill["poly"]]
            ys = [y for fill in fills for _, y in fill["poly"]]
            return min(xs), min(ys), max(xs), max(ys)

        self.assertNotEqual([round(v, 3) for v in bounds(plain)], [round(v, 3) for v in bounds(rotated)])

    def test_transform_scale_moves_points(self):
        plain = text.text_fills(self.element("o"), self.font, canvas=self.canvas)
        scaled = text.text_fills(
            self.element("o", transform={"center": [192, 128], "scale": 2.0}),
            self.font,
            canvas=self.canvas,
        )
        plain_x = [x for x, _ in plain[0]["poly"]]
        scaled_x = [x for x, _ in scaled[0]["poly"]]
        self.assertGreater(max(scaled_x) - min(scaled_x), 1.8 * (max(plain_x) - min(plain_x)))

    def test_unsupported_skew_fails(self):
        with self.assertRaises(text.TextError) as ctx:
            text.text_fills(self.element("o", transform={"skew": 10}), self.font, canvas=self.canvas)
        self.assertEqual(ctx.exception.code, "FP_E_TEXT_TRANSFORM")

    def test_overflow_warning_and_no_silent_clipping(self):
        warnings = []
        fills = text.text_fills(
            self.element("This line is far too wide for a tiny box", box=[0, 0, 40, 30]),
            self.font,
            canvas=self.canvas,
            warnings=warnings,
        )
        self.assertGreater(len(fills), 0)
        self.assertTrue(any(warning["code"] == "FP_W_TEXT_OVERFLOW" for warning in warnings))

    def test_missing_glyph_fails_loud(self):
        with self.assertRaises(text.TextError) as ctx:
            text.text_fills(self.element("🦄"), self.font, canvas=self.canvas)
        self.assertEqual(ctx.exception.code, "FP_E_TEXT_GLYPH")

    def test_default_box_from_canvas(self):
        element = self.element("Hi")
        element.pop("box")
        fills = text.text_fills(element, self.font, canvas=self.canvas)
        self.assertGreater(len(fills), 0)
        self.assertGreater(min(point[0] for fill in fills for point in fill["poly"]), 0.0)


if __name__ == "__main__":
    unittest.main()
