"""Tests for JSON extraction, chat messages and assistant-only label masking."""

from __future__ import annotations

import json
import unittest

from llm_fixtures import FakeTokenizer, PlainTokenizer

from fourier_prompt.llm.formatting import (
    FDSL_SYSTEM_PROMPT,
    ExtractJSONError,
    FormattingError,
    build_messages,
    canonical_plan_json,
    extract_json,
    tokenize_example,
)


class ExtractJsonTests(unittest.TestCase):
    def test_plain_json_object(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_json_block(self):
        text = 'Here you go:\n```json\n{"schema": "fdsl/2.1", "n": 2}\n```\nDone.'
        self.assertEqual(extract_json(text)["schema"], "fdsl/2.1")

    def test_prose_around_object(self):
        text = 'Sure! The plan is {"schema": "fdsl/2.1", "x": [1, 2]} — enjoy.'
        self.assertEqual(extract_json(text)["x"], [1, 2])

    def test_braces_inside_strings_are_not_counted(self):
        text = 'prefix {"text": "a } b { c", "nested": {"k": "v"}} suffix'
        payload = extract_json(text)
        self.assertEqual(payload["text"], "a } b { c")
        self.assertEqual(payload["nested"], {"k": "v"})

    def test_trailing_comma_is_repaired(self):
        self.assertEqual(extract_json('{"a": 1, "b": [2, 3,],}'), {"a": 1, "b": [2, 3]})

    def test_plan_envelope_is_unwrapped(self):
        text = '{"plan": {"schema": "fdsl/2.1", "canvas": {"width": 8}}}'
        self.assertEqual(extract_json(text)["schema"], "fdsl/2.1")

    def test_bad_json_raises_with_code(self):
        with self.assertRaises(ExtractJSONError) as context:
            extract_json('{"a": 1,, "b": 2}')
        self.assertEqual(context.exception.code, "llm/json_invalid")

    def test_empty_text_raises(self):
        with self.assertRaises(ExtractJSONError) as context:
            extract_json("   \n\t ")
        self.assertEqual(context.exception.code, "llm/json_empty")

    def test_non_string_raises(self):
        with self.assertRaises(ExtractJSONError) as context:
            extract_json(None)
        self.assertEqual(context.exception.code, "llm/json_type")

    def test_top_level_array_is_rejected(self):
        with self.assertRaises(ExtractJSONError):
            extract_json("[1, 2, 3]")


class CanonicalJsonTests(unittest.TestCase):
    def test_compact_sorted_and_deterministic(self):
        left = {"b": 1, "a": {"d": 2, "c": 3}}
        right = {"a": {"c": 3, "d": 2}, "b": 1}
        encoded = canonical_plan_json(left)
        self.assertEqual(encoded, canonical_plan_json(right))
        self.assertNotIn(": ", encoded)
        self.assertNotIn(", ", encoded)
        self.assertEqual(json.loads(encoded), left)

    def test_non_dict_rejected(self):
        with self.assertRaises(FormattingError):
            canonical_plan_json([1, 2])


class MessageTests(unittest.TestCase):
    def test_build_messages_shape(self):
        messages = build_messages("  画一只猫  ")
        self.assertEqual([item["role"] for item in messages], ["system", "user"])
        self.assertEqual(messages[0]["content"], FDSL_SYSTEM_PROMPT)
        self.assertEqual(messages[1]["content"], "画一只猫")

    def test_feedback_appends_user_turn(self):
        messages = build_messages("draw", feedback="- schema/FP_E_SCHEMA: bad")
        self.assertEqual([item["role"] for item in messages], ["system", "user", "user"])
        self.assertIn("FP_E_SCHEMA", messages[-1]["content"])

    def test_empty_prompt_rejected(self):
        with self.assertRaises(FormattingError):
            build_messages("  ")


class TokenizeTests(unittest.TestCase):
    def test_prompt_tokens_are_masked(self):
        tokenizer = FakeTokenizer()
        plan_json = canonical_plan_json({"schema": "fdsl/2.1", "name": "x"})
        example = tokenize_example(tokenizer, "draw a cat", plan_json)
        prompt_tokens = example["num_prompt_tokens"]
        self.assertGreater(prompt_tokens, 0)
        self.assertGreater(example["num_assistant_tokens"], 0)
        self.assertTrue(all(token == -100 for token in example["labels"][:prompt_tokens]))
        self.assertTrue(all(token != -100 for token in example["labels"][prompt_tokens:]))
        self.assertEqual(example["labels"][prompt_tokens:], example["input_ids"][prompt_tokens:])
        self.assertEqual(len(example["input_ids"]), len(example["labels"]))
        self.assertEqual(len(example["input_ids"]), len(example["attention_mask"]))
        self.assertFalse(example["truncated"])

    def test_assistant_label_contains_plan_characters(self):
        tokenizer = FakeTokenizer()
        plan_json = canonical_plan_json({"schema": "fdsl/2.1", "name": "abc"})
        example = tokenize_example(tokenizer, "draw", plan_json)
        decoded_assistant = tokenizer.encode(plan_json)[1:]  # drop the fake BOS
        # The assistant token ids must contain the encoded plan as a subsequence
        # of the unmasked label tail (the template may add separators).
        tail = example["labels"][example["num_prompt_tokens"] :]
        self.assertTrue(set(decoded_assistant).issubset(set(tail)))

    def test_fallback_template_without_chat_template(self):
        tokenizer = PlainTokenizer()
        plan_json = canonical_plan_json({"schema": "fdsl/2.1"})
        example = tokenize_example(tokenizer, "draw", plan_json)
        self.assertGreater(example["num_assistant_tokens"], 0)
        self.assertEqual(example["labels"][0], -100)
        self.assertGreater(example["num_prompt_tokens"], 0)

    def test_truncation_keeps_max_len(self):
        tokenizer = FakeTokenizer()
        plan_json = canonical_plan_json({"schema": "fdsl/2.1", "pad": "x" * 400})
        example = tokenize_example(tokenizer, "draw a cat", plan_json, max_len=64)
        self.assertEqual(len(example["input_ids"]), 64)
        self.assertTrue(example["truncated"])
        self.assertGreater(example["assistant_truncated"], 0)

    def test_invalid_arguments_rejected(self):
        tokenizer = FakeTokenizer()
        with self.assertRaises(FormattingError):
            tokenize_example(tokenizer, "draw", "")
        with self.assertRaises(FormattingError):
            tokenize_example(tokenizer, "draw", "{}", max_len=4)


if __name__ == "__main__":
    unittest.main()
