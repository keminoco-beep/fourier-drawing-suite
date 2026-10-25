"""Tests for the synthetic dataset builder (deterministic, no GPU, no planner)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from llm_fixtures import FakePlanner, bad_plan, valid_plan

from fourier_prompt.llm.dataset import (
    DatasetError,
    build_dataset,
    build_parser,
    load_jsonl,
    synthesise_prompts,
)
from fourier_prompt.llm.formatting import canonical_plan_json, extract_json
from fourier_prompt.schema import validate_plan


class DatasetBuildTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_small_dataset_files_and_split(self):
        planner = FakePlanner()
        out = os.path.join(self.tmp, "data")
        result = build_dataset(out_dir=out, n=10, seed=0, planner_fn=planner, validate_ratio=0.2)
        self.assertEqual(result["n"], 10)
        self.assertEqual(result["n_train"], 8)
        self.assertEqual(result["n_val"], 2)
        self.assertEqual(len(planner.calls), 10)
        for name in ("train.jsonl", "val.jsonl", "stats.json"):
            self.assertTrue(os.path.isfile(os.path.join(out, name)), name)
        self.assertEqual(len(load_jsonl(result["train_path"])), 8)
        self.assertEqual(len(load_jsonl(result["val_path"])), 2)

    def test_records_validate_and_are_canonical(self):
        out = os.path.join(self.tmp, "data")
        build_dataset(out_dir=out, n=6, seed=3, planner_fn=FakePlanner())
        for record in load_jsonl(os.path.join(out, "train.jsonl")) + load_jsonl(
            os.path.join(out, "val.jsonl")
        ):
            self.assertEqual(set(record), {"prompt", "plan", "plan_json", "meta"})
            self.assertTrue(record["prompt"].strip())
            self.assertEqual(validate_plan(record["plan"]), [])
            self.assertEqual(record["plan_json"], canonical_plan_json(record["plan"]))
            self.assertEqual(extract_json(record["plan_json"]), record["plan"])
            self.assertNotIn("\n", record["plan_json"])

    def test_deterministic_for_same_seed(self):
        first = build_dataset(out_dir=os.path.join(self.tmp, "a"), n=8, seed=7, planner_fn=FakePlanner())
        second = build_dataset(out_dir=os.path.join(self.tmp, "b"), n=8, seed=7, planner_fn=FakePlanner())
        for key in ("train_path", "val_path"):
            with open(first[key], "rb") as left, open(second[key], "rb") as right:
                self.assertEqual(left.read(), right.read(), key)
        self.assertEqual(first["stats"], second["stats"])

    def test_different_seed_changes_prompts(self):
        build_dataset(out_dir=os.path.join(self.tmp, "a"), n=5, seed=1, planner_fn=FakePlanner())
        build_dataset(out_dir=os.path.join(self.tmp, "b"), n=5, seed=2, planner_fn=FakePlanner())
        left = load_jsonl(os.path.join(self.tmp, "a", "train.jsonl"))
        right = load_jsonl(os.path.join(self.tmp, "b", "train.jsonl"))
        self.assertNotEqual([row["prompt"] for row in left], [row["prompt"] for row in right])

    def test_prompts_are_unique(self):
        out = os.path.join(self.tmp, "data")
        build_dataset(out_dir=out, n=30, seed=11, planner_fn=FakePlanner())
        rows = load_jsonl(os.path.join(out, "train.jsonl")) + load_jsonl(
            os.path.join(out, "val.jsonl")
        )
        prompts = [row["prompt"] for row in rows]
        self.assertEqual(len(prompts), len(set(prompts)))
        with open(os.path.join(out, "stats.json"), encoding="utf-8") as handle:
            stats = json.load(handle)
        self.assertEqual(stats["n_unique_prompts"], 30)
        self.assertEqual(stats["plan_bytes"]["count"], 30)
        self.assertGreater(stats["plan_bytes"]["max"], 0)

    def test_plain_dict_planner_is_accepted(self):
        planner = FakePlanner(return_tuple=False)
        out = os.path.join(self.tmp, "data")
        build_dataset(out_dir=out, n=3, seed=0, planner_fn=planner)
        self.assertEqual(len(load_jsonl(os.path.join(out, "train.jsonl")))
                         + len(load_jsonl(os.path.join(out, "val.jsonl"))), 3)

    def test_invalid_plans_are_dropped_and_counted(self):
        calls = {"n": 0}

        def mixed_planner(prompt, seed=0):
            calls["n"] += 1
            if calls["n"] <= 3:
                return bad_plan(), []
            return valid_plan(), []

        out = os.path.join(self.tmp, "data")
        result = build_dataset(out_dir=out, n=8, seed=0, planner_fn=mixed_planner)
        self.assertEqual(result["n"], 5)
        self.assertEqual(result["n_dropped"], 3)
        self.assertTrue(result["stats"]["drop_reasons"])
        self.assertTrue(any(key.startswith("repair:") for key in result["stats"]["drop_reasons"]))

    def test_planner_exceptions_are_dropped(self):
        out = os.path.join(self.tmp, "data")

        # A planner that always raises must produce no records -> DatasetError.
        def exploding(prompt, seed=0):
            raise RuntimeError("boom")

        with self.assertRaises(DatasetError):
            build_dataset(out_dir=out, n=4, seed=0, planner_fn=exploding)

    def test_zero_validation_ratio(self):
        out = os.path.join(self.tmp, "data")
        result = build_dataset(out_dir=out, n=3, seed=0, planner_fn=FakePlanner(), validate_ratio=0.0)
        self.assertEqual(result["n_val"], 0)
        self.assertEqual(result["n_train"], 3)
        self.assertEqual(load_jsonl(result["val_path"]), [])

    def test_single_record_has_no_validation_split(self):
        out = os.path.join(self.tmp, "data")
        result = build_dataset(out_dir=out, n=1, seed=0, planner_fn=FakePlanner())
        self.assertEqual(result["n_train"], 1)
        self.assertEqual(result["n_val"], 0)

    def test_invalid_arguments(self):
        with self.assertRaises(DatasetError):
            build_dataset(out_dir=self.tmp, n=0, planner_fn=FakePlanner())
        with self.assertRaises(DatasetError):
            build_dataset(out_dir=self.tmp, n=2, validate_ratio=1.0, planner_fn=FakePlanner())
        with self.assertRaises(DatasetError):
            build_dataset(out_dir=self.tmp, n=2, validate_ratio=-0.1, planner_fn=FakePlanner())

    def test_default_planner_unavailable_is_reported(self):
        try:
            from fourier_prompt.planner import plan_prompt  # noqa: F401
        except Exception:
            pass
        else:
            self.skipTest("WP-N planner is available; unavailability path not reachable")
        with self.assertRaises(DatasetError) as context:
            build_dataset(out_dir=self.tmp, n=2, seed=0)
        self.assertIn("planner", str(context.exception))


class PromptSynthesisTests(unittest.TestCase):
    def test_unique_and_reproducible(self):
        first = [prompt for prompt, _meta in synthesise_prompts(50, seed=4)]
        second = [prompt for prompt, _meta in synthesise_prompts(50, seed=4)]
        self.assertEqual(first, second)
        self.assertEqual(len(first), len(set(first)))
        self.assertEqual(len({_meta["language"] for _p, _meta in synthesise_prompts(20, seed=4)}), 2)

    def test_cli_parser_defaults(self):
        args = build_parser().parse_args(["--out", "x"])
        self.assertEqual(args.n, 2000)
        self.assertEqual(args.seed, 0)
        self.assertFalse(args.compile_check)


if __name__ == "__main__":
    unittest.main()
