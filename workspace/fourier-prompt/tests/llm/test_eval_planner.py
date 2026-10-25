"""Tests for scripts/eval_planner.py (rule-only; no GPU/model)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from llm_fixtures import FakePlanner, load_script, write_jsonl

from fourier_prompt.llm.dataset import DatasetError


class EvalPlannerTests(unittest.TestCase):
    def setUp(self):
        self.module = load_script("eval_planner")
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_builtin_prompt_set_has_twenty(self):
        self.assertEqual(len(self.module.DEFAULT_PROMPTS), 20)
        self.assertEqual(len(set(self.module.DEFAULT_PROMPTS)), 20)

    def test_load_prompts_falls_back_to_builtin(self):
        prompts, source = self.module.load_prompts(os.path.join(self.tmp, "missing.jsonl"))
        self.assertEqual(len(prompts), 20)
        self.assertEqual(source, "builtin")

    def test_load_prompts_reads_jsonl_objects_and_strings(self):
        path = os.path.join(self.tmp, "prompts.jsonl")
        write_jsonl(path, [{"prompt": "画猫"}, "Draw a cat", {"text": "画狗"}])
        prompts, source = self.module.load_prompts(path)
        self.assertEqual(prompts, ["画猫", "Draw a cat", "画狗"])
        self.assertEqual(source, os.path.abspath(path))

    def test_run_eval_rule_only_aggregates(self):
        report = self.module.run_eval(
            ["画猫", "draw a star", "画一只狐狸"],
            planner_fn=FakePlanner(),
            rule_only=True,
            limit=2,
        )
        aggregate = report["aggregate"]
        self.assertEqual(aggregate["n"], 2)
        self.assertEqual(report["mode"], "rule_only")
        self.assertEqual(aggregate["rule"]["compile_ok"], 2)
        self.assertEqual(aggregate["llm"]["compile_ok"], 2)
        self.assertEqual(aggregate["llm"]["fallback_rate"], 1.0)
        self.assertEqual(aggregate["comparison"]["both_compile"], 2)
        self.assertEqual(len(report["rows"]), 2)
        for row in report["rows"]:
            self.assertEqual(row["llm"]["source"], "rule")
            self.assertEqual(row["llm"]["attempts"], 0)
            self.assertGreaterEqual(row["rule"]["latency_ms"], 0.0)

    def test_run_eval_reports_unavailable_rule_planner(self):
        def unavailable(prompt, seed=0):
            raise DatasetError("planner missing")

        report = self.module.run_eval(
            ["画猫"], planner_fn=unavailable, rule_only=True, limit=1
        )
        row = report["rows"][0]
        self.assertFalse(row["rule"]["available"])
        self.assertFalse(row["rule"]["compile_ok"])
        self.assertFalse(row["llm"]["ok"])
        self.assertEqual(report["aggregate"]["rule"]["compile_rate"], 0.0)

    def test_main_writes_report(self):
        prompts = os.path.join(self.tmp, "prompts.jsonl")
        write_jsonl(prompts, [{"prompt": "画猫"}, {"prompt": "draw a star"}])
        output = os.path.join(self.tmp, "report.json")
        # Inject the fake planner so the test never needs WP-N or torch.
        original = self.module.run_eval

        def run_eval_with_fake(prompts_arg, **kwargs):
            kwargs["planner_fn"] = FakePlanner()
            return original(prompts_arg, **kwargs)

        self.module.run_eval = run_eval_with_fake
        try:
            with unittest.mock.patch("sys.stdout"):
                code = self.module.main(
                    ["--rule-only", "--prompts", prompts, "--out", output, "--limit", "2"]
                )
        finally:
            self.module.run_eval = original
        self.assertEqual(code, 0)
        with open(output, encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual(report["aggregate"]["n"], 2)
        self.assertEqual(report["aggregate"]["rule"]["compile_ok"], 2)

    def test_invalid_limit_exits_two(self):
        with self.assertRaises(SystemExit) as context:
            with unittest.mock.patch("sys.stderr"):
                self.module.main(["--rule-only", "--limit", "0"])
        self.assertEqual(context.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
