"""``compile_plan`` text integration: auto/outline/legacy, error codes (WP-K)."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from _fixtures import FONT_PATH, single_text_plan, text_plan

from fourier_prompt.compile import CompileError, compile_plan, normalized_fbs


def _background_fill():
    return {
        "kind": "fill",
        "id": "e9",
        "layer": 0,
        "color": "#eeeeee",
        "poly": [[8, 8], [376, 8], [376, 248], [8, 248]],
    }


def _text_plan_without_as(font_id="source_han_sans_sc"):
    plan = single_text_plan()
    plan["elements"][0].pop("as")
    if font_id is None:
        plan["elements"][0].pop("font")
    else:
        plan["elements"][0]["font"]["id"] = font_id
    plan["elements"].insert(0, _background_fill())
    return plan


class CompileTextCase(unittest.TestCase):
    def test_auto_mode_skips_legacy_builtin_font_without_as(self):
        result = compile_plan(_text_plan_without_as("inter"), resolution=128)
        self.assertEqual(result["stats"]["text_skipped"], 1)
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertIn("FP_W_TEXT_SKIPPED", codes)

    def test_text_only_plan_with_legacy_font_still_empty(self):
        plan = single_text_plan()
        plan["elements"][0].pop("as")
        plan["elements"][0]["font"]["id"] = "inter"
        with self.assertRaises(CompileError) as ctx:
            compile_plan(plan, resolution=128)
        self.assertEqual(ctx.exception.code, "FP_E_COMPILE_EMPTY")

    def test_auto_mode_compiles_explicit_outline_text(self):
        result = compile_plan(text_plan(), resolution=256)
        stats = result["stats"]
        self.assertEqual(stats["text_skipped"], 0)
        self.assertGreaterEqual(stats["text"], 2)  # two elements + texts[] entry
        self.assertGreater(stats["text_fills"], 0)
        self.assertEqual(stats["records"], len(result["fbs"]["strokes"]))
        self.assertTrue(
            all(stroke.get("fill_role") == "region_fill" for stroke in result["fbs"]["strokes"])
        )
        serialized = normalized_fbs(result["fbs"]).lower()
        for needle in ("fonttools", "font-family", "font_family", "fontfile", '"font"'):
            self.assertNotIn(needle, serialized)

    def test_texts_entry_is_compiled(self):
        plan = single_text_plan()
        plan["elements"] = []
        plan["texts"] = [
            {
                "id": "t1",
                "as": "outline",
                "layer": 0,
                "text": "texts 数组",
                "font": {"size_px": 32, "color": "ink", "align": "center"},
                "box": [10, 10, 360, 160],
            }
        ]
        result = compile_plan(plan, resolution=128)
        self.assertEqual(result["stats"]["text"], 1)
        self.assertGreater(result["stats"]["text_fills"], 0)

    def test_auto_mode_compiles_default_font_without_as(self):
        result = compile_plan(_text_plan_without_as(None), resolution=128)
        self.assertEqual(result["stats"]["text"], 1)
        self.assertGreater(result["stats"]["text_fills"], 0)

    def test_outline_mode_forces_text_with_overridden_font(self):
        result = compile_plan(
            _text_plan_without_as("inter"), resolution=128, text_mode="outline", font=FONT_PATH
        )
        self.assertEqual(result["stats"]["text"], 1)
        self.assertGreater(result["stats"]["text_fills"], 0)

    def test_legacy_mode_skips_explicit_text(self):
        plan = text_plan()
        plan["elements"].insert(0, _background_fill())
        result = compile_plan(plan, resolution=128, text_mode="legacy")
        self.assertEqual(result["stats"]["text"], 0)
        self.assertGreater(sum(1 for warning in result["warnings"] if warning["code"] == "FP_W_TEXT_SKIPPED"), 0)

    def test_missing_glyph_is_a_compile_error(self):
        plan = single_text_plan("bad 🦄 glyph")
        with self.assertRaises(CompileError) as ctx:
            compile_plan(plan, resolution=128)
        self.assertEqual(ctx.exception.code, "FP_E_TEXT_GLYPH")
        self.assertIn("U+1F984", ctx.exception.message)

    def test_missing_font_is_a_compile_error(self):
        plan = single_text_plan()
        missing = Path(tempfile.gettempdir()) / "wp-k-no-such-font.ttf"
        with self.assertRaises(CompileError) as ctx:
            compile_plan(plan, resolution=128, font=missing)
        self.assertEqual(ctx.exception.code, "FP_E_TEXT_FONT")
        self.assertIn("no system-font fallback", ctx.exception.message)

    def test_unshipped_font_id_is_a_compile_error(self):
        plan = single_text_plan()
        plan["elements"][0]["font"]["id"] = "inter"
        with self.assertRaises(CompileError) as ctx:
            compile_plan(plan, resolution=128)
        self.assertEqual(ctx.exception.code, "FP_E_TEXT_FONT")
        self.assertIn("not shipped", ctx.exception.message)

    def test_glyph_mode_is_rejected(self):
        plan = single_text_plan()
        plan["elements"][0]["as"] = "glyph"
        with self.assertRaises(CompileError) as ctx:
            compile_plan(plan, resolution=128)
        self.assertEqual(ctx.exception.code, "FP_E_TEXT_MODE")

    def test_invalid_text_mode_is_rejected(self):
        with self.assertRaises(CompileError) as ctx:
            compile_plan(single_text_plan(), resolution=128, text_mode="bitmap")
        self.assertEqual(ctx.exception.code, "FP_E_COMPILE_TEXT")

    def test_text_fills_respect_layer_and_alpha(self):
        plan = text_plan()
        result = compile_plan(plan, resolution=256)
        styles = result["fbs"]["brush_styles"]
        self.assertGreaterEqual(len(styles), 2)  # ink, accent and the alpha=0.85 texts[] entry
        alphas = []
        for style in styles:
            alpha = style.get("alpha") or {}
            keys = alpha.get("keys") or []
            alphas.extend(float(entry[1]) for entry in keys)
        self.assertTrue(any(value < 0.9 for value in alphas), alphas)
        layers = {int(stroke.get("layer", 0)) for stroke in result["fbs"]["strokes"]}
        self.assertEqual(layers, {0})


if __name__ == "__main__":
    unittest.main()
