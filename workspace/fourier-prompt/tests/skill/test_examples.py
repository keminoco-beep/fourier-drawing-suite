"""The shipped skill examples must stay valid, strict-clean and compilable."""

from __future__ import annotations

import unittest

from _support import EXAMPLES, load_example, read_text

from fourier_prompt.compile import compile_plan
from fourier_prompt.schema import validate_plan_detailed

EXAMPLE_NAMES = ("text_plan.json", "simple_plan.json")
_RESOLUTION = 64


class ExamplesCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plans = {name: load_example(name) for name in EXAMPLE_NAMES}
        cls.reports = {
            name: validate_plan_detailed(plan, strict=True) for name, plan in cls.plans.items()
        }
        cls.compiled = {
            name: compile_plan(plan, resolution=_RESOLUTION)
            for name, plan in cls.plans.items()
        }

    def test_examples_are_strict_valid_plans(self):
        for name in EXAMPLE_NAMES:
            with self.subTest(example=name):
                plan = self.plans[name]
                self.assertEqual(plan["schema"], "fdsl/2.1")
                self.assertIsInstance(plan.get("canvas"), dict)
                self.assertTrue(plan.get("elements"))
                self.assertTrue(plan.get("layers"))
                report = self.reports[name]
                self.assertTrue(report["ok"], report["errors"])
                self.assertEqual(report["warnings"], [], name)

    def test_examples_compile_to_records(self):
        for name in EXAMPLE_NAMES:
            with self.subTest(example=name):
                result = self.compiled[name]
                stats = result["stats"]
                self.assertGreater(stats["records"], 0)
                self.assertFalse(
                    [w for w in result["warnings"] if str(w["code"]).startswith("FP_E_")],
                    result["warnings"],
                )
                self.assertIn("fbs", result)
                self.assertTrue(result["fbs"].get("strokes"))

    def test_text_example_exercises_text_and_simple_does_not(self):
        text_stats = self.compiled["text_plan.json"]["stats"]
        simple_stats = self.compiled["simple_plan.json"]["stats"]
        self.assertEqual(text_stats["text"], 2)
        self.assertGreater(text_stats["text_fills"], 0)
        self.assertEqual(simple_stats["text"], 0)
        self.assertEqual(simple_stats["text_fills"], 0)
        self.assertGreaterEqual(text_stats["records"], simple_stats["records"])

    def test_examples_readme_mentions_both_files_and_demo(self):
        text = read_text(EXAMPLES / "README.md")
        self.assertIn("text_plan.json", text)
        self.assertIn("simple_plan.json", text)
        self.assertIn("demo.py", text)
        self.assertIn("--plan", text)
        self.assertIn("schema --strict", text)
        self.assertTrue((EXAMPLES / "text_plan.json").is_file())
        self.assertTrue((EXAMPLES / "simple_plan.json").is_file())


if __name__ == "__main__":
    unittest.main()
