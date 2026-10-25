"""Tests for ``generate_plan``: fallback, retries, error paths (no model/GPU)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from llm_fixtures import FakePlanner, FakeTokenizer, valid_plan

from fourier_prompt.llm import infer
from fourier_prompt.llm.dataset import DatasetError


def plan_json():
    return json.dumps(valid_plan(), ensure_ascii=False)


class GeneratePlanFallbackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_model_falls_back_to_rule(self):
        planner = FakePlanner()
        result = infer.generate_plan(
            "画一只猫",
            model_dir=os.path.join(self.tmp, "no-such-model"),
            planner_fn=planner,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "rule")
        self.assertEqual(result["attempts"], 0)
        self.assertTrue(result["compile_ok"])
        self.assertTrue(result["fallback"])
        self.assertEqual(result["errors"][0]["stage"], "load")
        self.assertIsNotNone(result["plan"])
        self.assertEqual(result["plan"]["name"], "plan-0")
        self.assertEqual(len(planner.calls), 1)
        self.assertIsInstance(result["latency_s"], float)

    def test_rule_only_skips_model_entirely(self):
        result = infer.generate_plan("draw a cat", rule_only=True, planner_fn=FakePlanner())
        self.assertEqual(result["source"], "rule")
        self.assertTrue(result["ok"])
        self.assertTrue(result["compile_ok"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["attempts"], 0)

    def test_empty_prompt_is_rejected(self):
        result = infer.generate_plan("   ", planner_fn=FakePlanner())
        self.assertFalse(result["ok"])
        self.assertIsNone(result["plan"])
        self.assertEqual(result["errors"][0]["code"], "FP_E_PROMPT_EMPTY")

    def test_missing_rule_planner_returns_soft_failure(self):
        def unavailable(prompt, seed=0):
            raise DatasetError("planner is not installed yet")

        result = infer.generate_plan(
            "draw",
            model_dir=os.path.join(self.tmp, "missing"),
            planner_fn=unavailable,
        )
        self.assertFalse(result["ok"])
        self.assertIsNone(result["plan"])
        self.assertEqual(result["source"], "rule")
        stages = {item["stage"] for item in result["errors"]}
        self.assertIn("load", stages)
        self.assertIn("rule", stages)


class GeneratePlanModelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.tokenizer = FakeTokenizer()
        self.model = object()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, side_effect, **kwargs):
        with mock.patch.object(infer, "_generate_raw", side_effect=side_effect) as patched:
            result = infer.generate_plan(
                "draw a cat",
                model=self.model,
                tokenizer=self.tokenizer,
                planner_fn=FakePlanner(),
                **kwargs,
            )
        return result, patched

    def test_valid_first_attempt(self):
        result, patched = self._run([plan_json()])
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["attempts"], 1)
        self.assertTrue(result["compile_ok"])
        self.assertFalse(result["fallback"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(patched.call_count, 1)
        self.assertEqual(result["attempt_trace"][0]["stage"], "compiled")

    def test_bad_json_then_valid_retries_once(self):
        result, patched = self._run(["no json at all", plan_json()])
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(patched.call_count, 2)
        self.assertIn("extract", {item["stage"] for item in result["errors"]})
        self.assertEqual(result["attempt_trace"][0]["ok"], False)
        self.assertEqual(result["attempt_trace"][1]["ok"], True)

    def test_invalid_plan_then_valid_retries(self):
        broken = json.dumps({"schema": "fdsl/2.1", "layers": []})
        result, patched = self._run([broken, plan_json()])
        self.assertEqual(result["attempts"], 2)
        self.assertIn("repair", {item["stage"] for item in result["errors"]})

    def test_all_attempts_fail_then_rule_fallback(self):
        result, patched = self._run(["nope", "still nope", "nope again"])
        self.assertEqual(patched.call_count, 3)
        self.assertEqual(result["source"], "rule")
        self.assertTrue(result["ok"])
        self.assertTrue(result["fallback"])
        self.assertEqual(result["attempts"], 3)
        self.assertIn("fallback_reason", result)
        self.assertTrue(result["compile_ok"])

    def test_retries_are_capped_at_two(self):
        result, patched = self._run(["bad"] * 6, max_retries=10)
        self.assertEqual(patched.call_count, 3)
        self.assertEqual(result["attempts"], 3)

    def test_generation_exception_falls_back(self):
        result, _patched = self._run([RuntimeError("CUDA out of memory")])
        self.assertEqual(result["source"], "rule")
        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"][0]["stage"], "generate")

    def test_compile_check_can_be_disabled(self):
        result, _patched = self._run(
            [plan_json()], compile_check_enabled=False
        )
        self.assertEqual(result["source"], "llm")
        self.assertTrue(result["compile_ok"])


class LoadModelTests(unittest.TestCase):
    def test_load_model_reports_missing_stack_or_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises((RuntimeError, FileNotFoundError)):
                infer.load_model(os.path.join(tmp, "missing"), None, "cpu")


if __name__ == "__main__":
    unittest.main()
