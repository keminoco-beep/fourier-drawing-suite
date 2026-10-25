"""Tests for the hybrid intent path (:mod:`fourier_prompt.llm.intent`).

No GPU, no model load and no network: the model is replaced by a fake
generator; the intent->planner path uses the real deterministic planner.
The one CLI case that would load torch patches ``intent._load_model``.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from llm_fixtures import FakePlanner

from fourier_prompt.llm import intent
from fourier_prompt.planner import plan_hash
from fourier_prompt.schema import validate_plan


def payload(**overrides):
    base = {
        "subject": ["cat"],
        "count": 1,
        "colors": ["black"],
        "style": "mono_ink",
        "text": [],
        "position": "center",
        "canvas": "square",
        "negations": [],
        "confidence": 0.9,
    }
    base.update(overrides)
    return base


def intent_json(**overrides):
    return json.dumps(payload(**overrides), ensure_ascii=False)


class FakeGenerator:
    """Deterministic stand-in for the local model's chat generation."""

    def __init__(self, outputs):
        self.outputs = list(outputs) if isinstance(outputs, (list, tuple)) else [outputs]
        self.calls = []

    def __call__(self, messages, *, max_new_tokens, seed):
        self.calls.append({"messages": messages, "max_new_tokens": max_new_tokens, "seed": seed})
        output = self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)]
        if isinstance(output, Exception):
            raise output
        return output


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


class NormalizeIntentTests(unittest.TestCase):
    def test_valid_payload_roundtrip(self):
        normalised, warnings = intent.normalize_intent(payload())
        self.assertEqual(normalised, payload())
        self.assertEqual(warnings, [])

    def test_field_aliases_and_embedded_words(self):
        normalised, _warnings = intent.normalize_intent(
            {
                "subjects": ["a black cat"],
                "colour": ["黑"],
                "ratio": "方图",
                "texts": ["春天"],
                "aspect": "方形",
            }
        )
        self.assertEqual(normalised["subject"], ["cat"])
        self.assertEqual(normalised["colors"], ["black"])
        self.assertEqual(normalised["canvas"], "square")
        self.assertEqual(normalised["text"], ["春天"])

    def test_unknown_values_fall_back_with_warnings(self):
        normalised, warnings = intent.normalize_intent(
            payload(
                subject=["dragon", "cat"],
                colors=["chartreuse", "red"],
                style="artdeco",
                position="diagonal",
                canvas="billboard",
            ),
            prompt=None,
        )
        codes = {item["code"] for item in warnings}
        self.assertEqual(normalised["subject"], ["cat"])
        self.assertEqual(normalised["colors"], ["red"])
        self.assertIsNone(normalised["style"])
        self.assertIsNone(normalised["position"])
        self.assertIsNone(normalised["canvas"])
        for code in (
            "FP_W_INTENT_SUBJECT_UNKNOWN",
            "FP_W_INTENT_COLOR_UNKNOWN",
            "FP_W_INTENT_STYLE_UNKNOWN",
            "FP_W_INTENT_POSITION_UNKNOWN",
            "FP_W_INTENT_CANVAS_UNKNOWN",
        ):
            self.assertIn(code, codes)

    def test_count_and_confidence_clamped(self):
        normalised, warnings = intent.normalize_intent(
            payload(count=99, confidence=1.4), prompt=None
        )
        self.assertEqual(normalised["count"], 12)
        self.assertEqual(normalised["confidence"], 1.0)
        self.assertIn("FP_W_INTENT_COUNT_CLAMPED", {item["code"] for item in warnings})

        normalised, warnings = intent.normalize_intent(payload(count=0), prompt=None)
        self.assertEqual(normalised["count"], 1)
        self.assertIn("FP_W_INTENT_COUNT_CLAMPED", {item["code"] for item in warnings})

        normalised, warnings = intent.normalize_intent(payload(count="三"), prompt=None)
        self.assertEqual(normalised["count"], 3)

        normalised, warnings = intent.normalize_intent(
            payload(confidence="not-a-number"), prompt=None
        )
        self.assertEqual(normalised["confidence"], 0.0)
        self.assertIn("FP_W_INTENT_CONFIDENCE_INVALID", {item["code"] for item in warnings})

    def test_colors_accept_aliases_hex_and_cap(self):
        normalised, warnings = intent.normalize_intent(
            payload(colors=["#AABBCC", "蓝色", "红"]), prompt=None
        )
        self.assertEqual(normalised["colors"], ["#aabbcc", "blue"])
        self.assertIn("FP_W_INTENT_COLOR_TRUNCATED", {item["code"] for item in warnings})

    def test_text_truncated_and_capped(self):
        normalised, warnings = intent.normalize_intent(
            payload(text=['"春天"', "a" * 50, "第三"]), prompt=None
        )
        self.assertEqual(len(normalised["text"]), 2)
        self.assertEqual(normalised["text"][0], "春天")
        self.assertEqual(len(normalised["text"][1]), 40)
        self.assertIn("FP_W_INTENT_TEXT_TRUNCATED", {item["code"] for item in warnings})

    def test_canvas_forms(self):
        normalised, _warnings = intent.normalize_intent(payload(canvas="横版"), prompt=None)
        self.assertEqual(normalised["canvas"], "landscape")

        normalised, warnings = intent.normalize_intent(
            payload(canvas={"width": 768, "height": 1024}), prompt=None
        )
        self.assertEqual(normalised["canvas"], "portrait")
        self.assertIn("FP_W_INTENT_CANVAS_SNAPPED", {item["code"] for item in warnings})

        normalised, _warnings = intent.normalize_intent(payload(canvas="16:9"), prompt=None)
        self.assertEqual(normalised["canvas"], "landscape")

    def test_negations_normalised_and_conflict_resolved(self):
        normalised, warnings = intent.normalize_intent(
            payload(text=["春天"], negations=["no text", "no background", "no watermark"]),
            prompt=None,
        )
        self.assertEqual(normalised["negations"], ["no_background"])
        codes = {item["code"] for item in warnings}
        self.assertIn("FP_W_INTENT_CONFLICT", codes)
        self.assertIn("FP_W_INTENT_NEGATION_UNKNOWN", codes)

    def test_unknown_fields_dropped(self):
        normalised, warnings = intent.normalize_intent(payload(layout="grid"), prompt=None)
        self.assertNotIn("layout", normalised)
        self.assertIn("FP_W_INTENT_UNKNOWN_FIELD", {item["code"] for item in warnings})

    def test_intent_envelope_unwrapped(self):
        normalised, _warnings = intent.normalize_intent({"intent": payload()})
        self.assertEqual(normalised, payload())

    def test_prompt_lexicon_fallback_fills_missing_fields(self):
        normalised, warnings = intent.normalize_intent(
            {"subject": [], "colors": [], "style": None, "confidence": 0.5},
            prompt="画一只黑猫，线稿风格，居中",
        )
        self.assertEqual(normalised["subject"], ["cat"])
        self.assertEqual(normalised["colors"], ["black"])
        self.assertEqual(normalised["style"], "mono_ink")
        self.assertIn(
            "FP_W_INTENT_FIELD_FALLBACK", {item["code"] for item in warnings}
        )


# ---------------------------------------------------------------------------
# extract_intent
# ---------------------------------------------------------------------------


class ExtractIntentTests(unittest.TestCase):
    def test_generator_parses_fenced_json_with_prose(self):
        generator = FakeGenerator(
            "Sure! Here is the intent:\n```json\n" + intent_json() + "\n```\n"
        )
        found, meta = intent.extract_intent("画一只黑猫", generator=generator, device="cpu")
        self.assertIsNotNone(found)
        self.assertEqual(found["subject"], ["cat"])
        self.assertTrue(meta["ok"])
        self.assertTrue(meta["json_ok"])
        self.assertFalse(meta["fallback"])
        self.assertEqual(meta["attempts"], 1)
        self.assertGreater(meta["raw_chars"], 0)
        self.assertIsInstance(meta["latency_s"], float)
        self.assertEqual(generator.calls[0]["max_new_tokens"], 256)
        self.assertEqual(generator.calls[0]["messages"][0]["role"], "system")
        self.assertIn("subject", generator.calls[0]["messages"][0]["content"])

    def test_bad_json_returns_fallback_meta(self):
        generator = FakeGenerator("I cannot help with that, sorry.")
        found, meta = intent.extract_intent("draw a cat", generator=generator, device="cpu")
        self.assertIsNone(found)
        self.assertFalse(meta["ok"])
        self.assertFalse(meta["json_ok"])
        self.assertTrue(meta["fallback"])
        self.assertEqual(meta["errors"][0]["stage"], "extract")
        self.assertEqual(meta["errors"][0]["code"], "llm/json_invalid")
        self.assertGreater(meta["raw_chars"], 0)

    def test_generator_exception_is_reported(self):
        generator = FakeGenerator(RuntimeError("CUDA out of memory"))
        found, meta = intent.extract_intent("draw", generator=generator, device="cpu")
        self.assertIsNone(found)
        self.assertEqual(meta["errors"][0]["stage"], "generate")
        self.assertTrue(meta["fallback"])

    def test_model_load_failure_reports_load_stage(self):
        with mock.patch.object(
            intent, "_load_model", side_effect=FileNotFoundError("no local model")
        ):
            found, meta = intent.extract_intent(
                "draw a cat", model_dir="missing-model", device="cpu"
            )
        self.assertIsNone(found)
        self.assertEqual(meta["errors"][0]["stage"], "load")
        self.assertEqual(meta["errors"][0]["code"], "FP_E_MODEL_UNAVAILABLE")
        self.assertTrue(meta["fallback"])

    def test_seed_is_forwarded_and_deterministic(self):
        generator = FakeGenerator(intent_json())
        first, _meta = intent.extract_intent("画一只黑猫", generator=generator, seed=7)
        second, _meta = intent.extract_intent("画一只黑猫", generator=generator, seed=7)
        self.assertEqual(first, second)
        self.assertEqual([call["seed"] for call in generator.calls], [7, 7])

    def test_empty_prompt_rejected(self):
        with self.assertRaises(intent.IntentError) as caught:
            intent.extract_intent("   ", generator=FakeGenerator(intent_json()))
        self.assertEqual(caught.exception.code, "FP_E_PROMPT_EMPTY")

    def test_invalid_device_rejected(self):
        with self.assertRaises(intent.IntentError) as caught:
            intent.extract_intent("画一只猫", generator=FakeGenerator(intent_json()), device="tpu")
        self.assertEqual(caught.exception.code, "FP_E_DEVICE")


# ---------------------------------------------------------------------------
# intent -> rule planner
# ---------------------------------------------------------------------------


class PlanFromIntentTests(unittest.TestCase):
    def test_plan_compiles_and_keeps_slots(self):
        from fourier_prompt.compile import compile_plan

        found, _meta = intent.normalize_intent(
            payload(subject=["cat"], colors=["black"], style="mono_ink", position="center")
        )
        plan, warnings = intent.plan_from_intent(found, seed=0)
        self.assertEqual(validate_plan(plan), [])
        compile_plan(plan, resolution=128)
        self.assertEqual(plan["meta"]["style"], "mono_ink")
        self.assertEqual(plan["meta"]["subjects"], ["cat"])
        self.assertEqual(plan["meta"]["colors"], ["black"])
        self.assertEqual(plan["meta"]["layout"], None)
        self.assertIsInstance(warnings, list)

    def test_plan_is_deterministic_for_same_seed(self):
        found, _meta = intent.normalize_intent(payload(canvas="portrait"))
        first, _warnings = intent.plan_from_intent(found, seed=3)
        second, _warnings = intent.plan_from_intent(found, seed=3)
        self.assertEqual(plan_hash(first), plan_hash(second))

    def test_count_and_ratio_reach_the_planner(self):
        found, _meta = intent.normalize_intent(
            payload(subject=["star"], count=5, colors=["purple"], canvas="square"),
            prompt=None,
        )
        plan, _warnings = intent.plan_from_intent(found, seed=0)
        self.assertIn("5个五角星", plan["meta"]["prompt"])
        self.assertEqual(plan["meta"]["ratio"], "square")

    def test_text_and_negation_reach_the_planner(self):
        found, _meta = intent.normalize_intent(
            payload(text=["春天"], negations=["no_text"], subject=["flower"]),
            prompt=None,
        )
        # normalize_intent drops no_text when explicit text is present.
        self.assertEqual(found["negations"], [])
        plan, _warnings = intent.plan_from_intent(found, seed=0)
        self.assertEqual(plan["texts"][0]["text"], "春天")

    def test_multi_subject_count_warning(self):
        found, _meta = intent.normalize_intent(
            payload(subject=["moon", "mountain"], count=3), prompt=None
        )
        _plan, warnings = intent.plan_from_intent(found, seed=0)
        self.assertIn("FP_W_INTENT_COUNT_IGNORED", {item["code"] for item in warnings})

    def test_empty_intent_still_produces_a_plan(self):
        found, _meta = intent.normalize_intent(
            {"subject": [], "colors": [], "text": [], "negations": []}, prompt=None
        )
        plan, warnings = intent.plan_from_intent(found, seed=0)
        self.assertEqual(validate_plan(plan), [])
        self.assertIn("FP_W_INTENT_GENERIC_FALLBACK", {item["code"] for item in warnings})

    def test_style_is_passed_through_planner_kwarg(self):
        found, _meta = intent.normalize_intent(payload(style="cyber"), prompt=None)
        plan, _warnings = intent.plan_from_intent(found, seed=0)
        self.assertEqual(plan["meta"]["style"], "cyber")

    def test_fake_planner_contract(self):
        fake = FakePlanner()
        found, _meta = intent.normalize_intent(payload(), prompt=None)
        plan, warnings = intent.plan_from_intent(found, planner_fn=fake)
        self.assertEqual(plan["name"], "plan-0")
        self.assertEqual(warnings, [])
        self.assertEqual(len(fake.calls), 1)

    def test_non_dict_intent_rejected(self):
        with self.assertRaises(intent.IntentError) as caught:
            intent.plan_from_intent("not-an-intent")
        self.assertEqual(caught.exception.code, "FP_E_INTENT_TYPE")

    def test_intent_to_prompt_encodes_slots(self):
        text = intent.intent_to_prompt(
            {
                "subject": ["star"],
                "count": 5,
                "colors": ["purple"],
                "style": "cyber",
                "text": ["夏日"],
                "position": "center",
                "canvas": "portrait",
                "negations": ["no_background"],
                "confidence": 0.9,
            }
        )
        for fragment in ("5个五角星", "紫色", "居中", "竖版", '文字："夏日"', "不要背景"):
            self.assertIn(fragment, text)
        self.assertNotIn("赛博朋克", text)  # style goes through plan_prompt(style=...)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class IntentCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = intent.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_rule_only_json_writes_plan(self):
        plan_path = os.path.join(self.tmp, "plan.json")
        code, stdout, _stderr = self._run(
            ["--prompt", "画一只黑色的猫，线稿风格，居中", "--rule-only", "--json", "--out", plan_path]
        )
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(plan_path))
        report = json.loads(stdout)
        self.assertEqual(report["source"], "rule")
        self.assertTrue(report["compile_ok"])
        self.assertFalse(report["fallback"])
        self.assertIsNone(report["intent"])
        with open(plan_path, "r", encoding="utf-8") as handle:
            plan = json.load(handle)
        self.assertEqual(plan["schema"], "fdsl/2.1")

    def test_render_writes_png(self):
        png_path = os.path.join(self.tmp, "out.png")
        code, _stdout, stderr = self._run(
            ["--prompt", "画一颗红色的爱心", "--rule-only", "--render", png_path, "--resolution", "64"]
        )
        self.assertEqual(code, 0, stderr)
        self.assertTrue(os.path.isfile(png_path))
        self.assertGreater(os.path.getsize(png_path), 0)

    def test_missing_prompt_is_usage_error(self):
        code, _stdout, stderr = self._run(["--rule-only"])
        self.assertEqual(code, 2)
        self.assertIn("--prompt", stderr)

    def test_bad_resolution_is_usage_error(self):
        code, _stdout, _stderr = self._run(["--prompt", "猫", "--rule-only", "--resolution", "9999"])
        self.assertEqual(code, 2)

    def test_bad_device_is_usage_error(self):
        code, _stdout, _stderr = self._run(["--prompt", "猫", "--device", "tpu"])
        self.assertEqual(code, 2)

    def test_unparseable_prompt_exits_3(self):
        code, _stdout, stderr = self._run(["--prompt", "!!!@@@", "--rule-only"])
        self.assertEqual(code, 3)
        self.assertIn("FP_E_PROMPT_UNPARSEABLE", stderr)

    def test_model_failure_falls_back_to_rule_planner(self):
        plan_path = os.path.join(self.tmp, "plan.json")
        with mock.patch.object(intent, "_load_model", side_effect=FileNotFoundError("no model")):
            code, stdout, stderr = self._run(
                [
                    "--prompt",
                    "画一只红色的爱心，水彩风格",
                    "--model-dir",
                    "missing",
                    "--device",
                    "cpu",
                    "--json",
                    "--out",
                    plan_path,
                ]
            )
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual(report["source"], "rule_fallback")
        self.assertTrue(report["fallback"])
        self.assertFalse(report["intent_ok"])
        self.assertTrue(os.path.isfile(plan_path))

    def test_intent_path_end_to_end_with_stub(self):
        plan_path = os.path.join(self.tmp, "plan.json")
        intent_path = os.path.join(self.tmp, "intent.json")
        stub_intent, stub_meta = intent.normalize_intent(
            payload(subject=["heart"], colors=["red"], style="watercolor"), prompt=None
        )
        with mock.patch.object(
            intent, "extract_intent", return_value=(stub_intent, {"ok": True, "warnings": [], "errors": []})
        ) as patched:
            code, stdout, stderr = self._run(
                ["--prompt", "画一颗红色的爱心", "--json", "--out", plan_path, "--intent-out", intent_path]
            )
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual(report["source"], "llm")
        self.assertEqual(report["intent"]["subject"], ["heart"])
        self.assertEqual(report["plan"]["style"], "watercolor")
        patched.assert_called_once()
        self.assertTrue(os.path.isfile(intent_path))
        with open(intent_path, "r", encoding="utf-8") as handle:
            sidecar = json.load(handle)
        self.assertEqual(sidecar["intent"]["style"], "watercolor")


if __name__ == "__main__":
    unittest.main()
