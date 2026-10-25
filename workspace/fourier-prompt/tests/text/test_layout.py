"""Layout tests: newlines, alignment, box wrapping, fail-loud glyphs (WP-K)."""

from __future__ import annotations

import unittest

from fourier_prompt import fonts, text


class LayoutCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.font = fonts.load_font()

    def test_explicit_newline_creates_lines(self):
        first_line = "你好，Fourier! 2026"
        glyphs = text.layout_text(
            first_line + "\n第二行", self.font, size_px=32, box=[0, 0, 400, 200], line_height=1.25
        )
        self.assertEqual([glyph["line"] for glyph in glyphs[: len(first_line)]], [0] * len(first_line))
        self.assertEqual({glyph["line"] for glyph in glyphs}, {0, 1})
        first_line_y = min(glyph["y"] for glyph in glyphs if glyph["line"] == 0)
        second_line_y = min(glyph["y"] for glyph in glyphs if glyph["line"] == 1)
        self.assertAlmostEqual(second_line_y - first_line_y, 32 * 1.25, places=6)

    def test_alignment_shifts_first_glyph(self):
        box = [10, 10, 300, 120]
        text_value = "align me"
        left = text.layout_text(text_value, self.font, size_px=24, box=box, align="left")
        center = text.layout_text(text_value, self.font, size_px=24, box=box, align="center")
        right = text.layout_text(text_value, self.font, size_px=24, box=box, align="right")
        self.assertAlmostEqual(left[0]["x"], 10.0)
        self.assertGreater(center[0]["x"], left[0]["x"])
        self.assertGreater(right[0]["x"], center[0]["x"])
        width = sum(glyph["advance"] for glyph in left)
        self.assertAlmostEqual(right[0]["x"], 10 + 300 - width, places=5)

    def test_box_wraps_long_text(self):
        box = [0, 0, 150, 300]
        glyphs = text.layout_text("你好，Fourier! 2026 中文自动换行", self.font, size_px=28, box=box)
        lines = {}
        for glyph in glyphs:
            lines.setdefault(glyph["line"], []).append(glyph)
        self.assertGreater(len(lines), 1)
        for line in lines.values():
            width = sum(glyph["advance"] for glyph in line)
            self.assertLessEqual(width, box[2] + 0.5)

    def test_missing_glyph_fails_with_code(self):
        with self.assertRaises(text.TextError) as ctx:
            text.layout_text("emoji 🦄", self.font, size_px=24)
        self.assertEqual(ctx.exception.code, "FP_E_TEXT_GLYPH")
        self.assertIn("U+1F984", ctx.exception.message)

    def test_tab_and_control_characters_fail(self):
        for value in ("a\tb", "a\x01b"):
            with self.subTest(value=value):
                with self.assertRaises(text.TextError) as ctx:
                    text.layout_text(value, self.font, size_px=24)
                self.assertEqual(ctx.exception.code, "FP_E_TEXT_CHAR")

    def test_empty_or_bad_size_fails(self):
        with self.assertRaises(text.TextError):
            text.layout_text("   ", self.font, size_px=24)
        for size in (0, -1, float("nan"), 5000):
            with self.subTest(size=size):
                with self.assertRaises(text.TextError):
                    text.layout_text("ok", self.font, size_px=size)

    def test_advance_matches_hmtx(self):
        glyphs = text.layout_text("Fourier", self.font, size_px=40)
        for glyph in glyphs:
            expected = self.font.advance_units(glyph["glyph_id"]) * 40.0 / self.font.upem
            self.assertAlmostEqual(glyph["advance"], expected, places=9)
            self.assertGreaterEqual(glyph["advance"], 0.0)

    def test_line_height_is_validated(self):
        with self.assertRaises(text.TextError):
            text.layout_text("ok", self.font, size_px=24, line_height=0.1)
        with self.assertRaises(text.TextError):
            text.layout_text("ok", self.font, size_px=24, align="justify")


if __name__ == "__main__":
    unittest.main()
