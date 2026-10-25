"""Planner unit tests (WP-N): keywords, colours, text, style, seed, failures."""

from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for candidate in (str(HERE), str(ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from fourier_prompt import prompts  # noqa: E402
from fourier_prompt.compile import compile_plan  # noqa: E402
from fourier_prompt.planner import (  # noqa: E402
    PlannerError,
    plan_hash,
    plan_prompt,
    plan_summary,
)
from fourier_prompt.render_fast import render  # noqa: E402
from fourier_prompt.schema import validate_plan  # noqa: E402


def codes(warnings):
    return [warning["code"] for warning in warnings]


class SubjectAndColorCase(unittest.TestCase):
    def test_chinese_cat_keywords(self):
        plan, warnings = plan_prompt("画一只黑色的猫，线稿风格，居中")
        summary = plan_summary(plan)
        self.assertEqual(summary["subjects"], ["cat"])
        self.assertEqual(summary["style"], "mono_ink")
        self.assertEqual(summary["colors"], ["black"])
        self.assertEqual(summary["layout"], None)
        self.assertEqual(warnings, [])
        self.assertGreaterEqual(summary["elements"], 10)

    def test_english_cat_manga(self):
        plan, _warnings = plan_prompt("a black cat, manga style, centered")
        summary = plan_summary(plan)
        self.assertEqual(summary["subjects"], ["cat"])
        self.assertEqual(summary["style"], "manga_color")
        self.assertEqual(summary["colors"], ["black"])
        self.assertEqual(plan["canvas"]["width"], plan["canvas"]["height"])

    def test_longest_alias_wins_snowflake_not_flower(self):
        plan, _warnings = plan_prompt("一片雪花，蓝白，赛博朋克")
        self.assertEqual(plan_summary(plan)["subjects"], ["snow"])
        self.assertEqual(plan_summary(plan)["style"], "cyber")
        self.assertEqual(plan_summary(plan)["colors"], ["blue", "white"])

    def test_hex_color_accepted(self):
        plan, _warnings = plan_prompt("画一个圆，颜色 #12ab34")
        self.assertEqual(plan["palette"]["accent"], "#12ab34")

    def test_negated_colour_not_applied(self):
        plan, warnings = plan_prompt("画一个圆，不要红色，蓝色")
        self.assertEqual(plan["palette"]["accent"], prompts.COLOR_HEX["blue"])
        self.assertIn("FP_W_NEGATION_APPLIED", codes(warnings))

    def test_background_colour_words(self):
        plan, _warnings = plan_prompt("画一个圆，红色背景")
        self.assertEqual(plan["palette"]["paper"], prompts.COLOR_HEX["red"])
        self.assertEqual(plan["canvas"]["background"], "paper")

    def test_dark_background_words_do_not_set_colour(self):
        plan, _warnings = plan_prompt("五颗星星，紫色，深色背景，方形", style="cyber")
        self.assertEqual(plan["palette"]["paper"], prompts.STYLE_PACKS["cyber"]["palette"]["paper"])


class CountLayoutRatioCase(unittest.TestCase):
    def test_grid_count_nine(self):
        plan, _warnings = plan_prompt("紫色的星星，网格排列，9个，方形")
        summary = plan_summary(plan)
        self.assertEqual(summary["layout"], "grid")
        self.assertEqual(summary["elements"], 18)  # 9 stars × (fill + outline)
        self.assertEqual(summary["ratio"], "square")

    def test_chinese_count_with_measure_word(self):
        plan, _warnings = plan_prompt("画三颗心，红粉，重复排列，横版")
        summary = plan_summary(plan)
        self.assertEqual(summary["ratio"], "landscape")
        self.assertEqual(summary["layout"], "repeat")
        self.assertEqual(summary["elements"], 6)  # 3 hearts × 2 elements

    def test_count_clamped(self):
        plan, warnings = plan_prompt("画 20 个圆")
        self.assertIn("FP_W_COUNT_CLAMPED", codes(warnings))
        self.assertEqual(plan_summary(plan)["elements"], 2 * prompts.MAX_COUNT)

    def test_ratio_presets(self):
        portrait, _ = plan_prompt("画一座山，竖版")
        landscape, _ = plan_prompt("画一座山，横版")
        square, _ = plan_prompt("画一座山，方形")
        self.assertGreater(portrait["canvas"]["height"], portrait["canvas"]["width"])
        self.assertGreater(landscape["canvas"]["width"], landscape["canvas"]["height"])
        self.assertEqual(square["canvas"]["width"], square["canvas"]["height"])

    def test_position_is_metadata_only(self):
        left, _ = plan_prompt("画一棵树，左边")
        right, _ = plan_prompt("画一棵树，右边")
        self.assertNotEqual(plan_hash(left), plan_hash(right))
        left_points = [element for element in left["elements"] if "points" in element]
        right_points = [element for element in right["elements"] if "points" in element]
        self.assertLess(
            min(point[0] for element in left_points for point in element["points"]),
            min(point[0] for element in right_points for point in element["points"]),
        )


class TextCase(unittest.TestCase):
    def test_quoted_chinese_text(self):
        plan, _warnings = plan_prompt('画一朵花，文字："春天"')
        self.assertEqual(len(plan["texts"]), 1)
        self.assertEqual(plan["texts"][0]["text"], "春天")
        self.assertEqual(plan["texts"][0]["as"], "outline")

    def test_english_text_prefix(self):
        plan, _warnings = plan_prompt("画一只眼睛，水墨风格，文字: EYE")
        self.assertEqual(plan["texts"][0]["text"], "EYE")
        self.assertEqual(plan_summary(plan)["style"], "ink_wash")

    def test_quotes_are_stripped_from_prefix_text(self):
        plan, _warnings = plan_prompt('画一颗心，文字：“你好世界”')
        self.assertEqual(plan["texts"][0]["text"], "你好世界")

    def test_no_text_negation(self):
        plan, warnings = plan_prompt("雪山，黑白，横版，不要文字")
        self.assertNotIn("texts", plan)
        self.assertIn("FP_W_NEGATION_APPLIED", codes(warnings))

    def test_text_box_is_inside_canvas(self):
        plan, _warnings = plan_prompt('画一朵花，文字："春天"')
        x, y, width, height = plan["texts"][0]["box"]
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + width, plan["canvas"]["width"] + 0.01)
        self.assertLessEqual(y + height, plan["canvas"]["height"] + 0.01)


class StyleCase(unittest.TestCase):
    def test_all_style_packs_are_known(self):
        self.assertEqual(len(prompts.STYLE_PACKS), 9)
        for name in prompts.STYLE_PACKS:
            plan, warnings = plan_prompt("画一个圆", style=name)
            self.assertEqual(plan_summary(plan)["style"], name)
            self.assertEqual(warnings, [])

    def test_explicit_style_overrides_prompt(self):
        plan, _warnings = plan_prompt("画一个圆，水彩风格", style="oil")
        self.assertEqual(plan_summary(plan)["style"], "oil")

    def test_unknown_style_falls_back_with_warning(self):
        plan, warnings = plan_prompt("画一个圆", style="does-not-exist")
        self.assertEqual(plan_summary(plan)["style"], prompts.DEFAULT_STYLE)
        self.assertIn("FP_W_STYLE_UNKNOWN", codes(warnings))

    def test_chinese_style_aliases(self):
        cases = {
            "画一个圆，水墨": "ink_wash",
            "画一个圆，铅笔": "pencil",
            "画一个圆，马克笔": "marker",
            "画一个圆，油画": "oil",
            "画一个圆，极简": "minimal",
            "画一个圆，黑白": "mono_ink",
            "画一个圆，漫画": "manga_color",
        }
        for prompt, expected in cases.items():
            plan, _warnings = plan_prompt(prompt)
            self.assertEqual(plan_summary(plan)["style"], expected, prompt)


class DeterminismAndSchemaCase(unittest.TestCase):
    def test_same_prompt_and_seed_hash_equal(self):
        prompt = "金色的太阳，居中，极简风格"
        first, _ = plan_prompt(prompt, seed=7)
        second, _ = plan_prompt(prompt, seed=7)
        self.assertEqual(plan_hash(first), plan_hash(second))
        self.assertEqual(first, second)

    def test_seed_changes_plan_for_fallback_composition(self):
        first, _ = plan_prompt("一条龙", seed=1)
        second, _ = plan_prompt("一条龙", seed=2)
        self.assertNotEqual(plan_hash(first), plan_hash(second))

    def test_plan_is_json_clean(self):
        plan, _warnings = plan_prompt("画一只猫，文字：\"MEOW\"")
        text = json.dumps(plan, ensure_ascii=False, allow_nan=False)
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)

    def test_schema_valid_and_layers(self):
        plan, _warnings = plan_prompt("画一棵树，绿色，铅笔画")
        self.assertEqual(validate_plan(plan), [])
        self.assertEqual(plan["schema"], "fdsl/2.1")
        self.assertTrue(plan["palette"])
        self.assertEqual([layer["id"] for layer in plan["layers"]], [0, 1, 2])
        for element in plan["elements"]:
            self.assertIn(element["layer"], (0, 1))
            self.assertRegex(element["id"], r"^e[1-9][0-9]{0,5}$")
        self.assertEqual(
            [element["id"] for element in plan["elements"]],
            [f"e{index + 1}" for index in range(len(plan["elements"]))],
        )

    def test_every_subject_builds_a_valid_plan(self):
        for subject in sorted(prompts.SUBJECTS):
            plan, warnings = plan_prompt(f"画一个{subject}，居中")
            self.assertEqual(validate_plan(plan), [], subject)
            self.assertEqual(warnings, [], subject)
            self.assertGreaterEqual(len(plan["elements"]), 2, subject)
            self.assertTrue(plan["meta"]["subjects"])


class FailureCase(unittest.TestCase):
    def test_empty_prompt(self):
        with self.assertRaises(PlannerError) as caught:
            plan_prompt("")
        self.assertEqual(caught.exception.code, "FP_E_PROMPT_EMPTY")

    def test_whitespace_only_prompt(self):
        with self.assertRaises(PlannerError) as caught:
            plan_prompt("   \n\t ")
        self.assertEqual(caught.exception.code, "FP_E_PROMPT_EMPTY")

    def test_unparseable_prompt(self):
        for prompt in ("???", "!!!...", "🐱🐱"):
            with self.assertRaises(PlannerError) as caught:
                plan_prompt(prompt)
            self.assertEqual(caught.exception.code, "FP_E_PROMPT_UNPARSEABLE", prompt)

    def test_prompt_type(self):
        with self.assertRaises(PlannerError) as caught:
            plan_prompt(123)
        self.assertEqual(caught.exception.code, "FP_E_PROMPT_TYPE")

    def test_prompt_too_long(self):
        with self.assertRaises(PlannerError) as caught:
            plan_prompt("画" * 2001)
        self.assertEqual(caught.exception.code, "FP_E_PROMPT_TOO_LONG")

    def test_bad_seed_and_style_types(self):
        with self.assertRaises(PlannerError) as caught:
            plan_prompt("画一个圆", seed=1.5)
        self.assertEqual(caught.exception.code, "FP_E_SEED_TYPE")
        with self.assertRaises(PlannerError) as caught:
            plan_prompt("画一个圆", style=42)
        self.assertEqual(caught.exception.code, "FP_E_STYLE_TYPE")

    def test_only_action_word_degrades_to_generic(self):
        plan, warnings = plan_prompt("画")
        self.assertIn("FP_W_SUBJECT_UNKNOWN", codes(warnings))
        self.assertEqual(validate_plan(plan), [])

    def test_unknown_subject_degrades_to_generic(self):
        plan, warnings = plan_prompt("一条龙，线稿风格，居中")
        self.assertIn("FP_W_SUBJECT_UNKNOWN", codes(warnings))
        self.assertEqual(plan_summary(plan)["subjects"], ["generic"])
        self.assertEqual(validate_plan(plan), [])

    def test_emoji_and_text_still_render(self):
        plan, warnings = plan_prompt("画一个圆 🎨")
        self.assertEqual(warnings, [])
        self.assertEqual(plan_summary(plan)["subjects"], ["circle"])


class CompileRenderCase(unittest.TestCase):
    def _roundtrip(self, prompt, seed=0):
        plan, _warnings = plan_prompt(prompt, seed=seed)
        result = compile_plan(plan, resolution=64)
        image = render(result["fbs"], resolution=64)
        self.assertEqual(max(image.shape[:2]), 64)
        self.assertEqual(len(image.shape), 3)
        self.assertGreater(int(image.std()), 0)
        return plan, result

    def test_cat_compiles(self):
        plan, result = self._roundtrip("画一只黑色的猫，线稿风格，居中")
        self.assertEqual(result["stats"]["elements"], len(plan["elements"]))

    def test_text_compiles_to_fills(self):
        plan, result = self._roundtrip('画一朵花，文字："春天"')
        self.assertEqual(len(plan["texts"]), 1)
        self.assertGreater(result["stats"]["text"], 0)

    def test_multi_subject_compiles(self):
        plan, _result = self._roundtrip("月亮和山，竖版，水墨")
        self.assertEqual(plan_summary(plan)["subjects"], ["moon", "mountain"])

    def test_unknown_subject_still_renders(self):
        self._roundtrip("一条龙，赛博风格")

    def test_no_float_noise_in_points(self):
        plan, _warnings = plan_prompt("画一个几何图形")
        for element in plan["elements"]:
            for point in element.get("points", []) + element.get("poly", []):
                self.assertTrue(math.isfinite(point[0]) and math.isfinite(point[1]))


if __name__ == "__main__":
    unittest.main()
