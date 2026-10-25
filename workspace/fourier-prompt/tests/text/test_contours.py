"""Glyph outline extraction / flattening / hole classification tests (WP-K)."""

from __future__ import annotations

import math
import unittest

from fourier_prompt import fonts, text


class GlyphContourCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.font = fonts.load_font()

    def gid(self, char):
        return fonts.require_glyph(self.font, char)

    def test_contours_are_closed_and_finite(self):
        rings = text.glyph_contours(self.font, self.gid("你"), size_px=40)
        self.assertGreaterEqual(len(rings), 3)
        for ring in rings:
            self.assertGreaterEqual(len(ring), 3)
            self.assertNotEqual(ring[0], ring[-1], "closure must be implicit, not a duplicate point")
            for x, y in ring:
                self.assertTrue(math.isfinite(x) and math.isfinite(y))
        total = sum(len(ring) for ring in rings)
        self.assertGreater(total, 100, "bezier flattening produced too few points")

    def test_o_has_one_counter(self):
        rings = text.glyph_contours(self.font, self.gid("o"), size_px=40)
        self.assertEqual(len(rings), 2)
        classified = text._classify_rings(rings)
        self.assertEqual(len(classified), 1)
        self.assertEqual(len(classified[0]["holes"]), 1)

    def test_eight_has_two_counters(self):
        rings = text.glyph_contours(self.font, self.gid("8"), size_px=40)
        self.assertEqual(len(rings), 3)
        classified = text._classify_rings(rings)
        self.assertEqual(len(classified), 1)
        self.assertEqual(len(classified[0]["holes"]), 2)

    def test_overlapping_outers_are_not_mistaken_for_holes(self):
        # The packaged Noto subset draws "合" with several nested clockwise
        # contours: winding must win over naive nesting (else the 一 stroke
        # would punch a background notch into the glyph).
        rings = text.glyph_contours(self.font, self.gid("合"), size_px=40)
        self.assertGreaterEqual(len(rings), 4)
        classified = text._classify_rings(rings)
        self.assertEqual(sum(len(entry["holes"]) for entry in classified), 0)
        self.assertEqual(len(classified), len(rings))

    def test_flattening_density_scales_with_size(self):
        gid = self.gid("你")
        small = sum(len(ring) for ring in text.glyph_contours(self.font, gid, size_px=16))
        large = sum(len(ring) for ring in text.glyph_contours(self.font, gid, size_px=96))
        self.assertGreater(large, small * 2)

    def test_space_has_no_rings(self):
        self.assertEqual(text.glyph_contours(self.font, self.gid(" "), size_px=40), [])

    def test_invalid_glyph_id_fails(self):
        with self.assertRaises(text.TextError) as ctx:
            text.glyph_contours(self.font, 999_999)
        self.assertEqual(ctx.exception.code, "FP_E_TEXT_GLYPH")

    def test_contour_point_limit_is_guarded(self):
        from unittest import mock

        with mock.patch.object(text, "MAX_POLY_POINTS", 8):
            with self.assertRaises(text.TextError) as ctx:
                text.glyph_contours(self.font, self.gid("你"), size_px=40)
        self.assertEqual(ctx.exception.code, "FP_E_TEXT_GEOMETRY")


if __name__ == "__main__":
    unittest.main()
