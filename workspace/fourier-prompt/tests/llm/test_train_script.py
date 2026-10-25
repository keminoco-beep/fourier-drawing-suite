"""Tests for scripts/train_lora.py: guarded imports, CLI, dry-run (no GPU)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from llm_fixtures import load_script, write_jsonl


class TrainScriptTests(unittest.TestCase):
    def setUp(self):
        self.module = load_script("train_lora")
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.dataset = os.path.join(self.tmp, "train.jsonl")
        write_jsonl(
            self.dataset,
            [
                {
                    "prompt": f"prompt {index}",
                    "plan": {"schema": "fdsl/2.1"},
                    "plan_json": '{"schema":"fdsl/2.1"}',
                    "meta": {},
                }
                for index in range(8)
            ],
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_import_is_guarded(self):
        self.assertTrue(hasattr(self.module, "main"))
        self.assertTrue(hasattr(self.module, "build_parser"))
        self.assertTrue(hasattr(self.module, "train"))
        # The stack import is captured instead of aborting module import.
        self.assertTrue(hasattr(self.module, "_IMPORT_ERROR"))
        self.assertTrue(callable(self.module.PlanJsonlDataset))

    def test_help_exits_zero(self):
        with self.assertRaises(SystemExit) as context:
            with contextlib.redirect_stdout(io.StringIO()):
                self.module.main(["--help"])
        self.assertEqual(context.exception.code, 0)

    def test_invalid_arguments_exit_two(self):
        for argv in (
            ["--lr", "0"],
            ["--lr", "-1"],
            ["--lora-r", "0"],
            ["--batch-size", "0"],
            ["--grad-accum", "0"],
            ["--max-len", "16"],
            ["--num-epochs", "0"],
            ["--max-steps", "0"],
            ["--lora-dropout", "1.0"],
            ["--target-modules", " , "],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as context:
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.module.main(argv)
                self.assertEqual(context.exception.code, 2)

    def test_dry_run_prints_config_without_torch(self):
        out_dir = os.path.join(self.tmp, "adapter")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(
                [
                    "--dry-run",
                    "--dataset",
                    self.dataset,
                    "--model",
                    os.path.join(self.tmp, "model"),
                    "--output",
                    out_dir,
                    "--max-steps",
                    "20",
                ]
            )
        self.assertEqual(code, 0)
        config = json.loads(stdout.getvalue())
        self.assertEqual(config["train_records"], 8)
        self.assertEqual(config["planned_optimizer_steps"], 20)
        self.assertEqual(config["batch_size"], 1)
        self.assertEqual(config["grad_accum"], 8)
        self.assertEqual(config["max_len"], 4096)
        self.assertEqual(config["lora"]["r"], 16)
        self.assertEqual(config["lora"]["alpha"], 32)
        self.assertIn("q_proj", config["lora"]["target_modules"])
        self.assertIn("down_proj", config["lora"]["target_modules"])

    def test_dry_run_missing_dataset_still_returns_zero(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.module.main(
                ["--dry-run", "--dataset", os.path.join(self.tmp, "nope.jsonl")]
            )
        self.assertEqual(code, 0)
        self.assertIn("warning", stderr.getvalue())
        self.assertIsNone(json.loads(stdout.getvalue())["train_records"])

    def test_smoke_sets_twenty_steps(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main(["--dry-run", "--smoke", "--dataset", self.dataset])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["planned_optimizer_steps"], 20)

    def test_training_without_stack_fails_softly(self):
        if self.module._IMPORT_ERROR is None:
            self.skipTest("torch/transformers/peft are installed; failure path not reachable")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = self.module.main(["--dataset", self.dataset, "--model", self.tmp])
        self.assertEqual(code, 2)
        self.assertIn("not available", stderr.getvalue())

    def test_missing_dataset_or_model_reports_error(self):
        if self.module._IMPORT_ERROR is not None:
            self.skipTest("torch stack not installed; covered by dependency check")
        code = self.module.main(
            ["--dataset", os.path.join(self.tmp, "nope.jsonl"), "--model", self.tmp]
        )
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
